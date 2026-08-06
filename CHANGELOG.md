# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for its published SDKs. APDL remains a pre-1.0 developer preview.

## [Unreleased]

### Added

- Added policy-governed xAI/Grok support to Agents, including separate fast and
  reasoning model candidates, OpenAI-compatible function calling, readiness
  probing, operator provisioning, audited provider-attempt storage, and Docker
  configuration.

## [0.3.4] - 2026-07-29

### Changed

- Server-side flag evaluation now defaults exposure logging off consistently in
  Config and Agents. Callers that require experiment exposure analytics must
  explicitly send `log_exposure: true` with a stable message ID unique to one
  flag exposure; otherwise observed experiment sample sizes will decrease.
- JavaScript SDK browser state now uses deployment-scoped storage. Existing
  project-only/unscoped state is deliberately discarded on first startup, so
  anonymous visitor counts can temporarily rise and pre-upgrade funnels cannot
  be joined across that boundary.
- Built-in SDK UI copy, including modal bodies, is rendered as text. Callers
  that previously supplied HTML must migrate to application-owned rendering.
- Query selectors now enforce exact JSON types instead of ClickHouse coercion;
  selectors that relied on string/number/boolean coercion no longer match, and
  integer filter values are limited to the exactly representable JSON range
  from `-9007199254740991` through `9007199254740991`.
- Codegen changeset creation now returns `409 changeset_creation_disabled`
  when a project has no active repository grant. Clients that treated the
  former `404 repository_not_configured` response as missing route/resource
  must migrate to the capability-conflict response.
- Codegen's three no-fix worker dependency suppressions have a hard
  `2026-08-22` UTC review boundary, after which CI deliberately fails without
  renewed review. Renewal must update the Python approval authority in
  `services/codegen/scripts/audit_worker_dependencies.py` and
  `services/codegen/dependency-audit-suppressions.json` together; one-sided
  changes fail closed.
- ClickHouse migration now requires the PostgreSQL maintenance coordinator;
  ClickHouse-only deployment and bare-Compose migration startup are unsupported.
- Fresh PostgreSQL installs now separate the migration owner from one shared
  non-owner runtime login and two dedicated audit roles. Existing databases and
  owner-valued local `POSTGRES_URL` files must be replaced with a fresh install;
  this preview does not support the in-place role handoff.
- Operators now have a confirmation-bound, reason-audited purge path for
  experiment audit retention and authenticated replay/discard recovery for
  quarantined Config exposure outbox rows.

### Removed

- Removed the JavaScript SDK environment variables
  `NEXT_PUBLIC_APDL_ENDPOINT` and `APDL_ENDPOINT`. Deployments must migrate to
  the canonical `NEXT_PUBLIC_APDL_URL` and `APDL_URL` names respectively;
  legacy names are ignored rather than treated as aliases.
- Removed the disconnected Kafka topic definitions and Flink job scaffolding.
  Redis Streams is now the only event bus represented in the repository.
- Removed the disconnected generic ETL/v2 design package and its setup, CI, and
  dependency-management hooks. The Redis-to-ClickHouse writer remains the only
  event-loading path.

### Fixed

- Replaced permissive JavaScript SDK configuration with one strict runtime
  contract and removed unsupported cookie persistence and strict privacy modes;
  memory persistence now avoids all browser storage, including IndexedDB, and
  reports retryable delivery failures as pending rather than persisted.
- Reduced default browser context and every reserved auto-capture signal to
  query-free, fragment-free property allowlists enforced outside custom hooks.
- Removed the disconnected JavaScript `ui_config` SSE handler; UI rendering is
  local-only until Config owns a canonical storage and distribution contract.

## [0.3.3] - 2026-07-13

This is the first release with an explicit OSS developer-preview contract. The
published artifact set is the GitHub source release, `@apdl-oss/sdk` on npm,
and `apdl-sdk` on PyPI, all gated from the same revision. There are no 0.3.3
GHCR/container artifacts; the local Compose stack builds images from source.

### Fixed

- Established one strict canonical event, identity, and exposure contract
  across both SDKs, Ingestion, the writer, ClickHouse, and Query, including a
  packed-SDK contract gate.
- Made accepted-event delivery atomic and retry-safe with stable message IDs,
  idempotent ClickHouse storage, bounded dead-letter handling, and recovery of
  stale Redis deliveries.
- Hardened SDK shutdown, consent revocation, project-scoped persistent state,
  and browser-safe restricted credentials.
- Made flag/experiment mutation transactional, protected experiment-owned
  flags, added a durable change outbox, and aligned evaluator behavior through
  shared fixtures.
- Made experiment analysis resolve Config-owned metadata, use canonical actor
  identity, handle crossover and multiple treatment arms, return finite typed
  insufficient-data results, and enforce query budgets. Autonomous experiment
  decisions remain disabled.
- Disabled Agents execution and approval for self-registered projects using
  immutable project provenance, while retaining read-only history and
  definitions.
- Added the CI-executed `make smoke-fresh` path: initialize fresh databases,
  provision strict demo credentials, start only the supported core, evaluate a
  flag, send and query exactly one event, and clean up all resources.
- Reconciled release gates, artifact/license/version metadata, canonical
  repository links, support/security scope, dependency policy, and minimal OSS
  governance.

### Release boundary

- Supported runtime: fresh, single-node, source-built Docker Compose core,
  including Admin.
- Preview-only: Agents for operator-provisioned projects; the offline Codegen
  API/control plane. Codegen's editor/worker and `agent` extra are unsupported.
- Unsupported: ETL v2, Kafka, Flink, Kubernetes, Terraform, multi-replica
  deployments, in-place upgrades, backup, and restore.

## [0.2.0] - 2026-06-15

### JavaScript SDK

- Advanced the npm package `@apdl-oss/sdk` to `0.2.0` with a single-endpoint
  client contract and React provider/hook adapter work from the 0.2 development
  line.
- Renamed the npm scope from `@apdl/sdk` to `@apdl-oss/sdk` and declared public
  package metadata.

This version records npm package history only. The repository has no matching
record of a published `apdl-sdk` PyPI release or service-container release for
0.2.0, so none is claimed here.

## [0.1.0] - 2026-03-02

Initial repository development baseline: JavaScript and Python SDK sources,
Ingestion, Config, Query, Agents, the Redis-to-ClickHouse writer, local Compose,
and initial database schemas. This entry does not assert that every package or
service was published to a registry.

[Unreleased]: https://github.com/kuvera-apdl/apdl/compare/v0.3.4...HEAD
[0.3.4]: https://github.com/kuvera-apdl/apdl/releases/tag/v0.3.4
[0.3.3]: https://github.com/kuvera-apdl/apdl/releases/tag/v0.3.3
[0.2.0]: https://github.com/kuvera-apdl/apdl/commit/91a75cfd6572e0a75c718615582c515205d9c3f6
[0.1.0]: https://github.com/kuvera-apdl/apdl/commit/03668bfa6b5d4e759d1b968cdb7c299402c0bd06
