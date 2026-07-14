# APDL OSS Release: Minimum Fix List

**Target:** first public OSS developer-preview release from the `recent-critical-fixes` line.

**Release posture:** single-node/self-hosted developer preview. Do not claim production readiness, lossless analytics, multi-replica operation, or autonomous code publication.

## Required scope exclusions

The minimal release is possible only if all unsupported surfaces are disabled and clearly excluded:

- Autonomous Codegen branch/PR publication is disabled. Codegen may remain available only in non-publishing offline/shadow mode.
- Autonomous Agents execution is disabled unless item 6 below is implemented.
- ETL v2, Kafka, Flink, Kubernetes, Terraform, multi-replica deployment, and production backup/restore are experimental or unsupported.
- The release supports fresh installation only unless ClickHouse upgrade migrations are made authoritative and tested.

## Bugs that must be fixed

### 1. Establish one strict event and exposure contract

The JavaScript SDK currently emits `experiment_context` on exposure events while Ingestion rejects it. SDK languages also use different event names and identity shapes, and Ingestion still accepts competing aliases.

Fix one canonical schema across both SDKs, Ingestion, the writer, ClickHouse, and Query. Reject aliases and unknown fields at the edge. Add a packed-SDK-to-Ingestion contract test.

**Audit findings:** APDL-AUD-012, 016, 017, 078, 107.

### 2. Prevent accepted-event loss, duplication, and permanent queue poisoning

Ingestion can partially publish a batch while returning batch-level success, the writer has no storage idempotency, and invalid/non-JSON events can block SDK queues indefinitely.

Validate JSON and timestamps before enqueue/acceptance, define atomic or per-event batch results that both SDKs implement, add a stable idempotency key at ClickHouse insertion, and prove crash/retry behavior without loss or duplicates.

**Audit findings:** APDL-AUD-009, 013, 018, 019, 071.

### 3. Close SDK lifecycle, consent, and browser-credential gaps

SDK shutdown does not reliably drain all work, consent revocation can still transmit queued events, persistent identity/session/consent state is not fully project-scoped, and the browser credential model exposes a long-lived secret with contradictory documentation.

Make shutdown drain-or-return every pending event, fence every send on current consent, clear queues on revocation, namespace all persistent state by project, and define a restricted browser-safe client credential that never carries administrative roles or appears in URLs.

**Audit findings:** APDL-AUD-002, 006, 011, 108, plus the remaining boundary under APDL-AUD-001.

### 4. Make experiment and flag mutations atomic and evaluator behavior identical

Experiment records and their backing flags can diverge after partial failures or direct flag CRUD. Audit/cache/broadcast side effects also have ambiguous failure semantics, and SDK/server targeting behavior is inconsistent.

Use one PostgreSQL transaction for experiment, flag, version, and audit state; prevent generic CRUD from mutating experiment-owned flags; publish cache/SSE changes through a durable outbox; and run shared targeting fixtures against every evaluator.

For the single-node preview, cross-replica fan-out may be deferred only if multi-replica operation is explicitly unsupported.

**Audit findings:** APDL-AUD-021, 022, 026, 027, 116, 117, 121, 123.

### 5. Correct experiment analytics before exposing decisions

Query currently trusts caller-supplied experiment labels, collapses anonymous actors, mishandles all-zero conversion and multi-treatment experiments, and can emit non-finite small-sample statistics.

Resolve experiments and variants from authoritative Config data, use one canonical actor identity, handle crossover explicitly, analyze every treatment against control, return finite/typed insufficient-data outcomes, and add strict query/time/row budgets.

Automatic guardrail stopping and autonomous decisions must remain disabled until these fixes pass end-to-end fixtures.

**Audit findings:** APDL-AUD-029 through 037, 118, 119.

### 6. Prevent public registration from creating unbounded work and spend

Any registered user can create projects and repeatedly trigger globally funded LLM work. Agents jobs, safety quotas, and several action/audit transitions are not fully durable or transactional.

For the minimum release, choose one strict option:

1. Disable Agents execution for self-registered projects; or
2. Add project/user creation limits, provider spend budgets, durable shared job claims and quotas, transactional proposal/verdict/action/audit transitions, and tenant-pinned provider policy.

**Audit findings:** APDL-AUD-039 through 042, 046, 047, 111, 114.

### 7. Provide one clean, reproducible installation and smoke path

The documented quick start and examples use stale schemas/credentials, Compose readiness does not prove the stack can perform its advertised work, and Codegen is ready while its default worker cannot launch.

Create one CI-executed fresh-clone flow that initializes PostgreSQL and ClickHouse, provisions a canonical demo project and restricted credentials, starts only supported services, sends an event exactly once, evaluates a flag, queries the result, and shuts down cleanly. Unsupported services must be off by default or visibly marked unavailable.

**Audit findings:** APDL-AUD-062, 076, 085 through 087, 115, 120.

### 8. Gate and publish the exact supported artifact set

GitHub CI omits tests for Ingestion, Config, Query, Agents, and Codegen; release automation publishes only npm and four images; the Python SDK is not published and its artifacts omit `LICENSE`; versions and documentation disagree.

Run lint and tests for every supported service, add the cross-service smoke and packed-consumer tests, publish every advertised artifact from the same tested revision, include correct license metadata, verify tag/package versions, and update README, SECURITY, changelog, repository links, supported-scope, and vulnerability/dependency gates.

**Audit findings:** APDL-AUD-015, 093 through 100, 122.

## Release decision

The developer preview is a **go** only when all eight items are complete or when an item explicitly allows the affected feature to be disabled and the release actually disables and documents it. Fixing only the currently passing unit tests is insufficient; items 1, 2, 5, and 7 require cross-service integration evidence.

Production readiness remains out of scope for this list.
