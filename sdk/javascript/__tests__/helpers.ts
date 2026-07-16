import type { APDLConfig } from '../src/core/config';

export const CLIENT_KEY = 'client_apdl_0123456789abcdef';
export const ENDPOINT = 'https://api.test.dev';

export type TestConfigOverrides = Partial<Omit<APDLConfig, 'auth'>> & {
  auth?: Partial<APDLConfig['auth']>;
};

export function createTestConfig(overrides: TestConfigOverrides = {}): APDLConfig {
  const { auth, ...rest } = overrides;

  return {
    endpoint: ENDPOINT,
    auth: {
      clientKey: CLIENT_KEY,
      ...auth,
    },
    autoCapture: false,
    persistence: 'memory',
    ...rest,
  };
}

/** Successful /v1/flags response with no flags, for stubbing fetch. */
export function emptyFlagsResponse() {
  return {
    ok: true,
    json: () =>
      Promise.resolve({
        schema_version: 2,
        project_id: 'apdl',
        flags: [],
      }),
    status: 200,
    headers: new Headers(),
  };
}

/** Controlled fetch response used by tests for the header-authenticated SSE stream. */
export class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly response: Response;
  readonly onopen = (): void => {};
  readonly onmessage: (event: MessageEvent) => void;
  readonly onerror = (): void => this.fail(new Error('mock SSE failure'));
  readyState = 1;
  private streamController!: ReadableStreamDefaultController<Uint8Array>;
  private readonly encoder = new TextEncoder();

  constructor(
    public readonly url: string,
    public readonly init: RequestInit
  ) {
    const body = new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.streamController = controller;
      },
    });
    this.response = {
      ok: true,
      status: 200,
      headers: new Headers({ 'Content-Type': 'text/event-stream' }),
      body,
    } as Response;
    this.onmessage = (event) => {
      this.emit('message', String(event.data), event.lastEventId || undefined);
    };
    init.signal?.addEventListener('abort', () => this.fail(
      new DOMException('Aborted', 'AbortError')
    ), { once: true });
    MockEventSource.instances.push(this);
  }

  emit(type: string, data: string, id?: string): void {
    const message = [
      ...(id === undefined ? [] : [`id: ${id}`]),
      ...(type === 'message' ? [] : [`event: ${type}`]),
      ...data.split('\n').map((line) => `data: ${line}`),
      '',
      '',
    ].join('\n');
    this.streamController.enqueue(this.encoder.encode(message));
  }

  emitRaw(value: string): void {
    this.streamController.enqueue(this.encoder.encode(value));
  }

  close(): void {
    if (this.readyState === 2) return;
    this.readyState = 2;
    this.streamController.close();
  }

  fail(error: Error): void {
    if (this.readyState === 2) return;
    this.readyState = 2;
    this.streamController.error(error);
  }
}

/** Default fetch implementation for SDK tests. */
export function mockApiFetch(
  input: RequestInfo | URL,
  init: RequestInit = {}
): Promise<unknown> {
  const url = String(input);
  if (url.endsWith('/v1/stream')) {
    return Promise.resolve(new MockEventSource(url, init).response);
  }
  if (url.endsWith('/v1/events')) {
    return Promise.resolve({
      ok: true,
      status: 202,
      headers: new Headers(),
    } as Response);
  }
  return Promise.resolve(emptyFlagsResponse());
}
