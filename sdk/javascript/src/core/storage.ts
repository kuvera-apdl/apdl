import { generateId, type TrackEvent } from './types';
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
const DB_VERSION = 4;
// Versions before 3 were project-only and may also contain click text captured
// before the mandatory privacy guard. Version 3 is migrated losslessly below.
const RECORD_SCHEMA_VERSION = 4;
export const MAX_OFFLINE_EVENTS_PER_PROJECT = 1000;
export const MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT = 5 * 1024 * 1024;
export const OFFLINE_LEASE_DURATION_MS = 5 * 60 * 1000;
const OFFLINE_RECORD_FIELDS = new Set([
  'id',
  'schema_version',
  'deployment_origin',
  'project_id',
  'stored_at',
  'serialized_bytes',
  'claim_owner',
  'claim_expires_at',
  'data',
]);
const VERSION_THREE_RECORD_FIELDS = new Set([
  'id',
  'schema_version',
  'deployment_origin',
  'project_id',
  'stored_at',
  'serialized_bytes',
  'data',
]);
const CLAIM_OWNER_PATTERN =
  /^lease_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

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
  claim_owner: string | null;
  claim_expires_at: number | null;
  data: TrackEvent;
}

type RecordDisposition = 'current' | 'mismatched' | 'stale';

export interface ClaimedOfflineEvent {
  id: number;
  event: TrackEvent;
}

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
  private readonly claimOwner = `lease_${generateId()}`;
  private fallbackQueue: StoredOfflineEvent[] = [];
  private fallbackNextId = Number.MAX_SAFE_INTEGER;
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

          const oldVersion = (event as IDBVersionChangeEvent).oldVersion;
          // Versions 1 and 2 were not deployment-scoped. They cannot be safely
          // attributed to an endpoint, so discard them during the migration.
          if (oldVersion > 0 && oldVersion < 3) {
            store.clear();
          } else if (oldVersion === 3) {
            // Version 3 is deployment-scoped and safe to preserve. Upgrade
            // every valid record into the one canonical leased-record shape;
            // invalid records are removed rather than accepted via aliases.
            const cursorRequest = store.openCursor();
            cursorRequest.onsuccess = () => {
              const cursor = cursorRequest.result;
              if (!cursor) return;
              const migrated = migrateVersionThreeRecord(cursor.value);
              if (migrated === null) {
                cursor.delete();
              } else {
                cursor.update(migrated);
              }
              cursor.continue();
            };
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
      this.appendFallback(records);
      this.enforceFallbackBounds(Date.now());
      return;
    }

    return new Promise<void>((resolve) => {
      let fellBack = false;
      const preserveInMemory = () => {
        if (!fellBack) {
          fellBack = true;
          this.appendFallback(records);
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
   * Atomically leases the oldest available records for this storage client.
   *
   * Claimed records remain durable until acknowledge() verifies the same
   * unexpired owner. Another tab can recover them only after release or lease
   * expiry, so merely restoring a record can never delete it.
   */
  async claim(limit: number): Promise<ClaimedOfflineEvent[]> {
    assertClaimLimit(limit);
    const now = Date.now();
    const expiresAt = now + OFFLINE_LEASE_DURATION_MS;
    const memoryClaims = this.claimFallback(limit, now, expiresAt);
    if (memoryClaims.length >= limit) return memoryClaims;

    const db = await this.getDB();
    if (!db) return memoryClaims;

    return new Promise<ClaimedOfflineEvent[]>((resolve) => {
      let settled = false;
      let scanFinished = false;
      const claims: ClaimedOfflineEvent[] = [];
      const finish = (includeDatabaseEvents: boolean) => {
        if (settled) return;
        settled = true;
        resolve([
          ...memoryClaims,
          ...(includeDatabaseEvents ? claims : []),
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
            const record = cursor.value as StoredOfflineEvent;
            if (
              claims.length + memoryClaims.length < limit &&
              isClaimAvailable(record, now)
            ) {
              const id = readCursorId(cursor);
              if (id === null) {
                cursor.delete();
              } else {
                record.claim_owner = this.claimOwner;
                record.claim_expires_at = expiresAt;
                cursor.update(record);
                claims.push({ id, event: record.data });
              }
            }
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

  /**
   * Deletes only records still held by this client's unexpired lease.
   *
   * The owner and expiry checks make a late acknowledgement harmless after
   * another tab has reclaimed a record.
   */
  async acknowledge(recordIds: readonly number[]): Promise<number> {
    const ids = normalizeRecordIds(recordIds);
    if (ids.length === 0) return 0;

    const now = Date.now();
    const fallbackAcknowledged = this.acknowledgeFallback(ids, now);
    const db = await this.getDB();
    if (!db) return fallbackAcknowledged;

    return new Promise<number>((resolve) => {
      let settled = false;
      let databaseAcknowledged = 0;
      const finish = (includeDatabaseCount: boolean) => {
        if (settled) return;
        settled = true;
        resolve(
          fallbackAcknowledged +
            (includeDatabaseCount ? databaseAcknowledged : 0)
        );
      };

      try {
        const tx = db.transaction(STORE_NAME, 'readwrite');
        const store = tx.objectStore(STORE_NAME);

        for (const id of ids) {
          const request = store.get(id);
          request.onsuccess = () => {
            const value: unknown = request.result;
            if (!isStoredOfflineEvent(value)) return;
            if (
              this.classifyRecord(value, now) === 'current' &&
              value.claim_owner === this.claimOwner &&
              value.claim_expires_at !== null &&
              value.claim_expires_at > now
            ) {
              store.delete(id);
              databaseAcknowledged += 1;
            }
          };
        }

        tx.oncomplete = () => finish(true);
        tx.onerror = () => finish(false);
        tx.onabort = () => finish(false);
      } catch {
        finish(false);
      }
    });
  }

  /** Releases this client's records for immediate recovery by another tab. */
  async release(recordIds: readonly number[]): Promise<number> {
    const ids = normalizeRecordIds(recordIds);
    if (ids.length === 0) return 0;

    const now = Date.now();
    const fallbackReleased = this.releaseFallback(ids, now);
    const db = await this.getDB();
    if (!db) return fallbackReleased;

    return new Promise<number>((resolve) => {
      let settled = false;
      let databaseReleased = 0;
      const finish = (includeDatabaseCount: boolean) => {
        if (settled) return;
        settled = true;
        resolve(
          fallbackReleased + (includeDatabaseCount ? databaseReleased : 0)
        );
      };

      try {
        const tx = db.transaction(STORE_NAME, 'readwrite');
        const store = tx.objectStore(STORE_NAME);

        for (const id of ids) {
          const request = store.get(id);
          request.onsuccess = () => {
            const value: unknown = request.result;
            if (
              !isStoredOfflineEvent(value) ||
              this.classifyRecord(value, now) !== 'current' ||
              value.claim_owner !== this.claimOwner
            ) {
              return;
            }
            value.claim_owner = null;
            value.claim_expires_at = null;
            store.put(value);
            databaseReleased += 1;
          };
        }

        tx.oncomplete = () => finish(true);
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
      claim_owner: null,
      claim_expires_at: null,
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
        const hasActiveLease =
          record.claim_owner !== null &&
          record.claim_expires_at !== null &&
          record.claim_expires_at > now;
        if ((exceedsCount || exceedsBytes) && !hasActiveLease) {
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

  private appendFallback(records: StoredOfflineEvent[]): void {
    for (const record of records) {
      this.fallbackQueue.push({
        ...record,
        id: this.fallbackNextId,
      });
      this.fallbackNextId -= 1;
    }
  }

  private claimFallback(
    limit: number,
    now: number,
    expiresAt: number
  ): ClaimedOfflineEvent[] {
    const claims: ClaimedOfflineEvent[] = [];
    const retained: StoredOfflineEvent[] = [];

    for (const record of this.fallbackQueue) {
      const disposition = this.classifyRecord(record, now);
      if (disposition === 'current') {
        if (claims.length < limit && isClaimAvailable(record, now)) {
          record.claim_owner = this.claimOwner;
          record.claim_expires_at = expiresAt;
          claims.push({ id: record.id!, event: record.data });
        }
        retained.push(record);
      } else if (disposition === 'mismatched') {
        retained.push(record);
      }
    }

    this.fallbackQueue = retained;
    return claims;
  }

  private acknowledgeFallback(recordIds: readonly number[], now: number): number {
    const ids = new Set(recordIds);
    let acknowledged = 0;
    this.fallbackQueue = this.fallbackQueue.filter((record) => {
      if (
        record.id !== undefined &&
        ids.has(record.id) &&
        this.classifyRecord(record, now) === 'current' &&
        record.claim_owner === this.claimOwner &&
        record.claim_expires_at !== null &&
        record.claim_expires_at > now
      ) {
        acknowledged += 1;
        return false;
      }
      return true;
    });
    return acknowledged;
  }

  private releaseFallback(recordIds: readonly number[], now: number): number {
    const ids = new Set(recordIds);
    let released = 0;
    for (const record of this.fallbackQueue) {
      if (
        record.id !== undefined &&
        ids.has(record.id) &&
        this.classifyRecord(record, now) === 'current' &&
        record.claim_owner === this.claimOwner
      ) {
        record.claim_owner = null;
        record.claim_expires_at = null;
        released += 1;
      }
    }
    return released;
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
      const hasActiveLease =
        record.claim_owner !== null &&
        record.claim_expires_at !== null &&
        record.claim_expires_at > now;
      if ((!exceedsCount && !exceedsBytes) || hasActiveLease) {
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
    !hasCanonicalClaimState(value) ||
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

function hasCanonicalClaimState(value: Record<string, unknown>): boolean {
  if (value.claim_owner === null && value.claim_expires_at === null) {
    return true;
  }
  return (
    typeof value.claim_owner === 'string' &&
    CLAIM_OWNER_PATTERN.test(value.claim_owner) &&
    typeof value.claim_expires_at === 'number' &&
    Number.isSafeInteger(value.claim_expires_at) &&
    value.claim_expires_at > 0
  );
}

function migrateVersionThreeRecord(value: unknown): StoredOfflineEvent | null {
  if (!isPlainObject(value)) return null;
  if (
    Object.keys(value).some(
      (field) => !VERSION_THREE_RECORD_FIELDS.has(field)
    )
  ) {
    return null;
  }
  if (
    value.schema_version !== 3 ||
    !(
      value.id === undefined ||
      (typeof value.id === 'number' &&
        Number.isInteger(value.id) &&
        value.id > 0)
    ) ||
    typeof value.deployment_origin !== 'string' ||
    typeof value.project_id !== 'string' ||
    typeof value.stored_at !== 'number' ||
    !Number.isFinite(value.stored_at) ||
    value.stored_at <= 0 ||
    typeof value.serialized_bytes !== 'number' ||
    !Number.isInteger(value.serialized_bytes) ||
    value.serialized_bytes <= 0 ||
    !isTrackEvent(value.data) ||
    serializedEventBytes(value.data) !== value.serialized_bytes
  ) {
    return null;
  }

  try {
    assertDeploymentStorageScope({
      deploymentOrigin: value.deployment_origin,
      projectId: value.project_id,
    });
  } catch {
    return null;
  }

  return {
    ...(value.id === undefined ? {} : { id: value.id }),
    schema_version: RECORD_SCHEMA_VERSION,
    deployment_origin: value.deployment_origin,
    project_id: value.project_id,
    stored_at: value.stored_at,
    serialized_bytes: value.serialized_bytes,
    claim_owner: null,
    claim_expires_at: null,
    data: value.data,
  };
}

function isClaimAvailable(record: StoredOfflineEvent, now: number): boolean {
  return (
    record.claim_owner === null ||
    (record.claim_expires_at !== null && record.claim_expires_at <= now)
  );
}

function readCursorId(cursor: IDBCursorWithValue): number | null {
  const id = cursor.primaryKey;
  return typeof id === 'number' && Number.isInteger(id) && id > 0
    ? id
    : null;
}

function assertClaimLimit(limit: number): void {
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_OFFLINE_EVENTS_PER_PROJECT) {
    throw new Error(
      `APDL: offline claim limit must be an integer between 1 and ${MAX_OFFLINE_EVENTS_PER_PROJECT}`
    );
  }
}

function normalizeRecordIds(recordIds: readonly number[]): number[] {
  const ids = [...new Set(recordIds)];
  if (
    ids.some(
      (id) => !Number.isSafeInteger(id) || id < 1
    )
  ) {
    throw new Error('APDL: offline record IDs must be positive safe integers');
  }
  return ids;
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
