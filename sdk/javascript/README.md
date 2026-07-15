# @apdl-oss/sdk

Browser TypeScript SDK for the **Autonomous Product Development Loop** platform.
The SDK sends product analytics events to the ingestion service, evaluates
feature flag variants client-side, receives real-time configuration updates from
the config service over SSE, provides a local UI renderer, and exposes
experiment context for flag targeting. It uses the same FNV-1a bucketing as the
Python SDK and the config service, so a user buckets identically no matter where
a flag is evaluated.

- 🪄 Auto-capture: page views, clicks, form submissions, scroll depth, rage
  clicks, frontend errors, web vitals
- 🚩 Local feature flag variant evaluation (no network round-trip on the hot path)
- 🔁 Real-time flag updates over SSE, with a persisted local flag cache
- 🧩 Local UI component renderer (backend UI-config delivery is not in 0.3.0)
- 🔒 Privacy controls: consent management, PII scrubbing, cookieless mode
- ⚛️ First-party React/Next adapter (`@apdl-oss/sdk/react`) — a provider + hook, no wrapper boilerplate
- 🧯 Zero-config setup: env conventions, SSR-safe init, idempotent singleton, fail-soft validation
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
  endpoint: 'https://api.example.com',
  auth: {
    clientKey: 'client_demo_0123456789abcdef',
  },
  autoCapture: true,
  privacyMode: 'standard',
});
```

`APDL.init(config)` (also exported as the bare `init(config)`) is the primary
public entrypoint. It is:

- **SSR-safe** — on the server (no `window`) it returns an inert no-op client
  and opens no sockets, timers, or fetches, so it is safe to call at module
  scope in frameworks like Next.js.
- **An idempotent singleton** — repeated calls with the same `clientKey` return
  the same client, so it is immune to React StrictMode double-invoke and HMR
  re-runs (no duplicate listeners, SSE connections, or flush loops). The
  instance is evicted on `shutdown()`, so a later `init()` starts fresh.
- **Fail-soft** — when `endpoint`/`clientKey` are absent it warns once and
  returns a no-op client instead of throwing, so an unset env var does not crash
  every route. Malformed values (bad key format, removed fields) still throw.

### Zero-config setup (env conventions)

If `endpoint` / `auth.clientKey` are omitted, they are read from environment
variables, so `init()` can be called with no arguments:

| Field | Browser (bundler-inlined) | Server |
|---|---|---|
| endpoint | `NEXT_PUBLIC_APDL_URL` | `APDL_URL` |
| clientKey | `NEXT_PUBLIC_APDL_CLIENT_KEY` | `APDL_CLIENT_KEY` |

For module-scope use without any `useEffect`, import the lazy `apdl` singleton.
It no-ops on the server and auto-starts on the first browser tick, reading config
from the env conventions above:

```typescript
import { apdl } from '@apdl-oss/sdk'; // no 'use client', no useEffect

apdl.track('cta_clicked', { id: 'hero' });
const variant = apdl.getVariant('new-checkout-flow');
```

## React & Next.js

Install the package and drop the provider in once — it owns the `'use client'`
boundary, the singleton lifecycle, and SSR safety internally:

```tsx
// app/layout.tsx — the entire integration
import { APDLProvider } from '@apdl-oss/sdk/react';

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <APDLProvider autoCapture>{children}</APDLProvider>;
}
```

With `NEXT_PUBLIC_APDL_URL` / `NEXT_PUBLIC_APDL_CLIENT_KEY` set, the example above
is a complete setup. You can also pass props explicitly
(`<APDLProvider endpoint={...} clientKey={...} autoCapture>`).

Read the client anywhere with the `useAPDL` hook — no instance threading:

```tsx
import { useAPDL } from '@apdl-oss/sdk/react';

function HeroCTA() {
  const apdl = useAPDL();
  const variant = apdl.getVariant('new-checkout-flow');
  return <button onClick={() => apdl.track('cta_clicked', { id: 'hero' })}>Buy</button>;
}
```

`react` (>= 18) is an optional peer dependency, required only when importing
`@apdl-oss/sdk/react`. Outside a provider, `useAPDL()` returns an inert no-op
client, so calls never throw.

## Config Fields

The SDK uses one initialization contract:

| Field | Required | Description |
|---|---:|---|
| `endpoint` | Yes¹ | Absolute HTTP(S) origin of the APDL gateway, with no credentials, path, query, or fragment. The SDK posts events to `/v1/events` and reads flags + SSE from `/v1/flags` and `/v1/stream` on this one origin. |
| `auth.clientKey` | Yes¹ | Browser-safe APDL client key used for service authentication and project identification. |

¹ Resolved from the env conventions above when omitted. If still absent, `init()`
returns a no-op client (fail-soft); `new APDLClient(config)` and
`resolveConfig(config, { strict: true })` throw.

`auth.clientKey` must use the canonical APDL client key format:

```text
client_{project_id}_{token}
```

The token must be 16+ alphanumeric characters. The SDK derives the project ID
from the client key internally. Do not pass `projectId`, `apiKey`, `host`,
`configHost`, or the old `endpoints` object; those fields are not part of the
public config contract and the SDK rejects them.

Optional fields include:

| Field | Description |
|---|---|
| `autoCapture` | `true`, `false`, or a per-signal capture config. |
| `batchSize` | Integer events per batch, from 1 through 100. |
| `flushInterval` | Integer queue flush interval from 100 through 3,600,000 milliseconds. |
| `privacyMode` | `'standard'` or `'cookieless'`. |
| `consent` | Initial consent state for `analytics`, `personalization`, and `experiments`. |
| `persistence` | `'localStorage'` for project-scoped browser storage or `'memory'` for no browser storage. |
| `maxQueueSize` | Integer maximum from 1 through 100,000 events owned in memory. A new event is rejected synchronously when full; an already accepted event is never evicted to make room. |
| `debug` | Enables SDK diagnostics when `true`. |

Configuration is validated at runtime for JavaScript and parsed-JSON callers.
Unknown fields, malformed types, non-finite or fractional numeric values,
out-of-range values, and unsupported enum members fail during initialization.
The former `persistence: 'cookie'` and `privacyMode: 'strict'` values are not
implemented and are rejected instead of being mapped to different behavior.

Automatic click and rage-click events contain structural element metadata only.
They never include DOM text, form-control values, URLs, IDs, or CSS classes.
Known credential, one-time-code, file, and payment controls identified from
native types and semantic hints are excluded entirely. Page and referrer context
is also omitted from these events so URL parameters, fragments, paths, and page
titles cannot disclose secrets. Use manual events when an application needs an
explicitly chosen semantic label.

## Local Development Endpoints

When running the local APDL services, initialize the SDK with the local
gateway URL (`make dev-core` starts the gateway on port 8000):

```typescript
const apdl = APDL.init({
  endpoint: 'http://localhost:8000',
  auth: {
    clientKey: 'client_demo_0123456789abcdef',
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

Events are batched and sent to the gateway `endpoint` at `/v1/events`.
Properties, traits, and custom context must be canonical JSON: finite numbers,
strings, booleans, nulls, arrays, and plain string-keyed objects. Cycles,
`BigInt`, accessors, sparse arrays, unsupported values, malformed timestamps,
unknown context fields, excessive nesting/cardinality, and events over 64 KiB
are rejected synchronously before queue ownership. Requests are split below
512 KiB. Network errors, HTTP 408/425/429, and 5xx retain the same stable
message IDs for retry; other non-2xx responses are permanent and cannot poison
later queue entries. If both a retryable send and offline persistence fail, the
batch is requeued once in memory and returned in the drain's `pending` report;
the SDK does not spin or silently discard it.

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
event. The SDK fetches initial flag configuration from the gateway `endpoint` at
`/v1/flags` and listens for real-time updates on `/v1/stream`. The SSE request
uses `X-API-Key` header authentication through a fetch stream; the client key is
never placed in a URL. Reconnects resume with the standard `Last-Event-ID`
header.

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

Baseline email, payment-card, and SSN scrubbers run in every privacy mode.
`privacyMode: 'cookieless'` additionally derives a daily-rotating anonymous ID
without persisting that identifier.

Revoking analytics consent is an immediate delivery fence: the SDK aborts the
active analytics request when possible, clears its in-memory queue and this
project's IndexedDB queue, and stops analytics auto-capture and health capture.
No retained event is restored or sent across a revoke/regrant boundary.
Regranting consent starts capture again for new events only.

Experiment consent is also fail-closed. Denial returns a `null` assignment with
reason `consent_denied`, suppresses and removes exposures, clears experiment
context and flag caches, and prevents the initial flag fetch and SSE stream.
Regranting starts from a fresh authoritative flag snapshot. Personalization
denial prevents slot discovery and rendering and removes already rendered SDK
components; regranting resumes discovery for application-owned UI configs.

With `persistence: 'localStorage'`, browser persistence is project-scoped.
Anonymous identity, session, consent, flag cache, and offline event records use
the project ID derived from the client key, so two APDL projects on one origin
cannot restore each other's state. `persistence: 'memory'` does not read or
write localStorage and does not open IndexedDB; all state ends with the client.

With `persistence: 'localStorage'`, failed analytics deliveries may be retained
in IndexedDB for up to seven days.
Each record is scoped to the canonical project ID derived from the client key;
the key itself is never persisted. A client cannot drain or clear another
project's records on the same origin, and current analytics consent is checked
again before any retained event is restored. Legacy, invalid, and expired
records are discarded. Each project retains at most the newest 1,000 events and
5 MiB of UTF-8 JSON event payloads; older records are evicted deterministically
without counting or deleting another project's records. A single oversized or
non-JSON-serializable event is not retained.

## Local UI Renderer (No 0.3.0 Backend Delivery)

The package includes component registration, rendering, and slot-discovery
utilities. APDL 0.3.0 does not have a canonical Config UI-config endpoint and
does not publish UI configurations over SSE, so applications must pass a
locally owned `UIConfig` to `apdl.ui.render(...)`. The Agents personalization
graph is disabled for the same reason.

```typescript
// Register a custom component
apdl.ui.register({
  type: 'countdown-banner',
  schema: { properties: { deadline: { type: 'string' } } },
  render: (props, ctx) => { /* return an HTMLElement */ },
});

apdl.ui.render(locallyOwnedConfig, document.querySelector('#offer')!);

// React when the SDK discovers a UI slot on the page
apdl.ui.onSlotUpdate((slotId, element) => { /* ... */ });
```

## Debugging & Shutdown

```typescript
apdl.debug.enable();          // verbose console logging
apdl.debug.getQueue();        // inspect queued events
const report = await apdl.debug.flush();

console.log(report.delivered, report.persisted);
console.log(report.permanentRejections, report.pending);

const finalReport = await apdl.shutdown();
```

`flush()` drains all currently owned in-memory events, and concurrent flushes
join the same operation. Its frozen `DeliveryReport` distinguishes delivered,
offline-persisted, permanently rejected, consent-discarded, and still-pending
events. `shutdown()` stops accepting tracking immediately, joins concurrent
callers, tears down capture and SSE, and returns the final drain report. Calls
to `track`, `identify`, `group`, `page`, or `reset` after shutdown throw.

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
