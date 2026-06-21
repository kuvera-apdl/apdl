import type { APDLConfig } from '../src/core/config';

export const CLIENT_KEY = 'proj_apdl_0123456789abcdef';
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

export class MockEventSource {
  static instances: MockEventSource[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 0;
  private listeners: Map<string, Set<(ev: MessageEvent) => void>> = new Map();

  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, new Set());
    }
    this.listeners.get(type)!.add(listener as (ev: MessageEvent) => void);
  }

  emit(type: string, data: string) {
    const event = new MessageEvent(type, { data });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }

  close() {
    this.readyState = 2;
  }
}
