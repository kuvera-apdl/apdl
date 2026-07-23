import type { TrackEvent } from './types';
import type { PersistenceMode } from './config';
import {
  assertDeploymentStorageScope,
  type DeploymentStorageScope,
} from './storage-scope';
import {
  assertSerializedEventSize,
  canonicalizeTrackEvent,
  MAX_EVENT_AGE_MS,
  MAX_EVENT_FUTURE_SKEW_MS,
  serializedJsonBytes,
} from './event-validation';

const DB_NAME = 'apdl-offline';
const STORE_NAME = 'events';
const DB_VERSION = 3;
// Earlier records were project-only and may also contain click text captured
// before the mandatory privacy guard. Reject them rather than guessing an
// owning deployment or replaying potentially sensitive legacy telemetry.
const RECORD_SCHEMA_VERSION = 3;
export const MAX_OFFLINE_EVENTS_PER_PROJECT = 1000;
export const MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT = 5 * 1024 * 1024;
const OFFLINE_RECORD_FIELDS = new Set([
  'id',
  'schema_version',
  'deployment_origin',
  'project_id',
  'stored_at',
  'serialized_bytes',
  'data',
]);

export interface OfflineStorageScope extends DeploymentStorageScope {
  persistence?: PersistenceMode;
}

interface StoredOfflineEvent {
  id?: number;
  schema_version: typeof RECORD_SCHEMA_VERSION;
  deployment_origin: string;
  project_id: string;
  stored_at: number;
  serialized_bytes: number;
  data: TrackEvent;
}

type RecordDisposition = 'current' | 'mismatched' | 'stale';

/**
 * IndexedDB-backed offline event storage with in-memory fallback.
 *
 * Every record is owned by the canonical deployment origin and project ID.
 * The credential itself is never persisted. A project can therefore rotate
 * its key and recover events within one deployment, while another deployment
 * or project sharing the same browser cannot drain or clear them.
 */
export class OfflineStorage {
  private readonly deploymentOrigin: string;
  private readonly projectId: string;
  private fallbackQueue: StoredOfflineEvent[] = [];
  private dbPromise: Promise<IDBDatabase | null> | null = null;
  private useMemory = false;

  constructor(scope: OfflineStorageScope) {
    assertDeploymentStorageScope(scope);
    this.deploymentOrigin = scope.deploymentOrigin;
    this.projectId = scope.projectId;
    this.useMemory = scope.persistence === 'memory';
    this.dbPromise = this.useMemory ? null : this.openDB();
  }

  private openDB(): Promise<IDBDatabase | null> | null {
    if (typeof indexedDB === 'undefined') {
      this.useMemory = true;
      return null;
    }

    return new Promise<IDBDatabase>((resolve, reject) => {
      let settled = false;
      try {
        const request = indexedDB.open(DB_NAME, DB_VERSION);

        request.onupgradeneeded = (event) => {
          const db = request.result;
          let store: IDBObjectStore;

          if (!db.objectStoreNames.contains(STORE_NAME)) {
            store = db.createObjectStore(STORE_NAME, {
              keyPath: 'id',
              autoIncrement: true,
            });
          } else {
            store = request.transaction!.objectStore(STORE_NAME);
          }

          // Earlier records were not deployment-scoped. They cannot be safely
          // attributed to an endpoint, so discard them during the migration.
          const oldVersion = (event as IDBVersionChangeEvent).oldVersion;
          if (oldVersion > 0 && oldVersion < DB_VERSION) {
            store.clear();
          }
        };

        request.onsuccess = () => {
          const db = request.result;
          if (settled) {
            db.close();
            return;
          }
          settled = true;
          db.onversionchange = () => db.close();
          resolve(db);
        };

        request.onerror = () => {
          if (settled) return;
          settled = true;
          this.useMemory = true;
          reject(request.error);
        };

        request.onblocked = () => {
          if (settled) return;
          settled = true;
          this.useMemory = true;
          reject(new Error('IndexedDB upgrade blocked'));
        };
      } catch {
        settled = true;
        this.useMemory = true;
        reject(new Error('IndexedDB not available'));
      }
    }).catch(() => {
      this.useMemory = true;
      return null;
    });
  }

  private async getDB(): Promise<IDBDatabase | null> {
    if (this.useMemory) return null;

    try {
      const db = await this.dbPromise;
      return db ?? null;
    } catch {
      this.useMemory = true;
      return null;
    }
  }

  async store(events: TrackEvent[]): Promise<void> {
    if (events.length === 0) return;

    const records = events
      .map((event) => this.createRecord(event))
      .filter((record): record is StoredOfflineEvent => record !== null);
    if (records.length === 0) return;

    const db = await this.getDB();
    if (!db) {
      this.fallbackQueue.push(...records);
      this.enforceFallbackBounds(Date.now());
      return;
    }

    return new Promise<void>((resolve) => {
      let fellBack = false;
      const preserveInMemory = () => {
        if (!fellBack) {
          fellBack = true;
          this.fallbackQueue.push(...records);
          this.enforceFallbackBounds(Date.now());
        }
        resolve();
      };

      try {
        const tx = db.transaction(STORE_NAME, 'readwrite');
        const store = tx.objectStore(STORE_NAME);

        for (const record of records) {
          store.add(record);
        }
        this.enforceIndexedDBBounds(store, tx, Date.now());

        tx.oncomplete = () => resolve();
        tx.onerror = preserveInMemory;
        tx.onabort = preserveInMemory;
      } catch {
        preserveInMemory();
      }
    });
  }

  /**
   * Removes and returns only records owned by this deployment and project.
   *
   * Valid records owned by other projects remain isolated for their owner.
   * Invalid, legacy, clock-skewed, and records older than seven days are
   * purged and never returned to any client.
   */
  async drain(): Promise<TrackEvent[]> {
    const now = Date.now();
    const memoryEvents = this.drainFallback(now);
    const db = await this.getDB();
    if (!db) return memoryEvents;

    return new Promise<TrackEvent[]>((resolve) => {
      let settled = false;
      let scanFinished = false;
      const events: TrackEvent[] = [];
      const finish = (includeDatabaseEvents: boolean) => {
        if (settled) return;
        settled = true;
        resolve([
          ...(includeDatabaseEvents ? events : []),
          ...memoryEvents,
        ]);
      };

      try {
        const tx = db.transaction(STORE_NAME, 'readwrite');
        const store = tx.objectStore(STORE_NAME);
        const cursorRequest = store.openCursor();

        cursorRequest.onsuccess = () => {
          const cursor = cursorRequest.result;
          if (!cursor) {
            scanFinished = true;
            return;
          }

          const disposition = this.classifyRecord(cursor.value, now);
          if (disposition === 'current') {
            events.push((cursor.value as StoredOfflineEvent).data);
            cursor.delete();
          } else if (disposition === 'stale') {
            cursor.delete();
          }
          cursor.continue();
        };

        cursorRequest.onerror = () => finish(false);
        tx.oncomplete = () => finish(scanFinished);
        tx.onerror = () => finish(false);
        tx.onabort = () => finish(false);
      } catch {
        finish(false);
      }
    });
  }

  /** Clears this scope without deleting another deployment/project queue. */
  async clear(): Promise<void> {
    const now = Date.now();
    this.clearFallback(now);

    const db = await this.getDB();
    if (!db) return;

    return new Promise<void>((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        resolve();
      };

      try {
        const tx = db.transaction(STORE_NAME, 'readwrite');
        const store = tx.objectStore(STORE_NAME);
        const cursorRequest = store.openCursor();

        cursorRequest.onsuccess = () => {
          const cursor = cursorRequest.result;
          if (!cursor) return;

          const disposition = this.classifyRecord(cursor.value, now);
          if (disposition !== 'mismatched') {
            cursor.delete();
          }
          cursor.continue();
        };

        cursorRequest.onerror = finish;
        tx.oncomplete = finish;
        tx.onerror = finish;
        tx.onabort = finish;
      } catch {
        finish();
      }
    });
  }

  /** Returns the number of valid records owned by this deployment/project. */
  async count(): Promise<number> {
    const now = Date.now();
    const memoryCount = this.fallbackQueue.filter(
      (record) => this.classifyRecord(record, now) === 'current'
    ).length;
    const db = await this.getDB();
    if (!db) return memoryCount;

    return new Promise<number>((resolve) => {
      let count = 0;
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        resolve(count + memoryCount);
      };

      try {
        const tx = db.transaction(STORE_NAME, 'readonly');
        const store = tx.objectStore(STORE_NAME);
        const cursorRequest = store.openCursor();

        cursorRequest.onsuccess = () => {
          const cursor = cursorRequest.result;
          if (!cursor) return;

          if (this.classifyRecord(cursor.value, now) === 'current') {
            count += 1;
          }
          cursor.continue();
        };

        cursorRequest.onerror = finish;
        tx.oncomplete = finish;
        tx.onerror = finish;
        tx.onabort = finish;
      } catch {
        finish();
      }
    });
  }

  private createRecord(event: TrackEvent): StoredOfflineEvent | null {
    let canonical: TrackEvent;
    let serializedBytes: number | null;
    try {
      canonical = canonicalizeTrackEvent(event);
      assertSerializedEventSize(canonical);
      serializedBytes = serializedEventBytes(canonical);
    } catch {
      return null;
    }
    if (serializedBytes === null) return null;

    return {
      schema_version: RECORD_SCHEMA_VERSION,
      deployment_origin: this.deploymentOrigin,
      project_id: this.projectId,
      stored_at: Date.now(),
      serialized_bytes: serializedBytes,
      data: canonical,
    };
  }

  private enforceIndexedDBBounds(
    store: IDBObjectStore,
    tx: IDBTransaction,
    now: number
  ): void {
    let retainedCount = 0;
    let retainedBytes = 0;
    const cursorRequest = store.openCursor(null, 'prev');

    cursorRequest.onsuccess = () => {
      const cursor = cursorRequest.result;
      if (!cursor) return;

      const disposition = this.classifyRecord(cursor.value, now);
      if (disposition === 'stale') {
        cursor.delete();
      } else if (disposition === 'current') {
        const record = cursor.value as StoredOfflineEvent;
        const exceedsCount = retainedCount >= MAX_OFFLINE_EVENTS_PER_PROJECT;
        const exceedsBytes =
          retainedBytes + record.serialized_bytes >
          MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT;
        if (exceedsCount || exceedsBytes) {
          // Descending key order makes this a deterministic oldest-first
          // eviction policy: retain the newest bounded set for this project.
          cursor.delete();
        } else {
          retainedCount += 1;
          retainedBytes += record.serialized_bytes;
        }
      }
      // Valid records owned by another project are never counted or deleted.
      cursor.continue();
    };

    cursorRequest.onerror = () => {
      try {
        tx.abort();
      } catch {
        // The transaction may already have aborted because of another request.
      }
    };
  }

  private classifyRecord(value: unknown, now: number): RecordDisposition {
    if (!isStoredOfflineEvent(value)) return 'stale';

    if (
      value.stored_at > now + MAX_EVENT_FUTURE_SKEW_MS ||
      now - value.stored_at > MAX_EVENT_AGE_MS
    ) {
      return 'stale';
    }

    return value.deployment_origin === this.deploymentOrigin
      && value.project_id === this.projectId
      ? 'current'
      : 'mismatched';
  }

  private drainFallback(now: number): TrackEvent[] {
    const events: TrackEvent[] = [];
    const retained: StoredOfflineEvent[] = [];

    for (const record of this.fallbackQueue) {
      const disposition = this.classifyRecord(record, now);
      if (disposition === 'current') {
        events.push(record.data);
      } else if (disposition === 'mismatched') {
        retained.push(record);
      }
    }

    this.fallbackQueue = retained;
    return events;
  }

  private clearFallback(now: number): void {
    this.fallbackQueue = this.fallbackQueue.filter(
      (record) => this.classifyRecord(record, now) === 'mismatched'
    );
  }

  private enforceFallbackBounds(now: number): void {
    const retainedNewestFirst: StoredOfflineEvent[] = [];
    let retainedCount = 0;
    let retainedBytes = 0;

    for (let index = this.fallbackQueue.length - 1; index >= 0; index -= 1) {
      const record = this.fallbackQueue[index];
      const disposition = this.classifyRecord(record, now);
      if (disposition === 'mismatched') {
        retainedNewestFirst.push(record);
        continue;
      }
      if (disposition === 'stale') continue;

      const exceedsCount = retainedCount >= MAX_OFFLINE_EVENTS_PER_PROJECT;
      const exceedsBytes =
        retainedBytes + record.serialized_bytes >
        MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT;
      if (!exceedsCount && !exceedsBytes) {
        retainedNewestFirst.push(record);
        retainedCount += 1;
        retainedBytes += record.serialized_bytes;
      }
    }

    this.fallbackQueue = retainedNewestFirst.reverse();
  }
}

function isStoredOfflineEvent(value: unknown): value is StoredOfflineEvent {
  if (!isPlainObject(value)) return false;
  if (Object.keys(value).some((field) => !OFFLINE_RECORD_FIELDS.has(field))) {
    return false;
  }

  if (
    !(
      value.id === undefined ||
      (typeof value.id === 'number' && Number.isInteger(value.id) && value.id > 0)
    ) ||
    value.schema_version !== RECORD_SCHEMA_VERSION ||
    typeof value.deployment_origin !== 'string' ||
    typeof value.project_id !== 'string' ||
    typeof value.stored_at !== 'number' ||
    !Number.isFinite(value.stored_at) ||
    value.stored_at <= 0 ||
    typeof value.serialized_bytes !== 'number' ||
    !Number.isInteger(value.serialized_bytes) ||
    value.serialized_bytes <= 0 ||
    !isTrackEvent(value.data)
  ) {
    return false;
  }

  try {
    assertDeploymentStorageScope({
      deploymentOrigin: value.deployment_origin,
      projectId: value.project_id,
    });
  } catch {
    return false;
  }

  return serializedEventBytes(value.data) === value.serialized_bytes;
}

function isTrackEvent(value: unknown): value is TrackEvent {
  try {
    const canonical = canonicalizeTrackEvent(value);
    assertSerializedEventSize(canonical);
    return true;
  } catch {
    return false;
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function serializedEventBytes(event: TrackEvent): number | null {
  try {
    return serializedJsonBytes(event);
  } catch {
    // Circular references and BigInt values cannot be sent as JSON, so they
    // cannot be safely retained for a later transport attempt either.
    return null;
  }
}
