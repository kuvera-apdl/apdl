import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { IDBFactory } from 'fake-indexeddb';
import { JSDOM } from 'jsdom';

const CLIENT_KEY = 'client_apdl_0123456789abcdef';
const FIRST_ENDPOINT = 'https://api.first.test';
const SECOND_ENDPOINT = 'https://api.second.test';
const bundle = await readFile(new URL('../dist/apdl.iife.js', import.meta.url), 'utf8');

await run(
  'RA-01 built bundle makes explicit denial authoritative and rejects legacy consent',
  async () => {
    const harness = createHarness();
    const { window } = harness;
    const scopedKey = storageKey('consent', FIRST_ENDPOINT);
    const grant = {
      analytics: true,
      personalization: true,
      experiments: true,
    };
    window.localStorage.setItem(scopedKey, JSON.stringify(grant));
    window.localStorage.setItem('apdl_consent_apdl', JSON.stringify(grant));

    const client = new harness.APDLClient({
      endpoint: FIRST_ENDPOINT,
      auth: { clientKey: CLIENT_KEY },
      autoCapture: true,
      persistence: 'localStorage',
      consent: {
        analytics: false,
        personalization: false,
        experiments: false,
      },
    });

    assert.deepEqual(normalize(client.consent.get()), {
      analytics: false,
      personalization: false,
      experiments: false,
    });
    assert.deepEqual(normalize(client.debug.getQueue()), []);
    assert.deepEqual(JSON.parse(window.localStorage.getItem(scopedKey)), {
      analytics: false,
      personalization: false,
      experiments: false,
    });
    assert.equal(window.localStorage.getItem('apdl_consent_apdl'), null);

    await client.shutdown();
    harness.close();
  }
);

await run(
  'RA-01 built bundle isolates all persisted state by deployment and project',
  async () => {
    const harness = createHarness();
    const { window } = harness;
    const consent = {
      analytics: true,
      personalization: true,
      experiments: true,
    };
    const first = new harness.APDLClient({
      endpoint: FIRST_ENDPOINT,
      auth: { clientKey: CLIENT_KEY },
      persistence: 'localStorage',
      consent,
    });
    first.track('first_deployment_event');
    assert.equal(first.debug.getQueue().length, 1);
    await settle(window);

    const secondConfig = {
      endpoint: SECOND_ENDPOINT,
      auth: { clientKey: CLIENT_KEY },
      persistence: 'localStorage',
    };
    const second = new harness.APDLClient(secondConfig);
    await settle(window);

    assert.deepEqual(normalize(second.consent.get()), {
      analytics: false,
      personalization: false,
      experiments: false,
    });
    assert.deepEqual(normalize(second.debug.getQueue()), []);
    second.consent.update(consent);
    second.track('second_deployment_event');
    await settle(window);

    for (const kind of ['anonymous_id', 'consent', 'flags', 'session']) {
      const firstKey = storageKey(kind, FIRST_ENDPOINT);
      const secondKey = storageKey(kind, SECOND_ENDPOINT);
      assert.notEqual(window.localStorage.getItem(firstKey), null, `${kind} missing`);
      assert.notEqual(window.localStorage.getItem(secondKey), null, `${kind} missing`);
      assert.notEqual(firstKey, secondKey);
    }
    assert.notEqual(
      window.localStorage.getItem(storageKey('anonymous_id', FIRST_ENDPOINT)),
      window.localStorage.getItem(storageKey('anonymous_id', SECOND_ENDPOINT))
    );
    assert.notEqual(
      window.localStorage.getItem(storageKey('session', FIRST_ENDPOINT)),
      window.localStorage.getItem(storageKey('session', SECOND_ENDPOINT))
    );
    assert.notEqual(
      window.localStorage.getItem(storageKey('flags', FIRST_ENDPOINT)),
      window.localStorage.getItem(storageKey('flags', SECOND_ENDPOINT))
    );
    assert.equal(first.storage.deploymentOrigin, FIRST_ENDPOINT);
    assert.equal(second.storage.deploymentOrigin, SECOND_ENDPOINT);
    assert.equal(first.storage.projectId, 'apdl');
    assert.equal(second.storage.projectId, 'apdl');

    await Promise.all([first.shutdown(), second.shutdown()]);
    harness.close();
  }
);

function createHarness() {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    runScripts: 'dangerously',
    url: 'https://customer.test/',
  });
  const { window } = dom;
  const indexedDB = new IDBFactory();

  Object.defineProperty(window, 'indexedDB', {
    configurable: true,
    value: indexedDB,
  });
  for (const name of [
    'Headers',
    'ReadableStream',
    'Request',
    'Response',
    'TextDecoder',
    'TextEncoder',
  ]) {
    if (!(name in window) && name in globalThis) {
      Object.defineProperty(window, name, {
        configurable: true,
        value: globalThis[name],
      });
    }
  }

  window.fetch = async (input, init = {}) => {
    const url = String(input);
    if (url.endsWith('/v1/events')) {
      return new window.Response(null, { status: 202 });
    }
    if (url.endsWith('/v1/flags')) {
      const flagKey = url.startsWith(FIRST_ENDPOINT) ? 'first-flag' : 'second-flag';
      return new window.Response(JSON.stringify({
        schema_version: 2,
        project_id: 'apdl',
        flags: [makeFlag(flagKey)],
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.endsWith('/v1/stream')) {
      const body = new window.ReadableStream({
        start(controller) {
          init.signal?.addEventListener('abort', () => {
            controller.error(new window.DOMException('Aborted', 'AbortError'));
          }, { once: true });
        },
      });
      return new window.Response(body, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      });
    }
    throw new Error(`Unexpected built-browser request: ${url}`);
  };

  window.eval(bundle);
  assert.equal(typeof window.APDL?.APDLClient, 'function');
  return {
    APDLClient: window.APDL.APDLClient,
    close: () => dom.window.close(),
    window,
  };
}

function storageKey(kind, endpoint) {
  return `apdl_${kind}_v2_${encodeURIComponent(new URL(endpoint).origin)}_apdl`;
}

function makeFlag(key) {
  return {
    key,
    enabled: true,
    default_variant: 'control',
    variants: [
      { key: 'control', weight: 1 },
      { key: 'treatment', weight: 1 },
    ],
    salt: 'built-browser-salt',
    rules: [],
    fallthrough: {
      rollout: { percentage: 100, bucket_by: 'user_id' },
    },
    version: 1,
  };
}

function normalize(value) {
  return JSON.parse(JSON.stringify(value));
}

async function settle(window) {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
  }
  await new Promise((resolve) => window.setTimeout(resolve, 0));
}

async function run(name, test) {
  try {
    await test();
    console.log(`ok - ${name}`);
  } catch (error) {
    console.error(`not ok - ${name}`);
    throw error;
  }
}
