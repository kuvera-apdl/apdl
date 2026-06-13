# @apdl-oss/sdk

Browser TypeScript SDK for the **Autonomous Product Development Loop** platform.
The SDK sends product analytics events to the ingestion service, evaluates
feature flag variants client-side, receives real-time configuration updates from
the config service over SSE, renders server-driven UI, and exposes experiment
context for flag targeting. It uses the same FNV-1a bucketing as the Python SDK
and the config service, so a user buckets identically no matter where a flag is
evaluated.

- 🪄 Auto-capture: page views, clicks, form submissions, scroll depth, rage
  clicks, frontend errors, web vitals
- 🚩 Local feature flag variant evaluation (no network round-trip on the hot path)
- 🔁 Real-time flag updates over SSE, with a persisted local flag cache
- 🧩 Server-driven UI components (banner, modal, toast, …) plus custom registrations
- 🔒 Privacy controls: consent management, PII scrubbing, cookieless mode
- 📦 Ships ESM, CJS, and an IIFE browser bundle, with full TypeScript types

## Installation

```bash
npm install @apdl-oss/sdk
```

Or drop the IIFE bundle into any page (exposes a global `APDL`):

```html
<script src="https://unpkg.com/@apdl-oss/sdk/dist/apdl.iife.js"></script>
```

## Initialization

```typescript
import { APDL } from '@apdl-oss/sdk';

const apdl = APDL.init({
  endpoints: {
    ingestion: 'https://ingestion.example.com',
    config: 'https://config.example.com',
  },
  auth: {
    clientKey: 'proj_apdl_0123456789abcdef',
  },
  autoCapture: true,
  privacyMode: 'standard',
});
```

`APDL.init(config)` is the primary public entrypoint and returns an `APDLClient`.

## Required Config Fields

The SDK uses one strict initialization contract:

| Field | Required | Description |
|---|---:|---|
| `endpoints.ingestion` | Yes | Base URL for event ingestion. The SDK posts batches to `/v1/events`. |
| `endpoints.config` | Yes | Base URL for feature flags, SSE updates, and UI configuration. The SDK reads `/v1/flags` and `/v1/stream`. |
| `auth.clientKey` | Yes | Browser-safe APDL client key used for service authentication and project identification. |

`auth.clientKey` must use the canonical APDL client key format:

```text
proj_{project_id}_{secret}
```

The secret must be 16+ alphanumeric characters. The SDK derives the project ID
from the client key internally. Do not pass `projectId`, `apiKey`, `host`, or
`configHost`; those fields are not part of the public config contract and the
SDK rejects them.

Optional fields include:

| Field | Description |
|---|---|
| `autoCapture` | `true`, `false`, or a per-signal capture config. |
| `batchSize` | Number of events to send per batch. |
| `flushInterval` | Queue flush interval in milliseconds. |
| `privacyMode` | `'standard'`, `'cookieless'`, or `'strict'`. |
| `consent` | Initial consent state for `analytics`, `personalization`, and `experiments`. |
| `persistence` | `'localStorage'`, `'cookie'`, or `'memory'`. |
| `maxQueueSize` | Maximum queued events before dropping the oldest event. |
| `debug` | Enables SDK diagnostics when `true`. |

## Local Development Endpoints

When running the local APDL services, initialize the SDK with the local
ingestion and config ports:

```typescript
const apdl = APDL.init({
  endpoints: {
    ingestion: 'http://localhost:8080',
    config: 'http://localhost:8081',
  },
  auth: {
    clientKey: 'proj_apdl_0123456789abcdef',
  },
  autoCapture: true,
  privacyMode: 'standard',
});
```

Start the local services from the repository root:

```bash
make run-ingestion
make run-config
```

## Event Tracking

```typescript
apdl.track('purchase_completed', {
  product_id: 'sku-123',
  revenue: 49.99,
});

apdl.page('Pricing', {
  path: '/pricing',
});
```

Events are batched and sent to `endpoints.ingestion` at `/v1/events`.

## User Identification

```typescript
apdl.identify('user-42', {
  email: 'user@example.com',
  plan: 'pro',
});

apdl.group('account-7', {
  tier: 'enterprise',
});

apdl.reset();
```

Identified user traits participate in feature flag evaluation.

## Feature Flags

```typescript
const variant = apdl.getVariant('new-checkout-flow');

if (variant === 'treatment') {
  renderTreatmentCheckout();
}
```

For diagnostics, use `getVariantDetails`:

```typescript
const result = apdl.getVariantDetails('new-checkout-flow', {
  page: '/checkout',
  component: 'checkout-form',
});

console.log(result.variant, result.reason);
```

Flag evaluation automatically emits a deduplicated `$feature_flag_exposure`
event. The SDK fetches initial flag configuration from `endpoints.config` at
`/v1/flags` and listens for real-time updates on `/v1/stream`.

React to real-time variant changes pushed over SSE:

```typescript
const unsubscribe = apdl.onVariantChange('new-checkout-flow', (variant) => {
  rerenderCheckout(variant);
});
// later: unsubscribe();
```

## Experiment Context

Use the `experiments` namespace to provide stable targeting attributes for flag
evaluation:

```typescript
apdl.experiments.setContext({
  attributes: {
    plan: 'pro',
    region: 'us',
  },
});

const context = apdl.experiments.getContext();

apdl.experiments.clearContext();
```

Experiment context must use the canonical shape
`{ attributes: Record<string, unknown> }`. These attributes are merged into the
feature flag evaluation context and may be included in feature flag exposure
event metadata.

## Privacy & Consent

```typescript
// Inspect or update consent at runtime (e.g. from a cookie banner)
apdl.consent.get();
apdl.consent.update({ analytics: false });
apdl.consent.onUpdate((state) => console.log('consent changed', state));

// Register or remove custom PII scrubbers applied to every outgoing event
const scrubSsn = (event) => {
  delete event.properties?.ssn;
  return event;
};
apdl.privacy.addScrubber(scrubSsn);
apdl.privacy.removeScrubber(scrubSsn);
```

`privacyMode: 'cookieless'` derives a daily-rotating anonymous ID with no
client-side persistence; `'strict'` additionally enables aggressive scrubbing.

## Server-Driven UI

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

## Debugging & Shutdown

```typescript
apdl.debug.enable();          // verbose console logging
apdl.debug.getQueue();        // inspect queued events
await apdl.debug.flush();     // force a flush

await apdl.shutdown();        // flush remaining events and tear down
```

## SDK Development

Run SDK commands from `sdk/javascript`:

```bash
npm run setup
npm test
npm run lint
npm run build
npm run release:check
```

Or use the repository-level make targets:

```bash
make setup-sdk
make test-sdk
make lint-sdk
make build-sdk
make release-sdk
```

`npm run lint` runs the strict `tsc` typecheck (the lint gate), `npm run build`
produces the ESM, CJS, and IIFE bundles in `dist/`, and `npm run release:check`
runs linting, tests, build, and an npm package dry run. Tests live in
`__tests__/**/*.test.ts`; the flag-evaluation suite pins golden hash values from
the canonical config-service implementation, guaranteeing this SDK buckets
identically to the server and the Python SDK.

## License

MIT
