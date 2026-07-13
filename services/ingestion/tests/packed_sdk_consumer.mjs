import { writeFile } from 'node:fs/promises';

import { APDLClient } from '@apdl-oss/sdk';

const outputPath = process.env.APDL_CAPTURE_PATH;
if (!outputPath) {
  throw new Error('APDL_CAPTURE_PATH is required');
}

let capturedPayload;
globalThis.fetch = async (input, init = {}) => {
  const url = String(input);
  if (url.endsWith('/v1/events') && init.method === 'POST') {
    capturedPayload = JSON.parse(String(init.body));
    return new Response('{}', { status: 202 });
  }
  return new Response(
    JSON.stringify({ schema_version: 2, project_id: 'contract', flags: [] }),
    { status: 200, headers: { 'Content-Type': 'application/json' } }
  );
};

const client = new APDLClient({
  endpoint: 'https://contract.invalid',
  auth: { clientKey: 'proj_contract_0123456789abcdef' },
  autoCapture: false,
  batchSize: 20,
  flushInterval: 60_000,
  persistence: 'memory',
  consent: { analytics: true, personalization: true, experiments: true },
});

client.identify('user-42', { plan: 'pro' });
client.group('account-7', { name: 'Acme' });
client.page('Pricing');
client.track('order_completed', { total: 42 });
await client.debug.flush();
await client.shutdown();

if (!capturedPayload) {
  throw new Error('packed SDK did not send an event payload');
}
await writeFile(outputPath, JSON.stringify(capturedPayload), 'utf8');
