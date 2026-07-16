import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { API_KEY_HEADER, SDK_IDENTIFIER, SDK_IDENTIFIER_HEADER } from '../../src/core/constants';
import { SSEConnection } from '../../src/sse/connection';
import {
  CLIENT_KEY,
  ENDPOINT,
  MockEventSource,
  mockApiFetch,
} from '../helpers';

describe('SSEConnection', () => {
  let connection: SSEConnection;
  const fetchMock = vi.fn(mockApiFetch);

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    fetchMock.mockImplementation(mockApiFetch);
    MockEventSource.instances = [];
    vi.stubGlobal('fetch', fetchMock);
    connection = new SSEConnection(`${ENDPOINT}/v1/stream`, CLIENT_KEY);
  });

  afterEach(() => {
    connection.disconnect();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('keeps the credential out of the URL and authenticates with headers', async () => {
    connection.connect();
    await flushAsync();

    const source = MockEventSource.instances[0];
    expect(source.url).toBe(`${ENDPOINT}/v1/stream`);
    expect(source.url).not.toContain(CLIENT_KEY);
    expect(source.init.headers).toMatchObject({
      Accept: 'text/event-stream',
      [API_KEY_HEADER]: CLIENT_KEY,
      [SDK_IDENTIFIER_HEADER]: SDK_IDENTIFIER,
    });
  });

  it('parses typed, multi-line messages and records their event ID', async () => {
    const callback = vi.fn();
    connection.onMessage(callback);
    connection.connect();
    await flushAsync();

    const source = MockEventSource.instances[0];
    source.emitRaw('id: cursor-1\nevent: flag_update\ndata: first\n');
    source.emitRaw('data: second\n\n');
    await flushAsync();

    expect(callback).toHaveBeenCalledWith({
      type: 'flag_update',
      data: 'first\nsecond',
      id: 'cursor-1',
    });
  });

  it('reconnects with Last-Event-ID after a stream failure', async () => {
    connection.connect();
    await flushAsync();
    const first = MockEventSource.instances[0];
    first.emit('message', '{}', 'cursor-42');
    await flushAsync();
    first.fail(new Error('network failed'));
    await flushAsync();

    await vi.advanceTimersByTimeAsync(999);
    expect(MockEventSource.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await flushAsync();

    expect(MockEventSource.instances).toHaveLength(2);
    expect(MockEventSource.instances[1].init.headers).toMatchObject({
      'Last-Event-ID': 'cursor-42',
      [API_KEY_HEADER]: CLIENT_KEY,
    });
  });

  it('reconnects after a terminal slow-consumer event and receives a new snapshot', async () => {
    const callback = vi.fn();
    connection.onMessage(callback);
    connection.connect();
    await flushAsync();
    const first = MockEventSource.instances[0];

    first.emit('config', '{"flags":[]}', '7');
    first.emit('stream_error', '{"reason":"slow_consumer","snapshot_required":true}');
    first.close();
    await flushAsync();

    await vi.advanceTimersByTimeAsync(1000);
    await flushAsync();
    const reconnected = MockEventSource.instances[1];
    reconnected.emit('config', '{"flags":[{"key":"latest"}]}', '9');
    await flushAsync();

    expect(reconnected.init.headers).toMatchObject({ 'Last-Event-ID': '7' });
    expect(callback).toHaveBeenLastCalledWith({
      type: 'config',
      data: '{"flags":[{"key":"latest"}]}',
      id: '9',
    });
  });

  it('clamps a server retry of zero to prevent a reconnect spin', async () => {
    connection.connect();
    await flushAsync();
    const first = MockEventSource.instances[0];
    first.emitRaw('retry: 0\ndata: heartbeat\n\n');
    await flushAsync();
    first.fail(new Error('network failed'));
    await flushAsync();

    await vi.advanceTimersByTimeAsync(999);
    expect(MockEventSource.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await flushAsync();
    expect(MockEventSource.instances).toHaveLength(2);
  });

  it('aborts the active fetch and suppresses reconnect after disconnect', async () => {
    connection.connect();
    await flushAsync();
    const source = MockEventSource.instances[0];
    const signal = source.init.signal;

    connection.disconnect();

    expect(signal?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(MockEventSource.instances).toHaveLength(1);
    expect(connection.isConnected()).toBe(false);
  });
});

async function flushAsync(): Promise<void> {
  for (let index = 0; index < 6; index += 1) {
    await Promise.resolve();
  }
}
