import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { resolveConfig } from '../../src/core/config';
import {
  MAX_OFFLINE_EVENTS_PER_PROJECT,
  MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT,
  OfflineStorage,
} from '../../src/core/storage';
import type { TrackEvent } from '../../src/core/types';
import { ENDPOINT } from '../helpers';

const PROJECT_A_KEY = 'proj_projectA_0123456789abcdef';
const PROJECT_A_ROTATED_KEY = 'proj_projectA_abcdef0123456789';
const PROJECT_B_KEY = 'proj_projectB_fedcba9876543210';
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
  return createEvent(event, { payload: 'x'.repeat(payloadBytes) });
}

function storageForKey(clientKey: string): OfflineStorage {
  const config = resolveConfig({
    endpoint: ENDPOINT,
    auth: { clientKey },
  });
  return new OfflineStorage({ projectId: config.projectId });
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
    const request = indexedDB.open('apdl-offline', 2);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function createLegacyDatabase(event: TrackEvent): Promise<void> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('apdl-offline', 1);
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore('events', {
        keyPath: 'id',
        autoIncrement: true,
      });
      store.add({ data: event });
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
    vi.unstubAllGlobals();
  });

  it('isolates offline events for same-origin clients with different project keys', async () => {
    const projectA = storageForKey(PROJECT_A_KEY);
    const projectB = storageForKey(PROJECT_B_KEY);
    const projectAEvent = createEvent('project_a_event');

    await projectA.store([projectAEvent]);

    expect(await projectB.drain()).toEqual([]);
    expect(await projectB.count()).toBe(0);
    expect(await projectA.count()).toBe(1);
    expect(await projectA.drain()).toEqual([projectAEvent]);
  });

  it('persists a non-secret project owner marker instead of the client key', async () => {
    const storage = storageForKey(PROJECT_A_KEY);
    const event = createEvent('private_key_check');
    await storage.store([event]);

    const records = await readRawRecords();
    const serialized = JSON.stringify(records);

    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({
      schema_version: 1,
      owner_id: 'project:projectA',
      serialized_bytes: serializedBytes(event),
    });
    expect(serialized).not.toContain(PROJECT_A_KEY);
    expect(serialized).not.toContain('0123456789abcdef');
  });

  it('allows a rotated key for the same project to recover its events', async () => {
    const originalKeyStorage = storageForKey(PROJECT_A_KEY);
    const rotatedKeyStorage = storageForKey(PROJECT_A_ROTATED_KEY);
    const event = createEvent('pre_rotation_event');

    await originalKeyStorage.store([event]);

    expect(await rotatedKeyStorage.drain()).toEqual([event]);
  });

  it('clears only the current project and retains another project queue', async () => {
    const projectA = storageForKey(PROJECT_A_KEY);
    const projectB = storageForKey(PROJECT_B_KEY);
    const projectBEvent = createEvent('project_b_event');

    await projectA.store([createEvent('project_a_event')]);
    await projectB.store([projectBEvent]);

    await projectA.clear();

    expect(await projectA.count()).toBe(0);
    expect(await projectB.drain()).toEqual([projectBEvent]);
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
    await projectA.store(projectAEvents);

    const retained = await projectA.drain();
    expect(retained).toHaveLength(MAX_OFFLINE_EVENTS_PER_PROJECT);
    expect(retained[0].event).toBe('bounded_2');
    expect(retained.at(-1)?.event).toBe(
      `bounded_${MAX_OFFLINE_EVENTS_PER_PROJECT + 1}`
    );
    expect(await projectB.drain()).toEqual([projectBEvent]);
  });

  it('enforces the per-project serialized-byte bound in IndexedDB', async () => {
    const storage = storageForKey(PROJECT_A_KEY);
    const payloadBytes = Math.floor(
      MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT / 2
    );
    const older = createLargeEvent('older_large_event', payloadBytes);
    const newer = createLargeEvent('newer_large_event', payloadBytes);

    expect(serializedBytes(older)).toBeLessThan(
      MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT
    );
    expect(serializedBytes(older) + serializedBytes(newer)).toBeGreaterThan(
      MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT
    );

    await storage.store([older, newer]);

    expect(await storage.drain()).toEqual([newer]);

    await storage.store([
      createLargeEvent(
        'single_oversized_event',
        MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT
      ),
    ]);
    expect(await storage.count()).toBe(0);
  });

  it('enforces count and byte bounds in the memory fallback', async () => {
    vi.stubGlobal('indexedDB', undefined);
    const countStorage = storageForKey(PROJECT_A_KEY);
    const countEvents = Array.from(
      { length: MAX_OFFLINE_EVENTS_PER_PROJECT + 1 },
      (_, index) => createEvent(`memory_count_${index}`)
    );

    await countStorage.store(countEvents);
    const countRetained = await countStorage.drain();
    expect(countRetained).toHaveLength(MAX_OFFLINE_EVENTS_PER_PROJECT);
    expect(countRetained[0].event).toBe('memory_count_1');

    const byteStorage = storageForKey(PROJECT_B_KEY);
    const payloadBytes = Math.floor(
      MAX_OFFLINE_SERIALIZED_BYTES_PER_PROJECT / 2
    );
    const older = createLargeEvent('memory_older_large', payloadBytes);
    const newer = createLargeEvent('memory_newer_large', payloadBytes);

    await byteStorage.store([older, newer]);

    expect(await byteStorage.drain()).toEqual([newer]);
  });

  it('does not retain events that cannot be serialized for transport', async () => {
    const storage = storageForKey(PROJECT_A_KEY);
    const circularProperties: Record<string, unknown> = {};
    circularProperties.self = circularProperties;
    const serializableEvent = createEvent('serializable_event');

    await storage.store([
      createEvent('circular_event', circularProperties),
      serializableEvent,
    ]);

    expect(await storage.drain()).toEqual([serializableEvent]);
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

    expect(await projectA.drain()).toEqual([]);
    expect(await readRawRecords()).toEqual([]);
  });

  it('discards unowned version 1 records during the database upgrade', async () => {
    await createLegacyDatabase(createEvent('legacy_event'));

    const projectA = storageForKey(PROJECT_A_KEY);

    expect(await projectA.drain()).toEqual([]);
    expect(await readRawRecords()).toEqual([]);
  });
});
