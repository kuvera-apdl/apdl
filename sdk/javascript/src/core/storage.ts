import type { TrackEvent } from './types';

const DB_NAME = 'apdl-offline';
const STORE_NAME = 'events';
const DB_VERSION = 2;
const RECORD_SCHEMA_VERSION = 1;
const MAX_RECORD_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_CLOCK_SKEW_MS = 5 * 60 * 1000;
export const MAX_OFFLINE_EVENTS_PER_PROJECT = 1000;
export const MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT = 5 * 1024 * 1024;
const PROJECT_ID_PATTERN = /^[a-zA-Z0-9]{1,64}$/;
const OWNER_ID_PATTERN = /^project:[a-zA-Z0-9]{1,64}$/;
const TRACK_EVENT_FIELDS = new Set([
  'type',
  'event',
  'userId',
  'anonymousId',
  'groupId',
  'properties',
  'traits',
  'context',
  'timestamp',
  'messageId',
  'sessionId',
]);
const OFFLINE_RECORD_FIELDS = new Set([
  'id',
  'schema_version',
  'owner_id',
  'stored_at',
  'serialized_bytes',
  'data',
]);

export interface OfflineStorageScope {
  projectId: string;
}

interface StoredOfflineEvent {
  id?: number;
  schema_version: typeof RECORD_SCHEMA_VERSION;
  owner_id: string;
  stored_at: number;
  serialized_bytes: number;
  data: TrackEvent;
}

type RecordDisposition = 'current' | 'mismatched' | 'stale';

/**
 * IndexedDB-backed offline event storage with in-memory fallback.
 *
 * Every record is owned by the canonical project ID derived from the client
 * key. The credential itself is never persisted. A project can therefore
 * rotate its key and recover its own events, while another project sharing the
 * same browser origin cannot drain or clear them.
 */
export class OfflineStorage {
  private readonly ownerId: string;
  private fallbackQueue: StoredOfflineEvent[] = [];
  private dbPromise: Promise<IDBDatabase | null> | null = null;
  private useMemory = false;

  constructor(scope: OfflineStorageScope) {
    if (!PROJECT_ID_PATTERN.test(scope.projectId)) {
      throw new Error('APDL: offline storage requires a canonical project ID');
    }

    this.ownerId = `project:${scope.projectId}`;
    this.dbPromise = this.openDB();
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

          // Version 1 records had no ownership marker. They cannot be safely
          // attributed to any project, so discard them during the migration.
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
   * Removes and returns only records owned by this project.
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

  /** Clears this project's records without deleting another project's queue. */
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

  /** Returns the number of valid records owned by this project. */
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
    const serializedBytes = serializedEventBytes(event);
    if (serializedBytes === null) return null;

    return {
      schema_version: RECORD_SCHEMA_VERSION,
      owner_id: this.ownerId,
      stored_at: Date.now(),
      serialized_bytes: serializedBytes,
      data: event,
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
      value.stored_at > now + MAX_CLOCK_SKEW_MS ||
      now - value.stored_at > MAX_RECORD_AGE_MS
    ) {
      return 'stale';
    }

    return value.owner_id === this.ownerId ? 'current' : 'mismatched';
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
    typeof value.owner_id !== 'string' ||
    !OWNER_ID_PATTERN.test(value.owner_id) ||
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

  return serializedEventBytes(value.data) === value.serialized_bytes;
}

function isTrackEvent(value: unknown): value is TrackEvent {
  if (!isPlainObject(value)) return false;
  if (Object.keys(value).some((field) => !TRACK_EVENT_FIELDS.has(field))) {
    return false;
  }

  if (!['track', 'identify', 'group', 'page'].includes(String(value.type))) {
    return false;
  }

  if (
    !isNonEmptyString(value.anonymousId) ||
    !isPlainObject(value.context) ||
    !isNonEmptyString(value.timestamp) ||
    !isNonEmptyString(value.messageId) ||
    !isNonEmptyString(value.sessionId)
  ) {
    return false;
  }

  return (
    isOptionalString(value.event) &&
    isOptionalString(value.userId) &&
    isOptionalString(value.groupId) &&
    isOptionalObject(value.properties) &&
    isOptionalObject(value.traits)
  );
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function isOptionalString(value: unknown): boolean {
  return value === undefined || typeof value === 'string';
}

function isOptionalObject(value: unknown): boolean {
  return value === undefined || isPlainObject(value);
}

function serializedEventBytes(event: TrackEvent): number | null {
  try {
    const serialized = JSON.stringify(event);
    return typeof serialized === 'string' ? utf8ByteLength(serialized) : null;
  } catch {
    // Circular references and BigInt values cannot be sent as JSON, so they
    // cannot be safely retained for a later transport attempt either.
    return null;
  }
}

function utf8ByteLength(value: string): number {
  let bytes = 0;
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit <= 0x7f) {
      bytes += 1;
    } else if (codeUnit <= 0x7ff) {
      bytes += 2;
    } else if (
      codeUnit >= 0xd800 &&
      codeUnit <= 0xdbff &&
      index + 1 < value.length &&
      value.charCodeAt(index + 1) >= 0xdc00 &&
      value.charCodeAt(index + 1) <= 0xdfff
    ) {
      bytes += 4;
      index += 1;
    } else {
      bytes += 3;
    }
  }
  return bytes;
}
