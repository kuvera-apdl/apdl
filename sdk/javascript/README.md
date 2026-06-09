# @apdl/sdk

Browser TypeScript SDK for APDL. The SDK sends product analytics events to the ingestion service, evaluates feature flags client-side, receives real-time configuration updates from the config service, and exposes experiment context for flag targeting.

## Installation

```bash
npm install @apdl/sdk
```

## Initialization

```typescript
import { APDL } from '@apdl/sdk';

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

The SDK derives the project ID from the client key internally. Do not pass `projectId`, `apiKey`, `host`, or `configHost`; those fields are not part of the public config contract.

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

When running the local APDL services, initialize the SDK with the local ingestion and config ports:

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

The SDK fetches initial flag configuration from `endpoints.config` at `/v1/flags` and listens for real-time updates on `/v1/stream`.

## Experiment Context

Use the `experiments` namespace to provide stable targeting attributes for flag evaluation:

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

Experiment context must use the canonical shape `{ attributes: Record<string, unknown> }`. These attributes are merged into the feature flag evaluation context and may be included in feature flag exposure event metadata.

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

`npm run release:check` runs linting, tests, build, and an npm package dry run.
