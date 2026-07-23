import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { resolveConfig } from '../../src/core/config';
import {
  MAX_OFFLINE_EVENTS_PER_PROJECT,
  MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT,
  OFFLINE_LEASE_DURATION_MS,
  OfflineStorage,
} from '../../src/core/storage';
import type { TrackEvent } from '../../src/core/types';
import { ENDPOINT } from '../helpers';

const PROJECT_A_KEY = 'client_projectA_0123456789abcdef';
const PROJECT_A_ROTATED_KEY = 'client_projectA_abcdef0123456789';
const PROJECT_B_KEY = 'client_projectB_fedcba9876543210';
const SECOND_ENDPOINT = 'https://api.second.test';
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

function createEvent(
  event: string,
  properties?: Record<string, unknown>
): TrackEvent {
  return {
    type: 'track',
    event,
    anonymousId: 'anon-1',
    context: {},
    timestamp: new Date().toISOString(),
    messageId: `message-${event}`,
    sessionId: 'session-1',
    ...(properties ? { properties } : {}),
  };
}

function serializedBytes(event: TrackEvent): number {
  return new TextEncoder().encode(JSON.stringify(event)).byteLength;
}

function createLargeEvent(event: string, payloadBytes: number): TrackEvent {
  return createEvent(event, { payload: { value: 'x'.repeat(payloadBytes) } });
}

function storageForKey(
  clientKey: string,
  endpoint: string = ENDPOINT
): OfflineStorage {
  const config = resolveConfig({
    endpoint,
    auth: { clientKey },
  });
  return new OfflineStorage({
    deploymentOrigin: config.endpoint,
    projectId: config.projectId,
  });
}

async function claimAndAcknowledge(
  storage: OfflineStorage,
  limit: number = MAX_OFFLINE_EVENTS_PER_PROJECT
): Promise<TrackEvent[]> {
  const claims = await storage.claim(limit);
  await storage.acknowledge(claims.map((claim) => claim.id));
  return claims.map((claim) => claim.event);
}

async function readRawRecords(): Promise<Array<Record<string, unknown>>> {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('events', 'readonly');
    const request = tx.objectStore('events').getAll();
    request.onsuccess = () => resolve(request.result as Array<Record<string, unknown>>);
    request.onerror = () => reject(request.error);
  });
}

async function databaseFor(storage: OfflineStorage): Promise<IDBDatabase> {
  const db = await (
    storage as unknown as {
      dbPromise: Promise<IDBDatabase | null>;
    }
  ).dbPromise;
  if (db === null) throw new Error('expected IndexedDB storage');
  return db;
}

async function addRawRecord(record: Record<string, unknown>): Promise<void> {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('events', 'readwrite');
    tx.objectStore('events').add(record);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('apdl-offline', 4);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function createLegacyDatabase(event: TrackEvent): Promise<void> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('apdl-offline', 2);
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore('events', {
        keyPath: 'id',
        autoIncrement: true,
      });
      store.add({
        schema_version: 2,
        owner_id: 'project:projectA',
        stored_at: Date.now(),
        serialized_bytes: serializedBytes(event),
        data: event,
      });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  db.close();
}

async function createVersionThreeDatabase(event: TrackEvent): Promise<void> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('apdl-offline', 3);
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore('events', {
        keyPath: 'id',
        autoIncrement: true,
      });
      store.add({
        schema_version: 3,
        deployment_origin: ENDPOINT,
        project_id: 'projectA',
        stored_at: Date.now(),
        serialized_bytes: serializedBytes(event),
        data: event,
      });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  db.close();
}

describe('OfflineStorage', () => {
  beforeEach(() => {
    vi.stubGlobal('indexedDB', new IDBFactory());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('keeps offline retries in memory without opening IndexedDB in memory mode', async () => {
    const open = vi.spyOn(indexedDB, 'open');
    const storage = new OfflineStorage({
      deploymentOrigin: ENDPOINT,
      projectId: 'projectA',
      persistence: 'memory',
    });
    const event = createEvent('memory_only_event');

    const result = await storage.store([event]);

    expect(open).not.toHaveBeenCalled();
    expect(result).toEqual({
      stored: [{ event, durability: 'memory' }],
      evicted: [],
      rejected: [],
    });
    expect(Object.isFrozen(result)).toBe(true);
    expect(Object.isFrozen(result.stored)).toBe(true);
    expect(Object.isFrozen(result.stored[0])).toBe(true);
    expect(await storage.count()).toBe(1);
    expect(await claimAndAcknowledge(storage)).toEqual([event]);
  });

  it('isolates offline events for same-origin clients with different project keys', async () => {
    const projectA = storageForKey(PROJECT_A_KEY);
    const projectB = storageForKey(PROJECT_B_KEY);
    const projectAEvent = createEvent('project_a_event');

    await projectA.store([projectAEvent]);

    expect(await claimAndAcknowledge(projectB)).toEqual([]);
    expect(await projectB.count()).toBe(0);
    expect(await projectA.count()).toBe(1);
    expect(await claimAndAcknowledge(projectA)).toEqual([projectAEvent]);
  });

  it('isolates the same project across different deployment origins', async () => {
    const firstDeployment = storageForKey(PROJECT_A_KEY, ENDPOINT);
    const secondDeployment = storageForKey(PROJECT_A_KEY, SECOND_ENDPOINT);
    const firstEvent = createEvent('first_deployment_event');

    await firstDeployment.store([firstEvent]);

    expect(await claimAndAcknowledge(secondDeployment)).toEqual([]);
    expect(await secondDeployment.count()).toBe(0);
    expect(await claimAndAcknowledge(firstDeployment)).toEqual([firstEvent]);
  });

  it('persists a non-secret project owner marker instead of the client key', async () => {
    const storage = storageForKey(PROJECT_A_KEY);
    const event = createEvent('private_key_check');
    await storage.store([event]);

    const records = await readRawRecords();
    const serialized = JSON.stringify(records);

    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({
      schema_version: 4,
      deployment_origin: ENDPOINT,
      project_id: 'projectA',
      serialized_bytes: serializedBytes(event),
      claim_owner: null,
      claim_expires_at: null,
    });
    expect(serialized).not.toContain(PROJECT_A_KEY);
    expect(serialized).not.toContain('0123456789abcdef');
  });

  it('allows a rotated key for the same project to recover its events', async () => {
    const originalKeyStorage = storageForKey(PROJECT_A_KEY);
    const rotatedKeyStorage = storageForKey(PROJECT_A_ROTATED_KEY);
    const event = createEvent('pre_rotation_event');

    await originalKeyStorage.store([event]);

    expect(await claimAndAcknowledge(rotatedKeyStorage)).toEqual([event]);
  });

  it('retains a restored record durably until its owner acknowledges it', async () => {
    const firstTab = storageForKey(PROJECT_A_KEY);
    const secondTab = storageForKey(PROJECT_A_ROTATED_KEY);
    const event = createEvent('restore_without_ack');
    await firstTab.store([event]);

    const claims = await firstTab.claim(1);

    expect(claims).toEqual([{ id: expect.any(Number), event }]);
    expect(await firstTab.count()).toBe(1);
    expect(await secondTab.claim(1)).toEqual([]);
    expect(await firstTab.count()).toBe(1);
  });

  it('reclaims an expired crash lease and rejects the stale owner acknowledgement', async () => {
    const initialNow = Date.now();
    const now = vi.spyOn(Date, 'now').mockReturnValue(initialNow);
    const crashedTab = storageForKey(PROJECT_A_KEY);
    const recoveryTab = storageForKey(PROJECT_A_ROTATED_KEY);
    const event = createEvent('crash_recovery');
    await crashedTab.store([event]);
    const [crashedClaim] = await crashedTab.claim(1);

    now.mockReturnValue(initialNow + OFFLINE_LEASE_DURATION_MS + 1);
    const [recoveredClaim] = await recoveryTab.claim(1);

    expect(recoveredClaim).toEqual({
      id: crashedClaim.id,
      event,
    });
    expect(await crashedTab.acknowledge([crashedClaim.id])).toBe(0);
    expect(await recoveryTab.acknowledge([recoveredClaim.id])).toBe(1);
    expect(await recoveryTab.count()).toBe(0);
  });

  it('allows an owner to release a retry for immediate recovery', async () => {
    const firstTab = storageForKey(PROJECT_A_KEY);
    const secondTab = storageForKey(PROJECT_A_ROTATED_KEY);
    const event = createEvent('released_retry');
    await firstTab.store([event]);
    const [claim] = await firstTab.claim(1);

    expect(await firstTab.release([claim.id])).toBe(1);
    await expect(secondTab.claim(1)).resolves.toEqual([
      { id: claim.id, event },
    ]);
  });

  it('atomically grants a record to only one of two concurrent tabs', async () => {
    const firstTab = storageForKey(PROJECT_A_KEY);
    const secondTab = storageForKey(PROJECT_A_ROTATED_KEY);
    const event = createEvent('concurrent_claim');
    await firstTab.store([event]);

    const [firstClaims, secondClaims] = await Promise.all([
      firstTab.claim(1),
      secondTab.claim(1),
    ]);

    expect(firstClaims.length + secondClaims.length).toBe(1);
    expect([...firstClaims, ...secondClaims]).toEqual([
      { id: expect.any(Number), event },
    ]);
  });

  it('clears only the current project and retains another project queue', async () => {
    const projectA = storageForKey(PROJECT_A_KEY);
    const projectB = storageForKey(PROJECT_B_KEY);
    const projectBEvent = createEvent('project_b_event');

    await projectA.store([createEvent('project_a_event')]);
    await projectB.store([projectBEvent]);

    await projectA.clear();

    expect(await projectA.count()).toBe(0);
    expect(await claimAndAcknowledge(projectB)).toEqual([projectBEvent]);
  });

  it('enforces the per-project count bound in IndexedDB without evicting another project', async () => {
    const projectA = storageForKey(PROJECT_A_KEY);
    const projectB = storageForKey(PROJECT_B_KEY);
    const projectBEvent = createEvent('project_b_retained');
    const projectAEvents = Array.from(
      { length: MAX_OFFLINE_EVENTS_PER_PROJECT + 2 },
      (_, index) => createEvent(`bounded_${index}`)
    );

    await projectB.store([projectBEvent]);
    const result = await projectA.store(projectAEvents);

    const retained = await claimAndAcknowledge(projectA);
    expect(result.stored).toHaveLength(MAX_OFFLINE_EVENTS_PER_PROJECT);
    expect(result.evicted).toEqual([
      {
        event: projectAEvents[0],
        reason: 'offline_count_limit',
      },
      {
        event: projectAEvents[1],
        reason: 'offline_count_limit',
      },
    ]);
    expect(result.rejected).toEqual([]);
    expect(retained).toHaveLength(MAX_OFFLINE_EVENTS_PER_PROJECT);
    expect(retained[0].event).toBe('bounded_2');
    expect(retained.at(-1)?.event).toBe(
      `bounded_${MAX_OFFLINE_EVENTS_PER_PROJECT + 1}`
    );
    expect(await claimAndAcknowledge(projectB)).toEqual([projectBEvent]);
  });

  it('enforces the per-project serialized-byte bound in IndexedDB', async () => {
    const storage = storageForKey(PROJECT_A_KEY);
    const events = Array.from(
      { length: 90 },
      (_, index) => createLargeEvent(`large_event_${index}`, 60_000)
    );
    expect(events.reduce((total, event) => total + serializedBytes(event), 0)).toBeGreaterThan(
      MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT
    );

    const result = await storage.store(events);

    const retained = await claimAndAcknowledge(storage);
    expect(result.stored).toHaveLength(retained.length);
    expect(result.stored.every(({ durability }) => durability === 'durable'))
      .toBe(true);
    expect(result.evicted).toHaveLength(events.length - retained.length);
    expect(
      result.evicted.every(
        ({ reason }) => reason === 'offline_byte_limit'
      )
    ).toBe(true);
    expect(retained.length).toBeLessThan(events.length);
    expect(retained.reduce((total, event) => total + serializedBytes(event), 0)).toBeLessThanOrEqual(
      MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT
    );
    expect(retained.at(-1)?.event).toBe('large_event_89');
    expect(retained[0].event).toBe(`large_event_${events.length - retained.length}`);

    const oversized = createLargeEvent(
      'single_oversized_event',
      MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT
    );
    const oversizedResult = await storage.store([oversized]);
    expect(oversizedResult).toEqual({
      stored: [],
      evicted: [],
      rejected: [
        {
          event: oversized,
          reason: 'offline_invalid_event',
        },
      ],
    });
    expect(await storage.count()).toBe(0);
  });

  it('never evicts an active lease while reporting displaced new records', async () => {
    const storage = storageForKey(PROJECT_A_KEY);
    const leasedEvent = createEvent('actively_leased');
    await storage.store([leasedEvent]);
    const [lease] = await storage.claim(1);
    const newEvents = Array.from(
      { length: MAX_OFFLINE_EVENTS_PER_PROJECT + 1 },
      (_, index) => createEvent(`lease_pressure_${index}`)
    );

    const result = await storage.store(newEvents);

    expect(result.evicted).toContainEqual({
      event: newEvents[0],
      reason: 'offline_count_limit',
    });
    expect(result.evicted.map(({ event }) => event.messageId))
      .not.toContain(leasedEvent.messageId);
    expect(await storage.count()).toBe(
      MAX_OFFLINE_EVENTS_PER_PROJECT + 1
    );
    expect(await storage.acknowledge([lease.id])).toBe(1);
    expect(await storage.count()).toBe(MAX_OFFLINE_EVENTS_PER_PROJECT);
  });

  it('enforces count and byte bounds in the memory fallback', async () => {
    vi.stubGlobal('indexedDB', undefined);
    const countStorage = storageForKey(PROJECT_A_KEY);
    const countEvents = Array.from(
      { length: MAX_OFFLINE_EVENTS_PER_PROJECT + 1 },
      (_, index) => createEvent(`memory_count_${index}`)
    );

    const countResult = await countStorage.store(countEvents);
    const countRetained = await claimAndAcknowledge(countStorage);
    expect(countResult.stored).toHaveLength(
      MAX_OFFLINE_EVENTS_PER_PROJECT
    );
    expect(
      countResult.stored.every(
        ({ durability }) => durability === 'memory'
      )
    ).toBe(true);
    expect(countResult.evicted).toEqual([
      {
        event: countEvents[0],
        reason: 'offline_count_limit',
      },
    ]);
    expect(countRetained).toHaveLength(MAX_OFFLINE_EVENTS_PER_PROJECT);
    expect(countRetained[0].event).toBe('memory_count_1');

    const byteStorage = storageForKey(PROJECT_B_KEY);
    const byteEvents = Array.from(
      { length: 90 },
      (_, index) => createLargeEvent(`memory_large_${index}`, 60_000)
    );

    const byteResult = await byteStorage.store(byteEvents);

    const byteRetained = await claimAndAcknowledge(byteStorage);
    expect(byteResult.stored).toHaveLength(byteRetained.length);
    expect(
      byteResult.evicted.every(
        ({ reason }) => reason === 'offline_byte_limit'
      )
    ).toBe(true);
    expect(byteRetained.length).toBeLessThan(byteEvents.length);
    expect(byteRetained.reduce((total, event) => total + serializedBytes(event), 0)).toBeLessThanOrEqual(
      MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT
    );
    expect(byteRetained.at(-1)?.event).toBe('memory_large_89');
  });

  it('does not retain events that cannot be serialized for transport', async () => {
    const storage = storageForKey(PROJECT_A_KEY);
    const circularProperties: Record<string, unknown> = {};
    circularProperties.self = circularProperties;
    const serializableEvent = createEvent('serializable_event');

    const circularEvent = createEvent('circular_event', circularProperties);
    const result = await storage.store([circularEvent, serializableEvent]);

    expect(result.stored).toEqual([
      { event: serializableEvent, durability: 'durable' },
    ]);
    expect(result.evicted).toEqual([]);
    expect(result.rejected).toEqual([
      {
        event: circularEvent,
        reason: 'offline_invalid_event',
      },
    ]);
    expect(await claimAndAcknowledge(storage)).toEqual([serializableEvent]);
  });

  it('falls back atomically when a partial IndexedDB batch setup fails', async () => {
    const storage = storageForKey(PROJECT_A_KEY);
    const db = await databaseFor(storage);
    const probeTransaction = db.transaction('events', 'readonly');
    const objectStorePrototype = Object.getPrototypeOf(
      probeTransaction.objectStore('events')
    ) as { add: IDBObjectStore['add'] };
    const originalAdd = objectStorePrototype.add;
    let addCount = 0;
    vi.spyOn(objectStorePrototype, 'add').mockImplementation(function (
      this: IDBObjectStore,
      value: unknown,
      key?: IDBValidKey
    ) {
      addCount += 1;
      if (addCount === 2) {
        throw new Error('simulated partial batch setup failure');
      }
      return key === undefined
        ? originalAdd.call(this, value)
        : originalAdd.call(this, value, key);
    });
    const events = [
      createEvent('partial_failure_first'),
      createEvent('partial_failure_second'),
    ];

    const result = await storage.store(events);

    expect(result).toEqual({
      stored: events.map((event) => ({ event, durability: 'memory' })),
      evicted: [],
      rejected: [],
    });
    expect(await readRawRecords()).toEqual([]);
    expect(await claimAndAcknowledge(storage)).toEqual(events);
  });

  it('purges invalid and expired records without returning them', async () => {
    const projectA = storageForKey(PROJECT_A_KEY);
    await projectA.count();

    await addRawRecord({ data: createEvent('legacy_event') });
    const expiredEvent = createEvent('expired_event');
    await addRawRecord({
      schema_version: 1,
      owner_id: 'project:projectA',
      stored_at: Date.now() - WEEK_MS - 1,
      serialized_bytes: serializedBytes(expiredEvent),
      data: expiredEvent,
    });

    expect(await claimAndAcknowledge(projectA)).toEqual([]);
    expect(await readRawRecords()).toEqual([]);
  });

  it('rejects non-canonical lease fields instead of accepting aliases', async () => {
    const projectA = storageForKey(PROJECT_A_KEY);
    await projectA.count();
    const event = createEvent('invalid_lease_alias');
    await addRawRecord({
      schema_version: 4,
      deployment_origin: ENDPOINT,
      project_id: 'projectA',
      stored_at: Date.now(),
      serialized_bytes: serializedBytes(event),
      claim_owner: null,
      claim_expires_at: null,
      lease_owner: 'legacy-alias',
      data: event,
    });

    expect(await claimAndAcknowledge(projectA)).toEqual([]);
    expect(await readRawRecords()).toEqual([]);
  });

  it('purges current version 1 records that can contain unsafe click text', async () => {
    const projectA = storageForKey(PROJECT_A_KEY);
    await projectA.count();
    const unsafeEvent = createEvent('$click', {
      text: 'stored-password',
      tag: 'input',
    });
    await addRawRecord({
      schema_version: 1,
      owner_id: 'project:projectA',
      stored_at: Date.now(),
      serialized_bytes: serializedBytes(unsafeEvent),
      data: unsafeEvent,
    });

    expect(await claimAndAcknowledge(projectA)).toEqual([]);
    expect(await readRawRecords()).toEqual([]);
  });

  it('discards project-only version 2 records during the database upgrade', async () => {
    await createLegacyDatabase(createEvent('legacy_event'));

    const projectA = storageForKey(PROJECT_A_KEY);

    expect(await claimAndAcknowledge(projectA)).toEqual([]);
    expect(await readRawRecords()).toEqual([]);
  });

  it('migrates deployment-scoped version 3 records to the canonical lease schema', async () => {
    const event = createEvent('version_three_event');
    await createVersionThreeDatabase(event);

    const projectA = storageForKey(PROJECT_A_KEY);

    expect(await claimAndAcknowledge(projectA)).toEqual([event]);
    expect(await readRawRecords()).toEqual([]);
  });
});
