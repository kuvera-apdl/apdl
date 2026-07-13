# APDL OSS Release Readiness Audit

**Audit date:** 2026-07-13

**Repository:** `APDL-OSS`

**Branch / revision:** `recent-critical-fixes` at `d095776`

**Scope:** JavaScript SDK, Python SDK, Ingestion, Config, Query, Agents, Codegen, Admin API, Admin UI, Redis-to-ClickHouse writer, ETL and future pipeline scaffolds, gateway, database contracts and migrations, local development, containers, CI, packaging, release automation, documentation, security posture, and OSS governance. Kubernetes and Terraform are evaluated only as removed/unsupported surfaces because their obsolete implementations were deleted before this revision.

## Executive verdict

**APDL is not ready for an initial tagged OSS release.** Publishing the source for review is reasonable, but the repository should not yet ship an installable public alpha, enable autonomous Codegen publication, or make production/multi-tenant claims.

The repository is materially stronger than the baseline audited at `294584a`. The fixes through `d095776` moved PostgreSQL schema authority into a checksummed migrator; made Redis-to-ClickHouse acknowledgement, stale-delivery recovery, backlog consumption, and stream tenant authority substantially safer; isolated Codegen execution from the credential-bearing API; scoped Codegen service authentication by project; fenced Agents run recovery; isolated JavaScript offline storage by project and current consent; and closed the three known Admin-to-Codegen/login criticals.

The three critical defects found at `5674b96` are now resolved in code. Commit `1be7124` removes DOM text/form values from reserved click events and adds a non-configurable SDK plus Ingestion privacy boundary. Commit `a6d51e8` separates strict tenant preferences from operator-owned safety floors. Commit `d095776` replaces tenant-selected repositories with verified operator grants, immutable numeric repository authority, repository-restricted least-privilege tokens, and changeset snapshots. The full repository test, lint, build, Compose rendering, and package checks pass at the audited revision. No open critical finding was verified in this static pass.

The most important high-severity release blockers are also cross-surface rather than isolated unit failures:

- JavaScript adds `experiment_context` to exposure events, while Ingestion rejects that strict reserved property; valid SDK exposures can poison an entire batch.
- Ingestion still exposes permissive legacy identities, accepts timestamps it cannot guarantee the writer can parse, reports partial Redis success as a batch-level `202`, and lacks body/depth/distributed event quotas.
- Config experiment/backing-flag mutations are non-atomic, direct flag CRUD can drift an experiment-owned flag, SSE is process-local/racy, and targeting semantics are not byte-for-byte consistent across SDK/server evaluators.
- Query experiment analysis trusts caller-supplied labels, mishandles all-zero and multi-treatment cases, and can emit non-finite small-sample statistics; autonomous decisions must not rely on it yet.
- Agents run leasing is improved, but job creation/execution, safety quotas, provider fallback policy, and several action/audit paths remain incomplete or process-local.
- The canonical `make dev-all` path now has an explicit development overlay that builds and launches the Codegen worker with development-only Docker socket, network, and draft-publication authority. This makes the local path runnable, but the launcher remains unsuitable for production; publication idempotency, kill-switch coverage, evidence provenance, production worker isolation, and live sandbox proof remain incomplete.
- Public self-registration plus unrestricted self-service project creation has no project/LLM-spend quota. A user can mint new project authority and trigger globally funded Agents work repeatedly.
- The canonical quick start and examples fail the current strict flag schema and project authority; CI omits tests for four backend services and all Codegen checks; tagged release publishes only npm plus four of the required images.

The defensible current milestone is a **private developer preview**, with Codegen publication disabled and Kafka/Flink and ETL v2 explicitly experimental. Kubernetes and Terraform are not part of this repository's supported release surface. A public alpha still needs the high-severity contract, durability, quick-start, CI, release-artifact, and quota gates in this document. Production readiness is substantially further away.

## Audit method and validation

The audit traced request authority, data flow, failure handling, schemas, persistence, background work, packaging, and deployment wiring from the current checkout. Static review covered every runtime surface listed above. Validation was run against the repository as a whole, not only individual changed packages.

| Check | Result |
|---|---|
| `make test` | Passed: 1,824 tests total |
| JavaScript SDK | 219 tests passed |
| Python SDK | 136 tests passed; 92.69% coverage against an 88% gate |
| Ingestion | 90 tests passed |
| Config | 145 tests passed |
| Query | 93 tests passed |
| Agents | 288 tests passed |
| Codegen | 532 tests passed |
| Redis writer | 21 tests passed |
| ETL | 45 tests passed |
| Admin API | 37 tests passed |
| Admin UI | 218 tests passed |
| `make lint` | Passed for every configured lint target |
| `make build` | Passed; Admin emitted a large-bundle warning |
| `make release-sdk` | Passed; npm pack dry-run produced `@apdl-oss/sdk@0.2.0` |
| Python SDK `uv build` | Passed with deprecated license metadata warnings; built artifacts omitted `LICENSE` |
| Docker Compose config | Full and dependency configurations parsed successfully |
| Shell syntax | Repository shell scripts passed `bash -n` |
| Dependency audit | `npm audit`: SDK 12 and Admin 6 development-tool findings, including vulnerable Vitest lines; `npm audit --omit=dev`: zero runtime findings in both packages |
| Kubernetes / Terraform | Obsolete implementations are absent from this revision and are not a supported release surface |

No live Redis, PostgreSQL, ClickHouse, GitHub App, LLM provider, real Aider, browser consumer, Docker sandbox, or clean-machine Compose deployment was exercised in this refresh. The full Compose file rendered, but no containers were started. Passing unit/static checks therefore does not validate the multi-process, browser, crash/replay, or external-provider defects described below.

### Baseline high/critical revalidation

| Status at `d095776` | Findings |
|---|---|
| Open criticals verified by this re-audit | None |
| Resolved prior high/critical findings in code | APDL-AUD-001, 038, 043, 049, 051, 053, 058, 059, 060, 068, 069, 070, 072, 082, 084, 104, 106, 109, 110 |
| Partially/substantially resolved but still requires live or production proof | APDL-AUD-048 (isolated Codegen worker default), APDL-AUD-120 (development Compose sandbox path) |
| Resolved by deleting unsupported infrastructure | APDL-AUD-088 through 092 |

### Finding inventory

This document contains 118 detailed findings: 20 resolved in code, 5 partially/substantially resolved, 73 open high-severity findings, 19 open medium-severity findings, and 1 open low/medium finding. There are no verified open critical findings at `d095776`; the unresolved high-severity catalog is still sufficient to block the advertised public release.

## Severity and release criteria

- **Critical:** a direct tenant-boundary, credential-execution, or silent data-loss defect. Must be fixed before any public multi-user or production claim.
- **High:** a material correctness, durability, security, deployment, or advertised-feature failure. Must be fixed or explicitly excluded from the supported release scope.
- **Medium:** an important robustness, operability, consistency, or maintenance gap. May follow an alpha only when documented and bounded.
- **Low:** polish or localized maintainability work with limited immediate impact.

## Service and surface scorecard

| Surface | Relevance to APDL | Completeness | Current release disposition |
|---|---|---|---|
| JavaScript SDK | Essential browser/client entry point | Feature-rich; offline project isolation and reserved-click privacy are fixed, but lifecycle/framework and cross-service contracts remain incomplete | **No-go** |
| Python SDK | Important server-side client | Strong models/tests; lifecycle and publication path incomplete | **No-go** until shutdown/package issues are fixed |
| Ingestion | Essential data-plane edge | Core API exists; strict SDK exposure compatibility, input bounds, partial failure, and canonicalization are incomplete | **No-go** |
| Config | Essential control plane | Flags/experiments are substantial; ownership, atomicity, evaluator parity, and multi-replica distribution are incomplete | **No-go** |
| Query | Essential analytics plane | Broad query/statistics coverage; identity and experiment correctness need work | **No-go** for experiment/autonomous decisions |
| Agents | Core product differentiator | Extensive workflows/tests; leasing is improved but execution/safety/action persistence remain incomplete | **No-go** for autonomous production use |
| Codegen | Optional but high-risk differentiator | Strong state/evidence model, verified repository grants, safety-authority separation, isolated-worker default, and an explicit runnable development overlay; production worker isolation, publication idempotency, and live proof remain incomplete | **No-go** for production PR publication |
| Admin API | Security and tenant boundary | Prior critical route/lockout bugs are fixed; self-service quotas, readiness, and proxy maintenance remain incomplete | Preview only |
| Admin UI | Primary operator experience | Broad tested UI; degraded-auth, external-link, workspace-state, and bundle issues remain | Preview only |
| Redis-to-ClickHouse writer | Essential persistence path | Core crash recovery and tenant authority fixed; idempotency, DLQ, health, and schema issues remain | **No-go** for lossless claims |
| ETL v2 | Relevant future canonical pipeline | Library/scaffolding only; not connected to live traffic | Exclude from supported release |
| Kafka/Flink | Optional future scale path | Aspirational scaffolding | Exclude from supported release |
| SDK gateway | Useful single front door | Suitable for local Compose only | Do not present as production gateway |
| PostgreSQL/ClickHouse migrations | Essential installation contract | PostgreSQL authority fixed; ClickHouse still lacks an immutable ledger/backfill/rollback contract | **No-go** for upgrades |
| Docker/local setup | Essential contributor onboarding | Stack and a development-only Codegen worker overlay are defined; strict smoke/examples and clean-checkout end-to-end proof remain incomplete | **No-go** for advertised quick start |
| Kubernetes / Terraform | Optional production deployment | Deliberately removed from the repository | Unsupported |
| CI/release | Essential OSS trust and delivery | Partial coverage and partial artifacts | **No-go** |
| Documentation/governance | Essential OSS usability | Good base documents; current architecture/release claims drifted | Needs release pass |

---

## 1. JavaScript SDK

### Relevance and completeness

The JavaScript SDK is APDL's primary browser integration and is directly responsible for event capture, identity, consent, offline delivery, feature configuration, flag evaluation, exposures, and SSE updates. Its flag schema is strict, the test suite is broad, packaging succeeds, and the published format covers ESM/CJS/IIFE and React. It is highly relevant and relatively mature. The prior default auto-capture privacy defect is fixed; lifecycle, framework, consent-revocation, serialization, and cross-service contracts remain incomplete.

### APDL-AUD-106 — Resolved critical: default click auto-capture leaked live form values

**Baseline problem.** Click capture fell back from text content to the target element's live value. With click capture and default analytics consent enabled, clicking an input could transmit a password, email, card value, or other live form content.

**Resolution.** Commit `1be7124` makes reserved `$click` and `$rage_click` events structural metadata only, excludes sensitive native/custom controls before click and rage-click bookkeeping, sanitizes current and legacy/offline events outside the configurable scrubber pipeline, and repeats the boundary in Ingestion. The SDK and Ingestion regression suites cover passwords, payment/OTP hints, labels, shadow DOM, custom scrubbers, stored legacy records, and direct API submissions (`sdk/javascript/src/privacy/auto-capture-safety.ts`, `sdk/javascript/src/capture/auto-capture.ts`, `services/ingestion/app/privacy.py`).

**Remaining boundary.** This was validated through unit/JSDOM tests, not a packed SDK in real browsers. The release gate should retain a browser-consumer regression so the structural-only contract cannot drift during bundling or framework integration.

### APDL-AUD-001 — Resolved critical: offline persistence crossed project and consent boundaries

**Baseline problem.** The prior store used one origin-wide IndexedDB queue without immutable project authority, and restored events without checking current consent.

**Resolution.** Commit `31d4ea4` introduced project-scoped records/storage, migration and TTL/bounds, and current-consent checks during restore. The focused IndexedDB suite now covers project isolation and passes.

**Remaining related gap.** Anonymous ID, session, and persisted consent keys are still origin-global (`sdk/javascript/src/core/client.ts:38,420-443`, `sdk/javascript/src/privacy/consent.ts:3,95-127`, `sdk/javascript/src/capture/session.ts:3,93-132`). Multiple project clients can therefore share identity/session/consent even though their offline event rows are now isolated. Namespace all persistent SDK identity and privacy state by project.

### APDL-AUD-002 — High: shutdown awaits only one batch

**Problem.** `sdk/javascript/src/core/event-queue.ts:98-135` snapshots and sends one batch, then schedules later work without returning a promise for the complete drain. `sdk/javascript/src/core/client.ts:337-345` awaits that single flush and stops.

**Impact.** More than one batch can leave a tail behind, while an in-flight scheduled flush is not joined. The public shutdown/flush guarantee is therefore stronger than the implementation.

**Solution.** Add one serialized `drainUntilEmpty()` lifecycle operation, join the active request, reject new intake after shutdown begins, and persist or return every unsent event before transport closes.

### APDL-AUD-003 — High: normal requests always use `keepalive`

**Problem.** `sdk/javascript/src/core/transport.ts:36-46` sets `keepalive: true` on ordinary batch requests, not only unload delivery.

**Impact.** Browsers impose small aggregate keepalive body limits, commonly around 64 KiB. A valid default batch can exceed that limit and enter a repeat-fail/offline cycle.

**Solution.** Use ordinary `fetch` for normal delivery. Reserve keepalive or `sendBeacon` for unload, split by serialized byte size rather than event count, and test payloads near browser limits.

### APDL-AUD-004 — High: React/Next package lacks a client boundary

**Problem.** `sdk/javascript/src/react/index.ts:1-18` does not begin with a preserved `'use client'` directive, although Next App Router usage is advertised in `sdk/javascript/README.md:83-99`.

**Impact.** Consumers following the documentation can encounter server/client component or hook errors after installing the packed artifact.

**Solution.** Preserve the literal directive in the emitted React entry and add a test that installs the tarball into a minimal Next App Router application and builds it.

### APDL-AUD-005 — High: environment auto-configuration is not compatible with advertised frameworks

**Problem.** `sdk/javascript/src/core/env.ts:13-37` dynamically indexes `process.env`. Next public variables need statically recognizable access, while Vite normally exposes `import.meta.env`. Existing tests only emulate Node-style environment variables.

**Impact.** The zero-configuration Next/Vite path in `sdk/javascript/README.md:62-99` can compile without embedding the intended endpoint/key.

**Solution.** Define explicit framework adapters or explicit static accesses, document the supported build-time contract, and build packed consumer fixtures in CI.

### APDL-AUD-006 — High: browser credential threat model is contradictory

**Problem.** `sdk/javascript/README.md:123-139` calls the full `proj_*_<secret>` value browser-safe, while `SECURITY.md:30-34` describes the secret portion as a password. `sdk/javascript/src/sse/connection.ts:73-81` places it in a query string, and the locally provisioned development key receives broad roles in `scripts/init-postgres.sh:72-89`.

**Impact.** A credential can enter bundles, browser tooling, proxy logs, and observability systems. Copying the local broad-role key into a browser would expose administrative authority.

**Solution.** Introduce one canonical restricted public client credential or exchange it for a short-lived stream token. Remove long-lived secrets from URLs, scope browser credentials to ingestion/config read, rate-limit them independently, and document rotation and exposure assumptions.

### APDL-AUD-007 — Medium: SDK identity and runtime configuration are not strict enough

**Problem.** `sdk/javascript/src/core/init.ts:65-73` reuses the singleton based only on client key and silently ignores conflicting endpoint/privacy configuration. `sdk/javascript/src/core/config.ts:186-207` coerces or accepts some malformed values, including invalid numeric and persistence values. `debug.enable()` mutates client config after transport/SSE constructors captured the old value.

**Impact.** Reinitialization can continue sending to the wrong endpoint or under the wrong privacy policy, and configuration errors fail silently.

**Solution.** Treat endpoint, tenant identity, persistence, and privacy configuration as immutable; reject conflicting reinitialization and non-finite/out-of-range values; make debug an observable shared setting.

### APDL-AUD-008 — Medium: advertised cookie persistence is not implemented

**Problem.** `persistence: "cookie"` is declared in `sdk/javascript/src/core/config.ts`, but consent and session persistence map it to local storage in `sdk/javascript/src/privacy/consent.ts:95-114` and `sdk/javascript/src/capture/session.ts:93-112`.

**Impact.** Applications choose a storage behavior they do not receive, which is particularly material for privacy and retention policies.

**Solution.** Implement the cookie contract with explicit attributes and limits, or remove it from the strict schema and documentation.

### APDL-AUD-009 — High: invalid properties can poison the delivery queue

**Problem.** Event properties are accepted before JSON serialization; serialization happens after dequeue in `sdk/javascript/src/core/transport.ts:28-31`.

**Impact.** Cyclic objects, `BigInt`, or other non-JSON values can cause repeated batch failure and prevent valid neighboring events from progressing.

**Solution.** Validate/canonicalize JSON before enqueue, reject with an observable client error, and quarantine a bad record instead of retrying the whole batch forever.

### APDL-AUD-010 — Medium: default consent and import-time auto-start are too permissive

**Problem.** Consent defaults are permissive in `sdk/javascript/src/core/config.ts:124-128`, and `sdk/javascript/src/index.ts:51-57` can auto-start from environment configuration during module import.

**Impact.** Events may be captured before a host application has resolved its consent state.

**Solution.** Require an explicit consent posture for browser auto-capture, defer startup until consent is resolved, and document opt-in/opt-out jurisdiction implications.

### APDL-AUD-107 — High: JavaScript exposure context violates Ingestion's strict contract

**Problem.** `sdk/javascript/src/core/client.ts:483-498` adds `properties.experiment_context` after `experiments.setContext()`, but the reserved exposure allowlist in `services/ingestion/app/validation/schema.py:40-53,210-223` rejects that property as unknown.

**Impact.** A valid JavaScript flag exposure receives HTTP 400; because the whole request is rejected, unrelated events in the same batch are retained/retried with the permanently invalid exposure and can stop delivery progress.

**Solution.** Select one canonical exposure schema and change the SDK, Ingestion, writer/projections, and Query attribution together. Add one packed-SDK-to-Ingestion shared-contract fixture so producer and reserved-event validation cannot drift independently.

### APDL-AUD-108 — High: consent revocation does not cancel already-queued events

**Problem.** Consent updates in `sdk/javascript/src/core/client.ts:213-217` change the manager only. Normal and unload sends in `sdk/javascript/src/core/event-queue.ts:98-150` neither re-check current consent immediately before transport nor clear the in-memory queue.

**Impact.** An event captured while analytics consent was granted can still be transmitted after the user revokes consent in the same session.

**Solution.** Make denial atomically stop auto-capture, clear in-memory and project offline queues, and fence every normal/unload send on current consent. Test revocation during an active request and before unload.

---

## 2. Python SDK

### Relevance and completeness

The Python SDK is the server-side integration path. Its Pydantic contracts, parity fixtures, tests, and coverage gate are strong. It is relevant to backend event production but not yet complete as a released OSS package because shutdown correctness, endpoint defaults, semantic parity, and PyPI automation are unresolved.

### APDL-AUD-011 — High: shutdown can close transport while retries are active

**Problem.** `sdk/python/apdl/queue.py:54-60` caps the worker join near `flush_interval + request_timeout`, while `sdk/python/apdl/transport.py:54-83` can spend longer across request attempts and retry sleeps. `sdk/python/apdl/client.py:283-293` then closes transport, and tracking methods can still enqueue after shutdown.

**Impact.** Pending events can be lost or race a closed HTTP client even though shutdown is documented as flushing them.

**Solution.** Stop intake first, make requests and retry sleeps cancellation-aware, join the worker before closing transport, and return or persist a clear pending/failure result.

### APDL-AUD-012 — High: SDK event semantics differ by language

**Problem.** JavaScript emits `identify`, `group`, and configurable page event names in `sdk/javascript/src/capture/manual.ts:48-101`; Python emits `$identify`, `$group`, and `$page` in `sdk/python/apdl/client.py:103-139`. Exposure deduplication also uses different scopes: JavaScript includes session/page/component, while Python uses process-lifetime identity/flag/version/variant.

**Impact.** The same product action produces different analytics and exposure counts depending on SDK language, violating the repository's strict canonical-schema rule.

**Solution.** Publish one language-independent event and exposure contract, migrate competing names, and run shared fixtures against both packed SDKs.

### APDL-AUD-013 — High: invalid payloads can be requeued forever

**Problem.** Arbitrary property values reach the worker, and `sdk/python/apdl/queue.py:103-122` can requeue an encoding failure as though it were transient transport failure.

**Impact.** One non-JSON record can repeatedly block or churn a queue.

**Solution.** Validate JSON compatibility synchronously before enqueue; classify permanent versus transient errors; isolate and report invalid records.

### APDL-AUD-014 — Medium: the self-hosted SDK silently defaults to a remote endpoint

**Problem.** `sdk/python/apdl/config.py:13,36-38,72-75` defaults to `https://api.apdl.dev` with limited URL validation.

**Impact.** A local OSS quick start that omits the endpoint can send data outside the local deployment or simply fail against an unrelated service.

**Solution.** Require an explicit endpoint for the OSS package, or define one clearly documented local default; validate scheme, host, credentials, and path.

### APDL-AUD-015 — High: Python release artifacts and automation are incomplete

**Problem.** The tagged release workflow does not publish the Python SDK. A local `uv build` succeeded but both wheel and sdist omitted `LICENSE`; `sdk/python/pyproject.toml` uses deprecated license-table/classifier metadata and stale repository links. Version information is duplicated in package code.

**Impact.** The promised PyPI artifact is not produced by the release, and built source/binary distributions do not carry the repository's license file.

**Solution.** Include `LICENSE`, adopt current PEP 639 metadata, derive one version source, add artifact inspection and installation tests, and use PyPI trusted publishing gated by the release test matrix.

---

## 3. Ingestion Service

### Relevance and completeness

Ingestion is the public data-plane boundary. Authentication and basic batch validation are present, but the service currently acknowledges contracts that downstream storage cannot safely preserve. Input bounding, canonicalization, partial failure, and distributed rate enforcement are incomplete.

### APDL-AUD-016 — High, partially mitigated: permissive legacy event schema is not canonical

**Problem.** `services/ingestion/app/models/schemas.py:24-36` uses `extra="allow"` and accepts competing snake_case/camelCase identity fields plus unknown fields. The writer now understands camel-case IDs and rejects conflicting pairs, which fixes the prior silent-loss path, but normalization and conflict detection still happen after Ingestion has returned `202`.

**Impact.** A request can be accepted at the public edge and later diverted to the DLQ; unknown fields and aliases undermine the repository's strict canonical-schema rule and make producer behavior dependent on a downstream implementation.

**Solution.** Select one strict event schema, reject unknown and aliased fields, and normalize exactly once at the edge. Test an event from each SDK through Redis and ClickHouse.

### APDL-AUD-017 — High, partially mitigated: invalid timestamps are accepted and later DLQed

**Problem.** Event timestamps remain arbitrary strings (`services/ingestion/app/models/schemas.py:33`, `services/ingestion/app/validation/schema.py:163-168`). The writer now sends parse failures to a safe metadata DLQ before acknowledging, but Ingestion still confirms values such as `not-a-date` and impossible calendar dates.

**Impact.** Ingestion confirms an event that will never be queryable.

**Solution.** Parse and canonicalize timestamps in the ingestion request model, reject invalid values synchronously, and retain the DLQ only for unexpected post-validation corruption.

### APDL-AUD-018 — High: partial Redis write semantics are incompatible with both SDKs

**Problem.** `services/ingestion/app/routers/events.py:77-118` writes events individually and returns a `202` body containing accepted/failed counts when only part of a batch succeeds. Both SDK transports treat any 2xx response as success for the entire batch.

**Impact.** Failed items are silently dropped and cannot be retried correctly.

**Solution.** Prefer an atomic Redis pipeline/transaction for the canonical batch. If partial acceptance is required, return stable per-item IDs/statuses and make every SDK retry only failed IDs idempotently.

### APDL-AUD-019 — High: body, JSON depth, and effective event volume are unbounded

**Problem.** The API caps batch count but not request bytes, property bytes, nesting depth, or object cardinality. `services/ingestion/app/middleware/rate_limit.py:17-89` is an in-memory, per-process request counter, and one request consumes one token whether it carries one or 500 events.

**Impact.** Large nested payloads can exhaust memory/CPU; horizontal replicas multiply/reset limits; clients can bypass intended event quotas with full batches.

**Solution.** Enforce edge and application byte/depth/value limits, count events and bytes, move quotas to an atomic shared store, and expose rate-limit metrics.

### APDL-AUD-020 — Low/medium: caller-controlled forwarding headers corrupt recorded client IP

**Problem.** `services/ingestion/app/routers/events.py:70-74` trusts `X-Forwarded-For` without a trusted-proxy boundary. The rate limiter is keyed by authenticated project, so the prior claim that this header controls the current rate identity is stale; it still controls persisted/logged IP metadata.

**Impact.** Direct callers can spoof analytics/log IP metadata and undermine investigations or any downstream geolocation/privacy rule.

**Solution.** Honor forwarding headers only from configured proxies and use the framework's trusted-proxy middleware/edge source address.

---

## 4. Config Service

### Relevance and completeness

Config owns flags, experiments, evaluation payloads, cache invalidation, and live SDK distribution. The evaluation model and tests are substantive. The main completeness gap is transactional authority: database records, audit, cache, SSE, and related flag/experiment mutations are coordinated in application code rather than one durable transaction/outbox.

### APDL-AUD-021 — High: experiment and backing-flag mutations are not atomic

**Problem.** Experiment creation first creates a flag and later creates the experiment with compensating archive logic in `services/config/app/routers/admin.py:712-793`. Update and deletion similarly mutate the flag and experiment in separate operations at `:796-944`. Generic flag update/archive routes at `:260-325,396-430` do not check whether an experiment owns that backing flag, and the result of flag archive during experiment deletion is not authoritative.

**Impact.** A database, cache, or process failure can leave active orphan flags, an experiment whose allocation differs from its flag, or a deleted experiment with a live flag.

**Solution.** Add an immutable experiment-to-flag ownership relation, reject generic mutation of experiment-managed flags, and perform the experiment, flag, audit, and outbox state transition on one PostgreSQL connection/transaction. Add experiment versioning.

### APDL-AUD-022 — High: mutation, audit, cache, and broadcast have ambiguous failure semantics

**Problem.** Flag routes in `services/config/app/routers/admin.py` commit data, write audit, invalidate cache, and broadcast as separate awaits.

**Impact.** The durable mutation can succeed but the HTTP request can return an error after audit or delivery fails. Retrying becomes ambiguous and audit/distribution can diverge from the database.

**Solution.** Put the authoritative mutation and audit/outbox record in one transaction; make cache/SSE consumers idempotent and retryable.

### APDL-AUD-023 — High: SSE distribution is process-local

**Problem.** `services/config/app/sse/broadcaster.py:15-25,99-154` stores subscribers in one process. A flag mutation broadcasts only to clients connected to that replica.

**Impact.** With two or more replicas, a client can remain stale until another synchronization path happens.

**Solution.** Fan out through Redis Pub/Sub/Streams or another durable broker and test mutation on replica A with a stream attached to replica B.

### APDL-AUD-101 — High: initial SSE snapshot has a registration race

**Problem.** `services/config/app/routers/stream.py:31-42` reads the initial PostgreSQL snapshot before registering the subscriber queue.

**Impact.** A mutation committed between the snapshot read and registration is present in neither the snapshot nor the new queue. The client can remain stale indefinitely if no later update causes a full refresh.

**Solution.** Register first and reconcile against a monotonic configuration revision, or replay from a durable project event log before declaring the stream current.

### APDL-AUD-102 — High: revoked credentials retain an already-open stream

**Problem.** Config authenticates `/v1/stream` only during connection setup; the long-lived generator never revalidates credential active/expiry state.

**Impact.** A revoked or expired API key can continue receiving future project configuration until the network connection ends.

**Solution.** Use short-lived, project-scoped SSE tickets or periodically revalidate the credential and terminate the stream on revocation.

### APDL-AUD-024 — High: a slow subscriber can remain connected but permanently stale

**Problem.** On queue overflow, `broadcaster.py:129-149` removes the connection. The generator in `services/config/app/routers/stream.py:51-67` does not learn that it was removed and can continue sending fallback heartbeats.

**Impact.** The SDK sees a healthy-looking connection, never reconnects, and misses all later updates.

**Solution.** Send an explicit close/resync sentinel, terminate the stream, and require the client to fetch the latest full state before reconnecting.

### APDL-AUD-103 — High: Redis cache failure becomes a Config availability failure

**Problem.** Cache get/set/invalidation awaits can propagate Redis errors before a PostgreSQL read or after an authoritative mutation has committed.

**Impact.** Healthy PostgreSQL flags become unavailable during a cache outage, and a committed mutation can return an error that invites an ambiguous retry.

**Solution.** Fall back to PostgreSQL on cache read/write failure and reconcile cache asynchronously through the same transactional outbox used for SSE.

### APDL-AUD-025 — Partially resolved high: readiness is unsafe; startup DDL is fixed

**Problem.** Config now validates the operator-owned migration 006 at startup (`services/config/app/main.py:39-43`, `services/config/app/schema.py:39-75`) instead of running DDL. However `/health` still reports degraded PostgreSQL/Redis with HTTP 200 at `services/config/app/main.py:133-157`, and there is no separate readiness contract.

**Impact.** Orchestrators can route traffic to an unusable instance even though the prior concurrent-startup-DDL risk is removed.

**Solution.** Keep schema authority in the checksummed PostgreSQL migrator and add separate liveness/readiness endpoints with `503` when required dependencies or schema are unavailable.

### APDL-AUD-026 — High: experiment dates are arbitrary strings

**Problem.** `services/config/app/models/schemas.py:387-418` accepts string start/end values. `services/config/app/experiments/expiry.py:36-75` can ignore an unparseable value.

`running` also enables the backing flag immediately regardless of future `start_date`, only end dates have a scheduler, and a primary metric is optional.

**Impact.** A typo can create an experiment that never expires; a future-scheduled experiment serves immediately; end-before-start is accepted; and a running experiment can have no measurable primary outcome.

**Solution.** Use strict timezone-aware date/datetime fields, require end after start, add a start scheduler, and require the primary metric/decision plan before transition to `running`.

### APDL-AUD-027 — Medium: generic update bypasses lifecycle metadata

**Problem.** A generic flag update can toggle enabled state without consistently using the disable reason/by/at semantics of the dedicated lifecycle route.

**Impact.** Audit and guardrail explanations become incomplete or contradictory.

**Solution.** Make lifecycle transitions a single canonical command and reject enabled-state changes through generic update.

### APDL-AUD-028 — High completeness gap: personalization delivery is absent

**Problem.** The Agents personalization graph explicitly reports that Config lacks the required UI-configuration API, and there is no downstream SDK/rendering contract for personalized UI.

**Impact.** Root-level product claims imply a capability that cannot complete an end-to-end loop.

**Solution.** Either implement the strict Config storage/distribution/SDK consumption path with audit and rollback, or remove personalization from the supported OSS feature list.

### APDL-AUD-116 — High: audit provenance is caller-spoofable

**Problem.** `FlagDisable.source` and `FlagCleanup.source` are public request fields in `services/config/app/models/schemas.py:327-336`; the handlers store those caller values as the audit actor in `services/config/app/routers/admin.py:371-381,485-495`.

**Impact.** Any `config:write` principal can make history claim a mutation came from `system` or `admin` rather than the authenticated credential.

**Solution.** Derive actor identity from `request.state.principal`, store origin/reason as a separate typed field, and require separate internal authority for automated guardrail commands.

### APDL-AUD-117 — High: client and server targeting semantics diverge

**Problem.** Python evaluation uses `float(...)` and Python regular expressions (`services/config/app/flags/evaluator.py:98-118`), while JavaScript uses ECMAScript `Number(...)`/`Number.isFinite` and ECMAScript regex (`sdk/javascript/src/flags/evaluator.ts:212-233`). The schema permits unbounded values/patterns/rules. Verified edge cases disagree: Python rejects empty/whitespace/hex numeric strings but accepts `Infinity`; JavaScript does the opposite.

**Impact.** A flag in `both` mode can assign different variants client-side and server-side, contaminating exposures. Pathological regex can also block the Config event loop or a browser consumer.

**Solution.** Define one strict numeric coercion contract and portable bounded regex dialect, add rule/condition limits, and share edge-case fixtures across JavaScript, Python SDK, Admin evaluator, and Config.

### APDL-AUD-121 — High: server-side exposure persistence fails open

**Problem.** `POST /v1/evaluate` returns the variant even when publishing the corresponding exposure to Redis fails; `_publish_exposure` swallows the error (`services/config/app/routers/evaluate.py:47-53,95-108`).

**Impact.** Users receive assignments that never enter analytics. Missingness is correlated with infrastructure failures and can bias experiment/guardrail decisions.

**Solution.** Persist exposures through a durable outbox. If availability policy deliberately fails open, return explicit persistence status and exclude incomplete assignments from autonomous decisions.

---

## 5. Query Service

### Relevance and completeness

Query is the analytical authority used by humans and agents. It includes funnels, cohorts, retention, experiment statistics, and guardrails with strict selector parsing and tenant checks. Identity semantics, statistical decision rules, and resource limits are not yet safe enough for autonomous actions.

### APDL-AUD-118 — High: experiment identity is a label, not analytics authority

**Problem.** `services/query/app/routers/experiments.py:28-67` states that `experiment_id` does not filter data and trusts caller-supplied `flag_key` and metric. SQL at `services/query/app/clickhouse/queries.py:310-357` filters only project/flag and ignores Config's authoritative experiment metric, control, variants, dates, and status.

**Impact.** Any label can be paired with any flag/metric; reused flags and post-completion events enter results; Query cannot prove which configured experiment produced an autonomous recommendation.

**Solution.** Resolve the project-scoped Config experiment record server-side, persist immutable experiment identity on exposures, and derive metric, control, arms, and observation window from that record.

### APDL-AUD-119 — High: zero-conversion experiments are reported as missing

**Problem.** Experiment metric SQL inner-joins metric events, and the route returns 404 on empty metric rows before consulting exposure rows (`services/query/app/clickhouse/queries.py:330-357`, `services/query/app/routers/experiments.py:69-83`).

**Impact.** A valid experiment with exposures in every arm but zero conversions is treated as no data, biasing analysis toward experiments that have at least one success.

**Solution.** Make exposures authoritative and left-join/zero-fill metric events for every configured arm.

### APDL-AUD-029 — High: anonymous actors collapse into one funnel/retention user

**Problem.** Funnel and retention SQL in `services/query/app/clickhouse/queries.py:125-151,183-259` groups on `user_id`. Anonymous browser events commonly have an empty `user_id` and a populated `anonymous_id`.

**Impact.** All anonymous users can collapse into one synthetic actor, corrupting funnel conversion and retention.

**Solution.** Define a namespaced canonical actor (`u:<id>` / `a:<id>`) or a durable identity-stitching table and use it consistently in every query.

### APDL-AUD-030 — High: experiment attribution has identity collisions and crossover

**Problem.** `services/query/app/clickhouse/queries.py:310-357` collapses `user_id` and `anonymous_id` into an untagged assignment string, groups one person separately per variant, does not bound events to the experiment window, and keys attribution by flag rather than immutable experiment identity.

**Impact.** Crossover users enter multiple arms; equal user/anonymous strings collide; reused flags and long-after-experiment events contaminate results.

**Solution.** Persist one canonical first assignment per immutable experiment and actor, namespace identities, define stitching rules, and bound the observation window.

### APDL-AUD-031 — High: frequentist peeking can drive unsafe autonomous stopping

**Problem.** Agents request default frequentist analysis in `services/agents/app/tools/experiments.py:237-274` and can treat ordinary `is_significant` as an early-stop boundary in `services/agents/app/graphs/experiment_evaluation.py:125-179`.

**Impact.** Repeated peeking inflates false-positive rates and can trigger automated ship/rollback decisions on invalid evidence.

**Solution.** Require a predeclared sequential/alpha-spending method for repeated observation, persist the experiment's decision plan, and prohibit ordinary fixed-horizon tests as early-stop signals.

### APDL-AUD-032 — High: multi-variant analysis silently evaluates one treatment

**Problem.** `services/query/app/routers/experiments.py:122-163` infers control by name/order, selects only the first treatment for the effect and p-value, but returns summaries for every variant.

**Impact.** A response looks multi-arm while its recommendation represents one unstated comparison and ignores other beneficial or harmful arms.

**Solution.** Require explicit control, return one corrected treatment/control comparison per arm, or reject multi-arm analysis until supported.

### APDL-AUD-033 — High: sessionless exposures corrupt automatic guardrails

**Problem.** Guardrail SQL at `services/query/app/clickhouse/queries.py:364-427` groups and joins solely by `session_id` without excluding empty values. The schema permits empty session IDs.

**Impact.** Unrelated empty-session exposures and frontend failures collapse together and can falsely trigger automatic flag disablement.

**Solution.** Require a nonempty session or canonical actor fallback, deduplicate exposures, and test anonymous/sessionless inputs before enabling automation.

### APDL-AUD-034 — High: every Query replica can run the guardrail mutator

**Problem.** `services/query/app/main.py:21-39,79-103` starts the monitor in each process; no lease or singleton owner exists.

**Impact.** Replicas can race duplicate Config mutations and audit records.

**Solution.** Move the monitor to a durable singleton worker or acquire a renewable distributed lease and make mutations idempotent.

### APDL-AUD-035 — Medium: reported total users are not distinct totals

**Problem.** Cohort responses sum daily unique counts in `services/query/app/routers/cohorts.py:46-70`, double-counting returning users. Event totals similarly sum selector-level uniques.

**Impact.** Values labeled as totals overstate unique users and can mislead users and agents.

**Solution.** Run a range-wide distinct-actor query or rename the field to an explicitly documented estimate.

### APDL-AUD-036 — High: warehouse work is effectively unbounded

**Problem.** Date validation checks ordering but not maximum span. ClickHouse execution has no per-query timeout, row/memory limit, project concurrency limit, or cancellation budget.

**Impact.** An authenticated client or agent can issue expensive scans that degrade every tenant.

**Solution.** Cap spans/cardinality, add project rate/concurrency quotas, configure ClickHouse execution settings, paginate large results, and propagate cancellation.

### APDL-AUD-037 — High: small/constant samples generate non-finite or inconsistent statistics

**Problem.** The endpoint does not require per-arm minimums or finite output; Welch calculations use sample variance/denominators directly (`services/query/app/models/statistics.py:68-120`). It also extracts p-values with truthiness, so exact `0.0` becomes `None` (`services/query/app/routers/experiments.py:148-161`). Reproduction produced NaN statistics for one observation/equal constant arms and `is_significant=true` with a missing exact-zero p-value.

**Impact.** Results can 500 during JSON serialization or contradict themselves, and Agents can consume these values for stopping/ship decisions.

**Solution.** Require minimum observations/variance, reject non-finite output, test `None` explicitly rather than truthiness, and return a strict typed `insufficient_data` state.

---

## 6. Agents Service

### Relevance and completeness

Agents implement APDL's autonomous differentiator and have extensive graph, tool, safety, approval, and provider-routing code. Lease-scoped startup recovery is now a meaningful strength, but FastAPI background tasks still stand in for durable jobs, safety state is process-local, and some claimed workflows terminate at placeholders.

### APDL-AUD-038 — Resolved critical: replica startup previously corrupted live work

**Baseline problem.** A new replica used to mark every active run failed and reopen implementing proposals.

**Resolution.** Commit `92ab654` added worker ownership, renewable leases, heartbeats, fencing, and expired-lease recovery. Startup now recovers only expired leases (`services/agents/app/main.py:36-57`), with extensive multi-worker tests.

**Remaining boundary.** Run creation/execution remains non-durable under APDL-AUD-039, and safety quotas remain process-local under APDL-AUD-040.

### APDL-AUD-039 — High: job creation races and execution is not durable

**Problem.** `services/agents/app/routers/triggers.py:105-163` checks for an active run and inserts using separate operations without a database constraint/lock, then executes using FastAPI `BackgroundTasks`.

**Impact.** Concurrent triggers can both pass; deploys/crashes lose jobs; retries and resume semantics are undefined.

**Solution.** Use a transaction plus project advisory lock or partial unique constraint, enqueue through an outbox, and execute in idempotent leased workers.

### APDL-AUD-040 — High: safety limits are process-local and conflict validation is a placeholder

**Problem.** `services/agents/app/safety/validator.py:40-51,295-372` stores rate state in a module dictionary, and the experiment-conflict check does not query Config authoritatively.

**Impact.** Restarting resets limits, replicas multiply limits, and conflicting automated mutations can pass validation.

**Solution.** Enforce transactional quotas in a shared store and perform conflict/version checks inside Config's authoritative mutation transaction.

### APDL-AUD-041 — High: failed experiment actions are recorded as applied

**Problem.** `services/agents/app/graphs/experiment_evaluation.py:318-369` records individual failures but unconditionally sets the action bundle's `applied` flag for ship, rollback, and iterate branches.

**Impact.** Audit and downstream work can claim success after stopping, disabling, or reverting failed.

**Solution.** Derive applied state from all required results, persist partial/retryable state, and gate downstream durable-feature work on confirmed mutations.

### APDL-AUD-042 — High, partially mitigated: provider fallback can cross tenant/vendor boundaries

**Problem.** Provider/model success and failure are now logged, but `services/agents/app/llm/router.py:97-135,269-284,600-632` can still replay the same messages across OpenAI, Anthropic, Google, and local providers after failure without a per-project residency policy.

**Impact.** Analytics, memory, source context, or PII can move to a vendor the tenant did not select.

**Solution.** Add per-project provider allowlists/residency, explicit cross-provider fallback opt-in, prompt classification/redaction, and durable provider/token/cost records.

### APDL-AUD-043 — Resolved high: embedding migration no longer deletes tenant memory at startup

**Baseline problem.** Agents startup previously deleted all memory when the configured vector dimension changed.

**Resolution.** Agents is now schema-validation-only at startup. Migration 004 preserves old embeddings in a legacy table before installing the canonical vector width (`pipeline/postgres/migrations/004_agents_core.sql:143-177`).

**Follow-up.** Future model changes still need explicit model/version routing and backfill rather than an in-place semantic conversion.

### APDL-AUD-044 — Medium: dependency outages can become successful no-op runs

**Problem.** Experiment listing/result failures are converted to empty arrays/objects in the evaluation graph.

**Impact.** A service outage is indistinguishable from no work and can produce a misleading successful run.

**Solution.** Model `no_data` separately from `dependency_unavailable`; mark the latter retryable/degraded and emit operational alerts.

### APDL-AUD-045 — Medium, partially mitigated: proposal fallback helpers are not fully tenant-scoped

**Problem.** Final mutations are now bound to `claim_run_id`, but `get_proposal` remains globally keyed and the approval fallback uses it without project scope (`services/agents/app/store/proposals.py:247-303`, `services/agents/app/routers/approvals.py:696-704`).

**Impact.** A future ID collision or route regression can update another project because the storage authority is not composite.

**Solution.** Make `(project_id, proposal_id)` the authority key and require project scope on every storage operation.

### APDL-AUD-046 — Medium: safety-critical audit persistence fails open

**Problem.** `services/agents/app/safety/audit.py:70-96` returns `-1` after persistence failure and lets the action continue.

**Impact.** A mutation can occur without its required audit evidence.

**Solution.** Commit mutation and audit/outbox together, or fail closed for safety-critical actions when audit durability is unavailable.

### APDL-AUD-047 — High completeness gap: scheduling and personalization are not end-to-end

**Problem.** Scheduled/threshold trigger types have no internal scheduler/alert integration, and personalization is explicitly disabled because Config has no UI-configuration API.

**Impact.** Product descriptions overstate autonomous operation.

**Solution.** Implement and test the external orchestration/delivery contracts, or mark these modes unsupported and remove them from release claims.

### APDL-AUD-111 — High: ship verdicts can be consumed before proposals are durable

**Problem.** Feature-proposal generation marks selected ship verdicts consumed before proposal results and the supervisor's waiting-approval state are durably persisted (`services/agents/app/graphs/feature_proposal.py:233-260`, `services/agents/app/graphs/supervisor.py:365-410`).

**Impact.** A crash or persistence failure after consumption can permanently drop a successful experiment outcome instead of producing a proposal.

**Solution.** Transactionally persist proposal/outbox state and then consume the verdict with one idempotency key.

---

## 7. Codegen Service

### Relevance and completeness

Codegen is APDL's implementation arm. Its strongest area is control-plane modeling: publication is off by default, rollout stages bind evidence to model/revision, GitHub remains CI/review/merge authority, no merge endpoint exists, exact-head observations are journaled, and repair loops are bounded. Docker isolation, project-scoped APDL authentication, verified repository grants, immutable repository snapshots, repository-scoped token leases, and operator/tenant safety separation are material improvements. The remaining release boundary is deployability and failure-proof publication, not a known direct tenant authority bypass.

### APDL-AUD-109 — Resolved critical: GitHub repository authority was not bound to the APDL project

**Baseline problem.** A project principal with `agents:manage` could bind an arbitrary repository visible to the shared GitHub App, including by supplying an installation ID. APDL project authority therefore incorrectly implied GitHub repository authority.

**Resolution.** Commit `d095776` removes repository slugs and installation IDs from the tenant connection contract. A trusted operator CLI now resolves the repository through GitHub, records immutable numeric installation/repository authority plus evidence in `codegen_repository_grants`, and activates one grant for the project. Tenant routes can inspect the public grant identity and manage preferences but cannot choose, replace, or disconnect the target. Migration 009 quarantines legacy bindings as `pending_reauthorization`. Codegen is no longer published on a host port in Compose (`services/codegen/app/models/connection.py`, `services/codegen/app/github/grant_cli.py`, `pipeline/postgres/migrations/009_codegen_repository_authority.sql`).

**Remaining boundary.** The operator-grant flow and two-project isolation are unit-tested but were not exercised against a real GitHub App. GitHub OAuth onboarding is represented in the strict authorization-source enum but not implemented; the OSS path is operator CLI only.

### APDL-AUD-110 — Resolved critical: tenant-managed policy could disable Codegen safety floors

**Baseline problem.** The tenant connection accepted an unrestricted dictionary that safety gates trusted for protected paths, allowlists, and file/line ceilings. The tenant could weaken controls described as platform guarantees.

**Resolution.** Commit `a6d51e8` introduces one strict, versioned `TenantCodegenConnectionPolicy`, a separate operator-loaded `PlatformCodegenSafetyPolicy`, and an immutable effective policy resolved by union/min/intersection rules. Tenants can only tighten size ceilings, add protected paths, choose a test command, and opt into runtime acceptance that the platform must also grant. Raw tenant dictionaries are rejected at the gate boundary, and migration 008 discards legacy safety overrides rather than preserving them (`services/codegen/app/safety/policy.py`, `services/codegen/app/safety/gates.py`, `pipeline/postgres/migrations/008_codegen_safety_policy.sql`).

**Remaining boundary.** The platform override is a local read-only JSON file, so multi-replica operators must distribute exactly the same content. There is no central policy revision/status surface or live rollout proof.

### APDL-AUD-048 — Substantially resolved critical: default execution is now isolated

**Baseline problem.** The default editor ran customer repository tooling inside the credential-bearing API process.

**Resolution.** Commit `510b257` made Docker isolation the default, removed Aider from the API image, disabled repository-defined tooling in the worker, and made PR stages fail closed unless Docker plus an operator-named network are configured (`services/codegen/app/main.py:75-104`, `services/codegen/app/editor/container_editor.py:131-207`).

**Remaining boundary.** APDL-AUD-120 now provides a runnable `make dev-all` worker path, but only through an explicit development overlay with development-only Docker socket, network, and draft-publication authority. No live Docker/Aider/GitHub/provider path was exercised. A production design should replace direct API access to the Docker socket with a separate worker launcher and short-lived brokered credentials.

### APDL-AUD-049 — Resolved critical: service authority is project/role scoped

**Baseline problem.** Codegen used one global internal bearer token.

**Resolution.** Commit `5674b96` moved Codegen to the shared PostgreSQL credential registry and verifies project plus role on every `/v1` resource. Admin and Agents now send project-scoped credentials, and cross-tenant child routes have negative tests.

**Repository boundary.** APDL project authority is now complemented by the separately verified repository-grant contract in APDL-AUD-109.

### APDL-AUD-050 — High: kill switches do not reach Compose or active repair paths

**Problem.** `.env.example` and the Codegen README advertise `CODEGEN_KILL_SWITCH` and `CODEGEN_DISABLED_PROJECTS`, but Compose does not forward them. The check occurs only at initial changeset job entry, not before all poller/repair GitHub writes.

**Impact.** The documented operator control may have no effect, and an existing PR repair can continue pushing after a kill request.

**Solution.** Put the switch in an externally mutable authoritative store, wire every deployment, check immediately before every mutation, cancel queued/active jobs safely, and expose audited status.

### APDL-AUD-051 — Resolved high: GitHub installation tokens were not repository-restricted

**Baseline problem.** Codegen requested installation-wide tokens without an exact repository ID or fixed least-privilege permission body.

**Resolution.** Commit `d095776` adds a `GitHubTokenBroker` that resolves only active verified grants, requests exactly one numeric repository plus fixed read/write permission profiles, validates GitHub's returned repository and permissions, revalidates the grant after minting, enforces a write-token lifetime budget, and revokes leases on exit (`services/codegen/app/github/app_auth.py`, `services/codegen/app/github/token_broker.py`).

**Remaining boundary.** Tokens still enter the isolated worker for clone/push and provider credentials enter the same worker. The stronger long-term design remains a trusted clone/push broker plus narrower task-specific credentials.

### APDL-AUD-052 — High: PR publication is not idempotent across GitHub and PostgreSQL

**Problem.** `services/codegen/app/jobs/runner.py:428-473` creates a GitHub PR before persisting its number. Failure between those operations leaves an unrecorded PR; retry logic can create another.

**Impact.** Orphaned branches and duplicate PRs are possible after an ordinary network/database failure.

**Solution.** Persist publication intent, use a deterministic head, reconcile by head before POST/retry, and recover `pushing` rows explicitly.

### APDL-AUD-053 — Resolved high: mutable connections could change an existing changeset's repository identity

**Baseline problem.** Connections overwrote repository/installation in place while later CI/repair work resolved the current connection, so an existing changeset could drift to another repository.

**Resolution.** Migration 009 and commit `d095776` snapshot grant ID, repository ID, installation ID, and full name on every changeset. Token leasing and webhook/CI routing resolve the immutable changeset target and require its grant to remain active. Activating a new project connection does not retarget existing work (`services/codegen/app/store/changesets.py`, `services/codegen/app/store/connections.py`).

**Remaining boundary.** Revoking the grant intentionally stops subsequent work for old changesets. The operator runbook must make that effect explicit and provide reconciliation for already-open GitHub PRs.

### APDL-AUD-054 — High: secret scanning is too narrow

**Problem.** `services/codegen/app/safety/gates.py:18-49` checks a small path/pattern set and misses common credentials. The separate artifact redactor already knows more patterns.

**Impact.** Generated commits can publish credentials while still passing the claimed pre-push gate.

**Solution.** Scan the complete diff/tree with a maintained scanner such as Gitleaks, include entropy/binary checks and protected security paths, and add representative leak fixtures.

### APDL-AUD-055 — High, partially mitigated: real sandbox path is not integration-tested

**Problem.** Aider is now pinned and sandbox construction is unit-tested, but the real Docker worker/GitHub App/provider/PR path has never been exercised by repository CI, and base/tool dependencies still build from mutable upstreams.

**Impact.** The most privileged path can change underneath a release and has no acceptance evidence in a disposable private repository.

**Solution.** Pin/lock Aider and sandbox images by digest, test a real GitHub App flow in an isolated disposable repository, and capture the exact toolchain in rollout evidence.

### APDL-AUD-056 — Resolved medium: schema authority and tracked shutdown are fixed

**Baseline problem.** Codegen previously mixed runtime DDL with startup and did not drain all tracked requeued/repair tasks before closing PostgreSQL.

**Resolution.** The service validates the checksummed migration ledger without mutating schema, retains references to poller, sweeper, repair, and requeued tasks, cancels and awaits them while the token broker and PostgreSQL pool are still available, and only then closes the pool (`services/codegen/app/main.py:128-278`). Cancellation tests cover isolated-container cleanup and token/PR projection behavior.

**Remaining boundary.** New request-triggered jobs are still in-process rather than a durable external queue; APDL-AUD-039's broader job durability concern still applies across the autonomous pipeline.

### APDL-AUD-057 — Medium: readiness leaks exception text

**Problem.** `services/codegen/app/main.py:313-329` returns `str(exc)` from an unauthenticated readiness response.

**Impact.** Connection/schema details may be disclosed to network observers.

**Solution.** Log structured internal details and return a generic public failure body.

### APDL-AUD-112 — High: publication evaluation bundles lack trusted provenance

**Problem.** The Codegen CLI can load arbitrary syntactically valid `EvaluationRun` input via `--results`, calculate reports, and emit a rollout authorization bundle (`services/codegen/app/evaluations/cli.py:58-79,83-132`, `services/codegen/app/evaluations/publication.py:41-52,126-136`). Hashes prove bundle consistency, not that the trusted harness produced the observations.

**Impact.** Self-supplied results can satisfy a gate described as evidence-backed and enable a PR rollout stage.

**Solution.** Emit publication bundles only from a just-completed trusted executor, or require signed CI/operator attestations binding corpus, executor/image digest, model, revision, and raw observations. Remove or constrain authorization generation from `--results`.

### APDL-AUD-113 — High: GitHub pagination can forward an installation token cross-origin

**Problem.** Checks pagination follows the `Link: rel=next` URL verbatim while reusing the authorization header (`services/codegen/app/github/checks.py:90-103`). The artifact client already implements a same-origin guard at `services/codegen/app/github/artifacts.py:88-101`.

**Impact.** A malicious or compromised GitHub-compatible endpoint can redirect the next page to another origin and receive the installation token.

**Solution.** Resolve and validate every pagination URL against the configured GitHub API origin before sending credentials.

### APDL-AUD-120 — Partially resolved high: `make dev-all` can launch a development sandbox

**Baseline problem.** Docker is the default editor, but the base Compose stack neither mounted a Docker control channel nor built/provisioned the configured worker image. Startup/readiness validated PostgreSQL only, not the daemon, image, network, GitHub App, or model credentials. The canonical full-stack command could therefore report Codegen ready and accept work that could not run.

**Resolution.** The `make dev-all` path now applies an explicit development-only Codegen overlay. It builds the checked-in worker, supplies a local Docker launcher through the host socket, attaches sandboxes to a dedicated development network, and installs development authorization restricted to opening draft PRs. Startup preflight rejects missing or incompatible launcher, socket, worker image, network, and authorization dependencies before Codegen accepts work. Focused tests cover the rendered Compose contract, worker build/identity, preflight failures, and the draft-only authorization boundary.

**Remaining boundary.** This is a runnable local-development contract, not a production sandbox design. Mounting the Docker socket gives the Codegen controller host-root-equivalent authority, the development network is not evidence of a real egress filter, and development authorization must never authorize non-draft or production rollout. Production still requires a separate constrained launcher/worker service, short-lived brokered credentials, dependency-specific readiness, and live Docker/Aider/GitHub/provider proof.

---

## 8. Admin API

### Relevance and completeness

The Admin API is the human security boundary. Its foundation is strong: HttpOnly/SameSite sessions, CSRF/origin enforcement, credential stripping, project roles, mutation audit, and service-specific proxy rules. The three prior criticals below are resolved. Remaining risk is concentrated in public self-service resource authority, dependency/readiness behavior, and the maintenance burden of a manual proxy route map.

### APDL-AUD-058 — Resolved critical: Codegen JSON media-type bypass

**Baseline problem.** Noncanonical JSON media types could bypass proxy body scoping.

**Resolution.** Commit `c8835d1` makes Codegen bodies fail closed unless the canonical media type is `application/json`, with negative tests for merge-patch/vendor/text JSON.

**Defense in depth.** Codegen now also validates project-scoped authority upstream.

### APDL-AUD-059 — Resolved critical: Codegen child routes bypassed project ownership

**Baseline problem.** Observation and runtime-observation child routes were missing from project ownership checks.

**Resolution.** Commit `c6b8a52` scopes every route rooted at a changeset, including future child resources; current Codegen also binds changeset project to the authenticated principal. Cross-tenant detail and child-route tests pass.

**Follow-up.** Keep the deny-by-default route map synchronized or generate it from a shared API contract.

### APDL-AUD-060 — Resolved critical: login lockout state was rolled back

**Baseline problem.** Login raised inside the transaction that wrote failed-attempt/lockout state, rolling the update back.

**Resolution.** Commit `bdfb341` raises only after the transaction commits (`services/admin-api/app/auth.py:178-244`) and adds repeated-failure/lock-expiry coverage.

**Follow-up.** The front nginx adds IP throttling to login/registration; production ingress must preserve equivalent controls.

### APDL-AUD-061 — High: SSE proxy timeout races the normal heartbeat

**Problem.** The Admin API's upstream read timeout is 30 seconds in `services/admin-api/app/main.py:19-21`; Config's normal heartbeat is also 30 seconds and its fallback can be 35 seconds.

**Impact.** A healthy idle stream can be terminated immediately before its heartbeat, causing reconnect churn and update gaps.

**Solution.** Use a stream-specific infinite or comfortably larger read timeout and test multiple idle heartbeat intervals.

### APDL-AUD-062 — High: readiness verifies only PostgreSQL

**Problem.** `services/admin-api/app/main.py:56-65` can report ready without validating required internal credentials/configuration or critical upstream reachability.

**Impact.** An operator can route users to a console that authenticates but cannot perform its core work.

**Solution.** Validate mandatory startup configuration and expose dependency-specific readiness without leaking secrets. Treat optional upstreams explicitly.

### APDL-AUD-063 — Medium: generated curl commands cannot replay protected mutations

**Problem.** `services/admin/src/api/http.ts:68-85` generates curl with a session cookie and CSRF header but omits the matching CSRF cookie and required Origin checked by the Admin API.

**Impact.** Debug tooling presents a command that fails even when copied exactly.

**Solution.** Generate a complete safe command or omit mutation curl generation and explain browser-only session requirements.

### APDL-AUD-114 — High: self-service projects have no resource or LLM-spend quota

**Problem.** Registration is public and new users can call `POST /api/projects`; each project grants the full creator role set including `agents:run/manage/approve` (`services/admin-api/app/auth.py:258-298`, `services/admin-api/app/projects.py:15-25,28-74`). The proxy mints working project credentials on demand, but project creation and Agents triggers have no per-account/project/cost entitlement. The nginx rate limit covers login/registration only.

**Impact.** A user can create many projects and repeatedly invoke globally funded LLM workflows under fresh per-project safety counters. This creates an abuse/cost boundary even though new accounts correctly start with zero access.

**Solution.** Add explicit deployment-level registration policy, verified account/tenant entitlement, project-count and LLM budget quotas, shared rate enforcement, and operator controls. Keep the minimal zero-project registration contract; do not silently grant funded runtime capacity merely because a project name was created.

---

## 9. Admin UI

### Relevance and completeness

The React admin console is the principal operator surface and has strong Zod validation, CSP through its nginx image, broad unit coverage, and no dangerous raw HTML rendering. It is usable after backend boundary fixes, but tenant switching and degraded-auth behavior need hardening.

### APDL-AUD-064 — High: workspace switches can retain stale tenant state

**Problem.** Workspace-scoped state is initialized only once in components such as `services/admin/src/features/analytics/SavedViews.tsx:32-61` and `services/admin/src/features/experiments/ExperimentResultsTab.tsx:38-53,122-134`. On a workspace switch, `SavedViews` keeps the previous list and experiment inputs can be written into the new workspace key before being reset.

**Impact.** Switching workspaces can display or persist state derived from the previous project, creating operator confusion and possible wrong-tenant mutations.

**Solution.** Key all workspace state/query caches by project, reset local state on workspace change, cancel stale requests, and add rapid-switch tests.

### APDL-AUD-065 — Medium: non-auth failures from `/me` are treated as logout

**Problem.** `services/admin/src/core/auth.tsx:58-70` maps network, 5xx, and response-schema errors to the same logged-out state as 401.

**Impact.** A transient backend outage looks like session expiration and loses useful recovery/error context.

**Solution.** Distinguish unauthenticated, authenticated, loading, and temporarily unavailable states with retry.

### APDL-AUD-066 — Medium: external Codegen URLs are not consistently constrained

**Problem.** Several Codegen Zod schemas accept arbitrary URL strings, and observation/changeset pages render them as links.

**Impact.** A compromised/misconfigured backend can place unsafe or deceptive external destinations in a privileged operator UI.

**Solution.** Require HTTPS and expected GitHub host/repository relationships before rendering clickable links.

### APDL-AUD-067 — Medium: production bundle is oversized

**Problem.** The current build produced one 1,384.47 kB minified / 386.54 kB gzip JavaScript bundle; `services/admin/src/router.tsx:1-35` eagerly imports all routes.

**Impact.** Slow first load and unnecessary code exposure for users who need only a subset of the console.

**Solution.** Lazy-load route groups and split heavy visualization/editor dependencies; set a CI bundle budget.

---

## 10. Redis-to-ClickHouse Writer

### Relevance and completeness

The writer is the only live persistence bridge between accepted ingestion events and analytics. It is indispensable. The recent durability work is substantial: ACK ordering, stale pending recovery, backlog consumption, tenant authority, safe DLQ handling, and 21 focused tests now exist. Remaining blockers are end-to-end idempotency, projection correctness, retention/backpressure, health/lag observability, and the disconnected legacy/v2 split.

### APDL-AUD-068 — Resolved critical: messages were ACKed before ClickHouse insert

**Baseline problem.** The writer acknowledged Redis before the in-memory buffer reached ClickHouse.

**Resolution.** Commit `b281bc0` retains stream IDs with the buffered rows and ACKs only after successful ClickHouse insertion. Failure keeps the messages pending for retry.

**Remaining boundary.** Without storage idempotency, a crash after ClickHouse commit but before Redis ACK can duplicate rows; see APDL-AUD-071.

### APDL-AUD-069 — Resolved critical: stale pending messages were not reclaimed

**Baseline problem.** A new consumer could not recover another crashed consumer's pending entries.

**Resolution.** Commit `3c2115c` adds idle-threshold reclaim via Redis claim semantics plus focused recovery tests.

**Follow-up.** Expose pending age/count and stale consumer cleanup as readiness/operational metrics.

### APDL-AUD-070 — Resolved critical: new groups skipped preexisting backlog

**Baseline problem.** New groups started at `$` and skipped events produced before group creation.

**Resolution.** Commit `56f23a8` creates groups from the backlog and adds a smoke-contract test. The remaining root smoke script is still broken for different strict-schema/project reasons under APDL-AUD-085.

**Follow-up.** The end-to-end smoke must send one event once and prove exact-once logical results after idempotency is added.

### APDL-AUD-104 — Resolved critical: embedded stream data overrode tenant authority

**Baseline problem.** An embedded project value could override the project encoded in the authoritative stream name.

**Resolution.** Commit `766f051` derives authority from the validated stream name and rejects conflicting embedded values, with tenant-conflict tests.

**Follow-up.** Restrict Redis producer permissions by stream prefix where the deployment supports ACLs.

### APDL-AUD-071 — High: at-least-once recovery has no storage idempotency

**Problem.** The legacy `events` table in `pipeline/clickhouse/migrations/001_events.sql` generates a random UUID and stores no Redis message/idempotency ID.

**Impact.** Correcting ACK/reclaim behavior to at-least-once delivery will create duplicates after ambiguous inserts.

**Solution.** Persist a stable message ID and use an idempotent insert/deduplication strategy appropriate to ClickHouse, with replay tests around crash boundaries.

### APDL-AUD-072 — Resolved high: malformed records lacked a DLQ

**Baseline problem.** Parse/conversion failures were logged and acknowledged without a recovery artifact.

**Resolution.** The writer now writes bounded safe metadata and reason to a DLQ before acknowledging poison records; focused tests cover parse/conversion failure.

**Follow-up.** Add alerts, tenant visibility, retention policy, and controlled replay tooling.

### APDL-AUD-073 — High: the live writer ignores the advertised v2 pipeline

**Problem.** Migration `008_events_v2.sql` calls for dual-write, but `clickhouse_writer.py:324-337` inserts only legacy `events`; Query reads legacy tables.

**Impact.** The canonical envelope/v2 claims do not describe live data.

**Solution.** Do not call v2 canonical until dual-write, backfill, parity monitoring, and Query cutover are implemented. Otherwise remove the unused schema from release scope.

### APDL-AUD-074 — High: materialized unique-user views are mathematically invalid

**Problem.** `pipeline/clickhouse/migrations/004_materialized_views.sql:4-26` stores scalar `uniq(user_id)` values in `SummingMergeTree` and ignores anonymous identity.

**Impact.** Summing partial unique counts double-counts actors across inserted blocks and excludes anonymous users.

**Solution.** Use `AggregatingMergeTree` with `uniqState`/`uniqMerge` over the canonical actor identity, then rebuild affected aggregates.

### APDL-AUD-075 — High: exposure replacement can erase first assignment semantics

**Problem.** `pipeline/clickhouse/migrations/006_feature_flag_exposures.sql` uses `first_exposure` as the `ReplacingMergeTree` version. Background merges retain the latest version while downstream semantics seek the first assignment.

**Impact.** Experiment assignment time and variant evidence can change after merges.

**Solution.** Store an immutable assignment/message ID and aggregate the earliest valid assignment explicitly; never use the timestamp whose minimum matters as a latest-row replacement version.

### APDL-AUD-076 — Partially resolved high: writer has tests but no health/lag surface

**Problem.** The writer now has 21 focused unit/contract tests, but exposes no health/readiness/lag/DLQ metrics endpoint. Compose defines no writer healthcheck.

**Impact.** Operators cannot distinguish a live process from a stalled pipeline or gate traffic on pending age/retention risk; real Redis/ClickHouse crash boundaries remain unverified.

**Solution.** Add liveness/readiness plus lag/pending/flush/DLQ metrics and live Redis/ClickHouse tests for claim, crash, ambiguous insert, and replay.

### APDL-AUD-105 — Medium: synchronous ClickHouse calls block the async consumer loop

**Problem.** The writer calls the synchronous ClickHouse client directly from its asyncio loop.

**Impact.** A slow insert blocks Redis reads, timed flush progress, and graceful signal handling, worsening lag exactly when storage is degraded.

**Solution.** Use an async client or execute inserts through a bounded worker pool with cancellation, timeout, and backpressure.

### APDL-AUD-077 — Medium: approximate stream trimming is not tied to readiness or lag

**Problem.** Streams use an approximate maximum length while the writer has no lag-age release gate.

**Impact.** A prolonged outage can trim unprocessed history silently.

**Solution.** Size retention by time/throughput, alert well before trimming, and fail producer readiness or spill durably when loss is imminent.

---

## 11. ETL, Kafka, and Flink Surfaces

### Relevance and completeness

The ETL package expresses a useful future canonical-envelope direction and has 45 tests. It is not part of the current runtime: the writer does not import it, no loader/entry point/Compose service consumes live data through it, and Kafka/Flink directories are future scaffolds. They should not be counted as supported components in the initial release.

### APDL-AUD-078 — High: canonical project ID type contradicts live authority

**Problem.** PostgreSQL auth and legacy events use text/alphanumeric project IDs. `events_v2`, `decisions_v2`, `feeds_v2`, `pipeline/etl/etl/envelope.py`, and Agent/Config envelope models use integer/`UInt32` IDs.

**Impact.** A normal project such as `apdl` cannot enter the purported canonical envelope without an undocumented mapping.

**Solution.** Choose one canonical `String` project ID everywhere, or introduce one explicit immutable numeric surrogate mapping at the authority boundary and migrate every producer/consumer together. Do not accept both shapes.

### APDL-AUD-079 — High: ETL architecture claims are not wired to production

**Problem.** Documentation says the same transforms run for live, backfill, and replay, but the live Redis writer has a separate transformation and writes only legacy events.

**Impact.** Backfills/replays can produce semantics different from live traffic, and maintained-looking code is effectively dead.

**Solution.** Make the ETL library the single transform used by the live writer and backfill jobs, or label/remove it until that integration exists.

### APDL-AUD-080 — Medium: Kafka/Flink code is aspirational

**Problem.** No default runtime, build/release artifact, CI job, deployment, or end-to-end test connects Kafka/Flink scaffolds.

**Impact.** Repository structure overstates supported scale paths and increases maintenance/security surface.

**Solution.** Move future designs to a clearly labeled experimental area or complete a supported runnable path with ownership and tests.

---

## 12. SDK Gateway

### Relevance and completeness

`infra/docker/gateway/nginx.conf` is a useful local single endpoint for `/v1/events`, `/v1/flags`, and `/v1/stream`, and it correctly disables SSE buffering. It is a Compose convenience, not a production edge.

### APDL-AUD-081 — High if presented as production: gateway lacks production controls

**Problem.** The gateway has no TLS termination, authentication policy of its own, request/body limit, rate limiting, structured access/redaction policy, upstream health-based readiness, or Kubernetes equivalent. Its `/` liveness returns success without checking Ingestion/Config. Compose publishes it on all host interfaces.

**Impact.** Operators may mistake a local nginx router for the supported secure ingress and expose internal defaults or unbounded ingestion directly.

**Solution.** Label it local-development-only. For production, specify a supported ingress contract with TLS, trusted proxy handling, byte/rate limits, redacted logs, upstream readiness, and immutable image configuration.

---

## 13. Database Contracts and Migrations

### Relevance and completeness

Database initialization is part of the product installation contract. PostgreSQL now has a real contiguous, checksummed, immutable-ledger migration authority and Config/Agents/Codegen validate rather than mutate schema at startup. ClickHouse remains a replay-every-file shell path with no ledger, safe upgrade/backfill contract, or Query schema readiness.

### APDL-AUD-082 — Resolved critical: PostgreSQL migrations were skipped

**Baseline problem.** PostgreSQL migrations were parked in the ClickHouse directory and skipped by both database runners.

**Resolution.** Commit `f5b41e6` moved canonical PostgreSQL schema to `pipeline/postgres/migrations/001-007`, added an exact-prefix/checksum/immutable-ledger/advisory-lock runner in `pipeline/postgres/migrate.py`, preserved legacy incompatible data explicitly, gated Compose services on migration completion, and removed runtime DDL from Config/Agents/Codegen.

**Follow-up.** Add live fresh-install and supported-upgrade fixtures; current tests largely validate plans/SQL with fakes.

### APDL-AUD-083 — Partially resolved high: observability schema exists; decision spine remains dead

**Problem.** Migration 005 now provides compatible text-project observability tables, resolving the misplaced/incompatible PostgreSQL half. Config's `DecisionEnvelope` still requires integer project IDs and claims publication to unused `decisions_v2`; runtime Config/Writer/Query do not use that decision spine.

**Impact.** Audit/cost/compliance claims are not backed by persisted runtime evidence, and the supplied migration cannot establish them.

**Solution.** Either remove the unused contract from the release or implement it through the canonical Postgres migration authority, router instrumentation, an outbox, retention/redaction policy, and end-to-end tests.

### APDL-AUD-084 — Resolved high: services mutated schema at startup

**Baseline problem.** Config, Agents, and Codegen executed DDL during process startup, and Agents could delete memory during a dimension change.

**Resolution.** All three services now assert the expected migration-owned schema. The PostgreSQL migrator owns DDL and preserves legacy vector data.

**Follow-up.** Use distinct least-privilege database roles in deployment and prove upgrade/rollback operations.

### APDL-AUD-115 — High: ClickHouse migrations have no migration authority

**Problem.** `scripts/init-clickhouse.sh:65-78` replays every SQL file on every run with no version/checksum ledger, deployment lock, or exact-prefix validation. Migration 003 includes unconditional retirement drops; most later `CREATE ... IF NOT EXISTS` statements do not upgrade incompatible existing tables, and materialized views receive no historical backfill. Query startup does not validate required schema.

**Impact.** Fresh installs can work while upgrades preserve stale shapes, repeat destructive operations, or silently omit historical projections.

**Solution.** Add a checksummed exactly-once ClickHouse ledger and lock, explicit forward-only ALTER/backfill migrations, schema readiness in Query, and live upgrade fixtures.

### APDL-AUD-123 — High: Config upgrade can silently replace legacy flag meaning

**Problem.** PostgreSQL migration 006 rewrites invalid/unmappable variant data to deterministic control/treatment defaults and later drops legacy columns (`pipeline/postgres/migrations/006_config.sql:88-129,203-210`). It preserves a separate legacy `feature_flags` table, but does not create a complete per-row backup for the existing `flags` rows whose meaning is rewritten.

**Impact.** Upgrading an existing pre-canonical deployment can silently change who receives which experience without an operator reconciliation report.

**Solution.** Preflight and abort on unmappable rows, preserve a complete backup, emit an explicit mapping/report, and test real legacy upgrade fixtures before accepting the migration as supported.

---

## 14. Docker Compose and Local Developer Experience

### Relevance and completeness

Compose is the most realistic first OSS installation path and should be the highest-priority supported deployment. It includes all major processes, but the documented first run, environment loading, and security boundaries do not yet form one reliable contract.

### APDL-AUD-085 — High: public quick starts and smoke tests are stale

**Problem.** Root JS documentation passes the removed `endpoints` field. Examples hard-code an unprovisioned `proj_demo...` key while `.env.example` provisions project `apdl`, use the obsolete `fallthrough.value` flag shape, and omit required `default_variant`/`variants`. The browser example starts a file server on gateway port 8000 and serves the repo root (including potential `.env`). `scripts/dev.sh` uses the same obsolete flag body and then queries project `demo` with the `apdl` credential. Direct Pydantic validation of its flag payload reports three strict-schema errors.

**Impact.** A new contributor following the canonical path cannot reproduce the advertised product and may expose secrets or send data to the wrong endpoint.

**Solution.** Generate one canonical demo project/credential/config, use the strict current schemas, serve only the example directory on loopback and a distinct port, make local endpoints explicit, and require the first event to arrive exactly once.

### APDL-AUD-086 — High: setup omits Codegen environment and dependency contracts

**Problem.** `scripts/dev.sh:123-130` does not create the Codegen virtual environment although run targets expect it. Several hot-reload commands do not load root `.env`; Compose Agents omits Google/local LLM variables supported by source. APDL-AUD-120 partially resolves the Docker full-stack path with an explicit development worker overlay, but the general setup and environment contracts remain inconsistent.

**Impact.** A successful-looking setup can leave advertised services unrunnable or differently configured inside/outside Compose.

**Solution.** Define one generated environment contract, set up every supported service, validate required variables at startup, and run a fresh-checkout smoke in CI.

### APDL-AUD-087 — High: Compose defaults are unsafe as production guidance

**Problem.** Databases and services bind all interfaces with known development credentials; internal token and insecure admin cookie defaults are predictable; images/dependencies use mutable tags or lower bounds; startup relies partly on ordering rather than complete health/readiness.

**Impact.** Copying the Compose file to a reachable host creates an insecure, irreproducible deployment.

**Solution.** State that the current file is local-only, bind dependencies to loopback by default, add complete health gates, use lockfiles/digests, and provide a separately hardened production example only after threat-model review.

---

## 15–16. Kubernetes and Terraform — removed from release scope

The obsolete Kubernetes and Terraform implementations audited at `294584a` were deleted by commit `5287078`. APDL-AUD-088 through APDL-AUD-092 are therefore **resolved by removal**, not by producing a working deployment. This is the correct current scope: the repository contains only Docker Compose infrastructure and must not claim Kubernetes, Terraform, managed cloud durability, backup/restore, or production ingress support. Any future reintroduction needs to be designed from the tested runtime contracts and audited as a new product surface.

---

## 17. CI, Packaging, and Release Automation

### Relevance and completeness

An OSS release is only reproducible if the tested commit is the artifact that reaches registries. Current local suites are much stronger than current GitHub Actions: CI omits most Python tests and several services, while release publishes only part of the stack without depending on a complete test gate.

### APDL-AUD-093 — High: CI omits tests for supported backend services

**Problem.** `.github/workflows/ci.yml` runs tests for both SDKs, Admin, Writer, and ETL, but Ingestion, Config, Query, and Agents are lint-only. Codegen is absent entirely, so its 532 tests and linter do not gate pull requests. Local `make test`/`make lint`, `make ci`, and the workflow are not one canonical matrix.

**Impact.** A PR can pass CI while breaking hundreds of tests or the most privileged service.

**Solution.** Define one canonical package/service matrix consumed by Make, local check scripts, and CI. Require lint and unit tests for every supported surface, including Codegen and Writer.

### APDL-AUD-094 — High: no cross-service or artifact-consumer release gate exists

**Problem.** CI has no fresh Compose data-flow test, migration upgrade test, packed Next/Vite/Python consumer test, real Redis/ClickHouse crash/replay test, Admin browser E2E/accessibility test, infrastructure validation, container scan, dependency/license scan, or secret scan.

**Impact.** The defects found in this audit remain invisible despite 1,824 passing unit tests.

**Solution.** Add staged gates: fast unit matrix; fresh-stack integration; crash/replay and multi-replica tests; packed consumer builds; infrastructure semantic checks; SBOM/vulnerability/license/secret policy.

### APDL-AUD-095 — High: tagged release delivers only a partial product

**Problem.** `.github/workflows/release.yml` publishes npm and four images (Ingestion, Config, Query, Agents). It omits Python SDK, Writer, Codegen API/worker, Admin API/UI, and Gateway; the Docker job lacks explicit package-write permission.

**Impact.** The release described by `CHANGELOG.md` cannot be installed from its released artifacts.

**Solution.** Declare the exact supported artifact manifest and publish every item atomically after the same commit's gates pass. Add PyPI trusted publishing and GHCR permissions.

### APDL-AUD-096 — High: version and channel policy are inconsistent

**Problem.** JavaScript is 0.2.0, Python is 0.1.0, and the changelog describes only 0.1.0. Any `v*` tag can publish `latest`, including prereleases, with no tag/package consistency check or required CI dependency.

**Impact.** Registries can contain mismatched versions or a prerelease can replace the stable channel.

**Solution.** Use a release manifest, validate tag/package/changelog versions, separate prerelease/stable tags, gate publication on all tests, and generate signed provenance, SBOMs, checksums, scans, and a GitHub release.

### APDL-AUD-097 — High: dependency and image builds are not reproducible

**Problem.** Python packages use broad lower-bound dependencies without checked lockfiles; setup/CI use `npm install`; Docker bases and NodeSource/tool inputs are mutable. Aider is now pinned in the worker, but the rest of the resolved graph and image bases are not reproducible.

**Impact.** Rebuilding the same tag later can produce different behavior and security posture.

**Solution.** Commit/freeze service lockfiles, use `npm ci`, pin base images and tools by digest/version, automate controlled updates, and attest the resolved dependency graph.

### APDL-AUD-122 — Medium: vulnerable development toolchains are not gated

**Problem.** Current `npm audit` reports 12 SDK and 6 Admin findings, including critical vulnerable Vitest lines and high Vite/transitive findings. `npm audit --omit=dev` reports zero in both packages, so these are not bundled runtime dependencies, but contributors and any exposed watch/UI tooling still execute them. CI has no dependency policy gate.

**Impact.** Developer/CI environments carry known vulnerabilities and upgrades can drift until a release is already being prepared.

**Solution.** Upgrade Vitest/Vite/Rollup tooling with compatibility tests, add automated dependency updates, and gate only actionable production/development severities with documented exceptions so the signal remains usable.

---

## 18. Documentation, Security Policy, and OSS Governance

### Relevance and completeness

MIT `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, and `CHANGELOG.md` exist, which is a sound OSS foundation. Documentation has drifted behind strict APIs and newly added Admin/Codegen architecture, and governance/support expectations are not defined.

### APDL-AUD-098 — High: primary documentation describes a different system

**Problem.** The root component tree still omits Codegen/Admin components, examples use removed schemas/credentials, personalization and shared ETL claims are incomplete, and README claims the workflow releases npm, PyPI, and Docker although Python is not published and the image set is partial. Kubernetes/Terraform are now correctly absent, but older enterprise design documents must not be confused with supported OSS deployment.

**Impact.** Users cannot distinguish supported runtime, experimental code, and design intent; security assumptions are easy to get wrong.

**Solution.** Rewrite the root quick start from a tested fresh clone, publish a current component/trust-boundary diagram, and label every surface as supported, experimental, future, or deprecated.

### APDL-AUD-099 — High: repository and security-reporting metadata is stale

**Problem.** README badges/clone commands, CONTRIBUTING, SECURITY advisory links, changelog links, Python metadata, and Admin package metadata still reference older repository ownership while the actual checkout remote differs.

**Impact.** Vulnerability reports and contributor traffic can go to the wrong project; package consumers see inconsistent provenance.

**Solution.** Choose the final OSS organization/repository identity and update every URL, package manifest, image name, badge, advisory route, and CODEOWNERS entry in one verified pass.

### APDL-AUD-100 — Medium: governance and support contracts are missing

**Problem.** The repository lacks CODEOWNERS, Code of Conduct, maintainer/governance model, support/version policy, and automated dependency-update policy. SECURITY covers an older release line and does not enumerate current high-risk surfaces.

**Impact.** Contributors and security reporters do not know ownership, response expectations, or supported versions.

**Solution.** Add minimal governance/maintainer/support documents, update SECURITY scope and response channel, define release support, and configure dependency automation.

---

## Existing strengths worth preserving

1. **Tenant-aware service authentication.** Ingestion, Config, Query, Agents, and Codegen derive project/roles from hashed PostgreSQL credentials rather than trusting a body project ID alone.
2. **Strict flag/config contracts.** SDK flag parsing rejects legacy/ambiguous fields and cross-language hash behavior has meaningful tests.
3. **Admin BFF architecture.** HttpOnly/SameSite sessions, CSRF/origin checks, stripped upstream credentials, per-project roles, mutation audit, and CSP are the right design. The prior media-type/child-route/lockout criticals are now fixed with negative tests.
4. **Codegen publication authority.** GitHub remains the CI, review, and merge authority; APDL has no merge endpoint. Publication is off by default, rollout stages require model/revision-bound evidence, exact-head observations are recorded, and repair loops are bounded.
5. **Substantial test investment.** The current 1,824 passing tests make refactoring safer and already catch many schema, migration, lease, durability, and state-machine regressions.
6. **Package/build baseline.** JavaScript SDK lint, tests, Rollup build, and npm pack succeed. Python lint, tests, coverage, and build succeed apart from release metadata/license packaging.
7. **Explicit scope cleanup.** Removing obsolete Kubernetes/Terraform implementations prevents broken deployment scaffolds from being mistaken for supported OSS infrastructure.
8. **PostgreSQL schema authority.** The new immutable, checksummed, lock-protected migration runner and schema-validation-only services are a strong foundation to retain.
9. **Writer recovery foundation.** ACK-after-insert, stale pending reclaim, backlog consumption, tenant authority, safe DLQ behavior, and focused tests materially reduce silent-loss risk.

## Recommended release plan

### Gate 0 — Decide and document the supported release

- Keep the current milestone private while the high-severity data, installation, quota, and release-contract defects remain; call the first public version a developer preview/alpha only after its advertised scope satisfies the checklist below.
- List exact supported artifacts and exclude Kubernetes, Terraform, Kafka/Flink, ETL v2, and autonomous Codegen PR publication until their gates pass.
- Define one canonical project ID, event schema, SDK event-name contract, browser credential model, and version manifest. Reject competing aliases.

### Gate 1 — Preserve resolved critical boundaries and fix remaining data-loss defects

- Keep structural-only JavaScript auto-capture, defense-in-depth Ingestion sanitization, verified Codegen repository grants, repository-scoped tokens, and non-overridable platform safety floors covered by packed-browser and real-GitHub integration tests.
- Keep the resolved Admin route/media-type/lockout, writer ACK/reclaim/backlog/tenant/DLQ, project-scoped offline storage, lease recovery, isolated Codegen worker, and PostgreSQL migration fixes covered by integration tests.
- Add end-to-end event idempotency so ACK-after-insert recovery cannot duplicate analytics.
- Reconcile the remaining text/integer project-ID split before any v2/decision/ETL contract is supported.

### Gate 2 — Make the core stack coherent under failure and scale

- Make Config mutations transactional with an outbox and cross-replica SSE fan-out.
- Correct Query canonical identity, experiment attribution/statistics, guardrail leadership, and query budgets.
- Make ingestion batches atomic/idempotent and enforce bytes/depth/distributed quotas.
- Align SDK shutdown, consent fencing, JSON validation, event names, exposure/context semantics, project-persistent state, and browser credentials.
- Move Agents execution and safety quotas to durable shared authority; make verdict/proposal/action/audit transitions transactional.
- Make Codegen kill switches comprehensive, PR publication idempotent, evaluation evidence provenance trusted, and GitHub pagination origin-safe.
- Add fresh-stack, crash/replay, multi-replica, and packed-consumer integration tests.

### Gate 3 — Make release artifacts and onboarding reproducible

- Replace the quick start with one CI-executed fresh-clone path and a canonical demo project.
- Publish npm, PyPI, and every supported image from the same tested revision.
- Lock dependencies/images, add SBOMs, vulnerability/license/secret scans, provenance, signing, and prerelease channels.
- Update repository URLs, architecture, threat boundaries, support status, changelog, security policy, and governance.

### Gate 4 — Add production deployment support only after core release

- Rebuild Kubernetes only from tested runtime contracts with migrations, networking, secrets, and every supported component.
- Design Terraform/cloud deployment anew with durable storage, secrets, network security, state bootstrap, application installation, and proven backup/restore.
- Promote these deployment paths from experimental only after semantic cluster tests pass.

## Minimum go/no-go checklist

A release may be called an OSS **developer preview** when all of the following are true:

- [x] JavaScript default auto-capture is structural-only and cannot retain form-control values in the tested SDK/Ingestion paths.
- [x] Every Codegen repository is bound to an operator-granted numeric repository ID and tenant policy cannot weaken platform safety floors.
- [x] No known cross-tenant Admin/Codegen authorization bypass remains in the statically reviewed and unit-tested routes.
- [ ] Accepted events survive writer crash/restart and ClickHouse retry without loss or duplication.
- [ ] SDK shutdown, queued/offline storage, identity, session, and consent preserve project/privacy boundaries.
- [ ] Codegen publication is unavailable unless a hardened isolated worker and scoped credentials are configured.
- [ ] Agent jobs and safety limits are durable and replica-safe, or autonomous execution is explicitly disabled.
- [ ] One strict project/event/experiment schema is used by all supported runtime paths.
- [ ] Fresh Postgres/ClickHouse initialization and supported upgrade migrations are automated and tested.
- [ ] The documented Compose quick start passes from a clean checkout and sends the first event exactly once.
- [ ] CI runs tests/builds for every supported service and installs packed SDK artifacts into real consumers.
- [ ] The release publishes every advertised artifact from the same gated revision with correct license/provenance metadata.
- [ ] Experimental infrastructure/features are clearly excluded from support claims.
- [ ] Security, repository, changelog, governance, and support metadata are current.

A release should not be called **production-ready** until the multi-replica, sandbox, crash/replay, infrastructure, backup/restore, observability, incident-control, and security-scanning gates are also demonstrated in a deployed environment.
