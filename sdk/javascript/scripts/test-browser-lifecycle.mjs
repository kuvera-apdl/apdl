import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { access, mkdtemp, readFile, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CLIENT_KEY = 'client_apdl_0123456789abcdef';
const KEEPALIVE_BUDGET_BYTES = 48 * 1024;
const bundle = await readFile(
  new URL('../dist/apdl.iife.js', import.meta.url),
  'utf8'
);

class CdpClient {
  constructor(input, output) {
    this.input = input;
    this.output = output;
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = '';
    output.on('data', (chunk) => {
      this.buffer += chunk.toString('utf8');
      let boundary = this.buffer.indexOf('\0');
      while (boundary >= 0) {
        const frame = this.buffer.slice(0, boundary);
        this.buffer = this.buffer.slice(boundary + 1);
        if (frame.length > 0) {
          this.receive(JSON.parse(frame));
        }
        boundary = this.buffer.indexOf('\0');
      }
    });
  }

  receive(message) {
    if (message.id === undefined) return;
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    if (message.error) {
      pending.reject(new Error(message.error.message));
    } else {
      pending.resolve(message.result);
    }
  }

  send(method, params = {}, sessionId) {
    const id = this.nextId;
    this.nextId += 1;
    const response = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.input.write(`${JSON.stringify({
      id,
      method,
      params,
      ...(sessionId ? { sessionId } : {}),
    })}\0`);
    return response;
  }

  session(sessionId) {
    return {
      send: (method, params = {}) => this.send(method, params, sessionId),
    };
  }

  close() {
    this.input.end();
  }
}

let chrome;
let cdp;
let page;
let server;
let profileDirectory;
let heldResponse;
const requests = [];

try {
  const requestWaiter = deferred();
  server = createServer((request, response) => {
    const url = new URL(request.url ?? '/', 'http://127.0.0.1');
    if (request.method === 'POST' && url.pathname === '/v1/events') {
      const chunks = [];
      request.on('data', (chunk) => chunks.push(chunk));
      request.on('end', () => {
        const body = Buffer.concat(chunks);
        const recorded = {
          body,
          headers: request.headers,
          payload: JSON.parse(body.toString('utf8')),
        };
        requests.push(recorded);
        requestWaiter.resolve(requests.length);

        if (requests.length === 1) {
          heldResponse = response;
          return;
        }
        response.writeHead(requests.length === 2 ? 503 : 202).end();
      });
      return;
    }

    if (url.pathname === '/apdl.iife.js') {
      response.writeHead(200, { 'Content-Type': 'text/javascript' }).end(bundle);
      return;
    }
    if (url.pathname === '/next') {
      response.writeHead(200, { 'Content-Type': 'text/html' }).end(
        `<!doctype html>
        <title>APDL lifecycle recovery</title>
        <script src="/apdl.iife.js"></script>
        <script>
          window.__apdlLifecycleError = null;
          try {
            window.__apdlRecoveryClient = new APDL.APDLClient({
              endpoint: location.origin,
              auth: { clientKey: ${JSON.stringify(CLIENT_KEY)} },
              autoCapture: false,
              batchSize: 100,
              flushInterval: 600000,
              persistence: 'localStorage',
              consent: {
                analytics: true,
                personalization: false,
                experiments: false
              }
            });
          } catch (error) {
            window.__apdlLifecycleError = String(error?.stack ?? error);
          }
        </script>`
      );
      return;
    }

    response.writeHead(200, { 'Content-Type': 'text/html' }).end(
      `<!doctype html>
      <title>APDL lifecycle test</title>
      <script src="/apdl.iife.js"></script>
      <script>
        window.__apdlLifecycleError = null;
        try {
          const client = new APDL.APDLClient({
            endpoint: location.origin,
            auth: { clientKey: ${JSON.stringify(CLIENT_KEY)} },
            autoCapture: false,
            batchSize: 100,
            flushInterval: 600000,
            persistence: 'localStorage',
            consent: {
              analytics: true,
              personalization: false,
              experiments: false
            }
          });
          for (let index = 0; index < 3; index += 1) {
            client.track('browser_navigation_takeover_' + index, {
              lifecycle: 'pagehide',
              value: { payload: 'x'.repeat(20000) }
            });
          }
          window.__apdlNormalDrain = client.debug.flush();
        } catch (error) {
          window.__apdlLifecycleError = String(error?.stack ?? error);
        }
      </script>`
    );
  });
  const origin = await listen(server);

  profileDirectory = await mkdtemp(join(tmpdir(), 'apdl-chrome-'));
  const chromePath = await findChrome();
  const stderr = [];
  chrome = spawn(chromePath, [
    '--headless=new',
    '--disable-background-networking',
    '--disable-component-update',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--no-default-browser-check',
    '--no-first-run',
    '--no-sandbox',
    '--remote-debugging-pipe',
    `--user-data-dir=${profileDirectory}`,
    'about:blank',
  ], {
    stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe'],
  });
  chrome.stderr.on('data', (chunk) => {
    if (stderr.length < 20) stderr.push(chunk.toString());
  });

  cdp = new CdpClient(chrome.stdio[3], chrome.stdio[4]);
  try {
    await withTimeout(
      cdp.send('Browser.getVersion'),
      10_000,
      'timed out waiting for Chrome DevTools'
    );
  } catch (error) {
    throw new Error(`${error.message}\n${stderr.join('')}`);
  }
  const { targetId } = await cdp.send('Target.createTarget', {
    url: `${origin}/`,
  });
  const { sessionId } = await cdp.send('Target.attachToTarget', {
    targetId,
    flatten: true,
  });
  page = cdp.session(sessionId);
  await page.send('Page.enable');
  await page.send('Runtime.enable');

  await waitForRequestCount(requestWaiter, requests, 1);
  assert.equal(
    await pageError(page),
    null,
    'the SDK test page must initialize without an exception'
  );

  await page.send('Page.navigate', { url: `${origin}/next` });
  await waitForRequestCount(requestWaiter, requests, 2);

  assert.equal(
    requests[1].headers['x-api-key'],
    CLIENT_KEY,
    'the unload-safe request must preserve header authentication'
  );
  assert.ok(
    requests[1].body.byteLength <= KEEPALIVE_BUDGET_BYTES,
    `keepalive body exceeded ${KEEPALIVE_BUDGET_BYTES} bytes`
  );
  assert.equal(requests[0].payload.events.length, 3);
  assert.equal(requests[1].payload.events.length, 2);
  assert.deepEqual(
    requests[1].payload.events.map((event) => event.message_id),
    requests[0].payload.events.slice(0, 2).map((event) => event.message_id),
    'navigation takeover must retain the original event identities'
  );
  assert.equal(
    requests[1].payload.events[0].event,
    'browser_navigation_takeover_0'
  );

  await waitForPagePath(page, '/next');
  assert.equal(
    await pageError(page),
    null,
    'the reopened SDK page must initialize without an exception'
  );
  await waitForRequestCount(requestWaiter, requests, 3);
  assert.deepEqual(
    requests[2].payload.events.map((event) => event.message_id),
    requests[0].payload.events.map((event) => event.message_id),
    'the reopened client must recover the failed keepalive batch and overflow'
  );
  assert.equal(
    requests[2].headers.referer,
    `${origin}/next`,
    'the recovery request must originate from the reopened document'
  );
  await waitForOfflineEventCount(page, 0);

  // Let beforeunload/pagehide/visibilitychange and all response microtasks
  // quiesce, then prove they emitted one lifecycle request in total.
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(requests.length, 3);
  const originalDocumentRequests = requests.filter(
    (request) => request.headers.referer === `${origin}/`
  );
  assert.equal(
    originalDocumentRequests.length,
    2,
    'the original normal request must be followed by exactly one lifecycle request'
  );
  const offlineEvents = await readOfflineEvents(page);
  assert.deepEqual(
    offlineEvents,
    [],
    'accepted recovery must acknowledge every durable lifecycle record'
  );

  console.log(
    '✓ H-04 failed keepalive is issued once and fully recovered after navigation'
  );
} finally {
  if (heldResponse && !heldResponse.destroyed) {
    heldResponse.writeHead(503).end();
  }
  cdp?.close();
  await stopProcess(chrome);
  await closeServer(server);
  if (profileDirectory) {
    await rm(profileDirectory, { recursive: true, force: true });
  }
}

function deferred() {
  let resolve;
  const promise = new Promise((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

async function listen(httpServer) {
  await new Promise((resolve, reject) => {
    httpServer.once('error', reject);
    httpServer.listen(0, '127.0.0.1', resolve);
  });
  const address = httpServer.address();
  assert.ok(address && typeof address === 'object');
  return `http://127.0.0.1:${address.port}`;
}

async function waitForRequestCount(waiter, recorded, count) {
  while (recorded.length < count) {
    await withTimeout(
      waiter.promise,
      10_000,
      `timed out waiting for browser request ${count}`
    );
    if (recorded.length < count) {
      waiter.promise = new Promise((resolve) => {
        waiter.resolve = resolve;
      });
    }
  }
}

async function pageError(client) {
  const result = await client.send('Runtime.evaluate', {
    expression: 'window.__apdlLifecycleError',
    returnByValue: true,
  });
  return result.result.value ?? null;
}

async function waitForPagePath(client, expectedPath) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const result = await client.send('Runtime.evaluate', {
        expression: 'location.pathname',
        returnByValue: true,
      });
      if (result.result.value === expectedPath) return;
    } catch {
      // Navigation can replace the execution context between CDP commands.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`timed out waiting for navigation to ${expectedPath}`);
}

async function readOfflineEvents(client) {
  const result = await client.send('Runtime.evaluate', {
    expression: `new Promise((resolve, reject) => {
      const open = indexedDB.open('apdl-offline', 4);
      open.onerror = () => reject(open.error);
      open.onsuccess = () => {
        const transaction = open.result.transaction('events', 'readonly');
        const records = transaction.objectStore('events').getAll();
        records.onerror = () => reject(records.error);
        records.onsuccess = () => resolve(
          records.result.map((record) => record.data)
        );
      };
    })`,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text);
  }
  return result.result.value;
}

async function waitForOfflineEventCount(client, expectedCount) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const events = await readOfflineEvents(client);
    if (events.length === expectedCount) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(
    `timed out waiting for ${expectedCount} durable lifecycle event(s)`
  );
}

async function findChrome() {
  const candidates = [
    process.env.CHROME_BIN,
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    process.env.PROGRAMFILES
      ? join(process.env.PROGRAMFILES, 'Google/Chrome/Application/chrome.exe')
      : undefined,
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      await access(candidate);
      return candidate;
    } catch {
      // Try the next supported installation path.
    }
  }
  throw new Error(
    'Google Chrome or Chromium is required (set CHROME_BIN to its executable)'
  );
}

async function withTimeout(promise, milliseconds, message) {
  let timeout;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error(message)), milliseconds);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

async function closeServer(httpServer) {
  if (!httpServer?.listening) return;
  await new Promise((resolve) => httpServer.close(resolve));
}

async function stopProcess(processHandle) {
  if (!processHandle || processHandle.exitCode !== null) return;
  processHandle.kill('SIGTERM');
  try {
    await withTimeout(
      new Promise((resolve) => processHandle.once('exit', resolve)),
      3_000,
      'Chrome did not stop after SIGTERM'
    );
  } catch {
    processHandle.kill('SIGKILL');
  }
}
