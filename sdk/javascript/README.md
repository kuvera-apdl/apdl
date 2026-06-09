# @apdl/sdk (JavaScript / TypeScript)

Browser client for the **Autonomous Product Development Loop** platform.
Capture analytics events automatically, evaluate feature gates locally, and
render server-driven UI — with the same FNV-1a bucketing as the Python SDK and
the config service, so a user buckets identically no matter where a gate is
evaluated.

- 🪄 Auto-capture: page views, clicks, form submissions, scroll depth, rage
  clicks, frontend errors, web vitals
- 🚩 Local feature gate evaluation (no network round-trip on the hot path)
- 🔁 Real-time flag updates over SSE, with a persisted local flag cache
- 🧩 Server-driven UI components (banner, modal, toast, …) plus custom
  registrations
- 🔒 Privacy controls: consent management, PII scrubbing, cookieless mode
- 📦 Ships ESM, CJS, and an IIFE browser bundle, with full TypeScript types

## Install

```bash
npm install @apdl/sdk
```

Or drop the IIFE bundle into any page (exposes a global `APDL`):

```html
<script src="https://unpkg.com/@apdl/sdk/dist/apdl.iife.js"></script>
```

## Quick start

```typescript
import { APDL } from '@apdl/sdk';

const apdl = APDL.init({
  apiKey: 'proj_demo_0123456789abcdef',
  autoCapture: true,          // clicks, page views, forms, scroll depth, …
  privacyMode: 'standard',    // 'standard' | 'cookieless' | 'strict'
});

// Manual event tracking
apdl.track('purchase_completed', { product_id: 'sku-123', revenue: 49.99 });

// Identity
apdl.identify('user-42', { email: 'user@example.com', plan: 'pro' });
apdl.group('org-7', { name: 'Acme' });
apdl.page('/checkout');

// Feature gates (evaluated locally)
if (apdl.checkGate('new-checkout-flow')) {
  // Show the gated experience.
}

// Clean up (flushes pending events)
await apdl.shutdown();
```

## Configuration

All fields except `apiKey` are optional:

```typescript
const apdl = APDL.init({
  apiKey: 'proj_demo_0123456789abcdef',
  host: 'https://ingest.apdl.dev',        // event ingestion endpoint
  configHost: 'https://config.apdl.dev',  // flag config + SSE endpoint
  autoCapture: {                          // true | false | per-feature object
    pageViews: true,
    clicks: true,
    formSubmissions: true,
    inputChanges: false,
    scrollDepth: true,
    rage_clicks: true,
    frontend_errors: true,
    web_vitals: true,
  },
  batchSize: 20,                          // 1..100 events per request
  flushInterval: 3000,                    // ms between background flushes
  maxQueueSize: 1000,                     // oldest events dropped past this
  privacyMode: 'standard',                // 'standard' | 'cookieless' | 'strict'
  persistence: 'localStorage',            // 'localStorage' | 'cookie' | 'memory'
  consent: { analytics: true, personalization: true, experiments: true },
  debug: false,
});
```

For local development against `make dev-all`, point the SDK at your stack:

```typescript
const apdl = APDL.init({
  apiKey: 'proj_demo_0123456789abcdef',
  host: 'http://localhost:8080',
  configHost: 'http://localhost:8081',
});
```

API keys follow `proj_{project_id}_{secret}` — the secret must be 16+
alphanumeric characters or ingestion rejects the request.

## Feature gates

`checkGate` returns a `boolean`; `checkGateDetails` returns a fully-explained
result:

```typescript
const result = apdl.checkGateDetails('new-checkout-flow');
console.log(result.value, result.reason, result.rule_id, result.bucket);
```

Gate checks automatically emit a deduplicated `$feature_flag_exposure` event.

React to real-time config changes pushed over SSE:

```typescript
const unsubscribe = apdl.onFlagChange('new-checkout-flow', (value) => {
  rerenderCheckout(value);
});
// later: unsubscribe();
```

## Privacy & consent

```typescript
// Inspect or update consent at runtime (e.g. from a cookie banner)
apdl.consent.get();
apdl.consent.update({ analytics: false });
apdl.consent.onUpdate((state) => console.log('consent changed', state));

// Register custom PII scrubbers applied to every outgoing event
apdl.privacy.addScrubber((event) => {
  delete event.properties?.ssn;
  return event;
});
```

`privacyMode: 'cookieless'` derives a daily-rotating anonymous ID with no
client-side persistence; `'strict'` additionally enables aggressive scrubbing.

## Server-driven UI

Agents and the config service can push UI configurations (banners, modals,
toasts, …) rendered into page slots:

```typescript
// Register a custom component
apdl.ui.register({
  type: 'countdown-banner',
  schema: { properties: { deadline: { type: 'string' } } },
  render: (props, ctx) => { /* return an HTMLElement */ },
});

// React when the SDK discovers a UI slot on the page
apdl.ui.onSlotUpdate((slotId, element) => { /* ... */ });
```

## Debugging

```typescript
apdl.debug.enable();          // verbose console logging
apdl.debug.getQueue();        // inspect queued events
await apdl.debug.flush();     // force a flush
```

## Development

```bash
cd sdk/javascript
npm install
npm test               # Vitest (jsdom)
npm run typecheck      # strict tsc, the lint gate
npm run build          # Rollup → dist/ (ESM, CJS, IIFE)
```

Tests live in `__tests__/**/*.test.ts`. The flag-evaluation suite pins golden
hash values from the canonical config-service implementation, guaranteeing
this SDK buckets identically to the server and the Python SDK.

## License

MIT
