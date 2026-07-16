# APDL Architecture

> **0.3.0 developer-preview boundary:** this document describes the
> source-built, fresh, single-node Compose runtime. Ingestion, Config, Query,
> the Redis-to-ClickHouse writer, Gateway, Admin API, and Admin Console are the
> supported core. Agents is an opt-in operator preview; only the Codegen API
> control plane is a source-only offline preview. Its editor/worker and
> publication paths are unsupported. ETL v2, Kafka, Flink, Kubernetes, Terraform,
> multi-replica operation, upgrades, backup, and restore are not supported.
> See [Support](../SUPPORT.md).

APDL is a product analytics and experimentation platform with an optional
operator-run Agents preview. Events flow in from the SDKs and land in
ClickHouse for analytics. Agents can read those analytics and propose
experiment drafts, but 0.3.0 does not close the loop automatically: a human
must approve the draft, treatment implementation and review are separate, and
an operator must explicitly activate the completed experiment. The closed
feedback cycle remains a product direction rather than a current capability.

## Components

| Component | Tech | Port | 0.3.0 status | Docs |
|---|---|---|---|---|
| `@apdl-oss/sdk` (browser) | TypeScript, Rollup | — | Published npm SDK | [README](../sdk/javascript/README.md) |
| `apdl-sdk` (server) | Python 3.12, httpx | — | Published PyPI SDK | [README](../sdk/python/README.md) |
| Ingestion Service | FastAPI, Redis Streams | 8080 | Core, source-built | [README](../services/ingestion/README.md) |
| Config Service | FastAPI, asyncpg, SSE | 8081 | Core, source-built | [README](../services/config/README.md) |
| Query Service | FastAPI, ClickHouse, SciPy | 8082 | Core, source-built | [README](../services/query/README.md) |
| Agents Service | FastAPI, LLM SDKs, pgvector | 8083 | Operator preview, opt-in | [README](../services/agents/README.md) |
| Codegen API/control plane | FastAPI, GitHub App | 8084 (internal) | Source-only offline preview; editor/worker unsupported | [README](../services/codegen/README.md) |
| Admin API | FastAPI, Argon2id, opaque sessions | 8085 (internal) | Core, source-built | [README](../services/admin-api/README.md) |
| Admin Console | React, Vite, nginx | 5173 | Core, source-built | [README](../services/admin/README.md) |
| Redis-to-ClickHouse writer | Python, clickhouse-driver | — | Core, source-built | [README](../pipeline/README.md) |
| ETL v2 / Kafka / Flink | Python / design scaffolds | — | Unsupported | [README](../pipeline/README.md) |

## The three flows

### 1. Events (write path)

```
SDKs ──POST /v1/events──→ Ingestion ──XADD──→ Redis Streams ──XREAD──→ ClickHouse Writer ──→ ClickHouse
```

- Ingestion verifies API keys against the hashed credential registry, derives
  project/role authority server-side, applies a shared Redis token bucket
  charged by event count and bytes, validates bounded strict JSON batches
  (1–100 canonical events), and atomically admits each complete batch to
  `events:raw:{project_id}` only while the exact outstanding depth remains at
  or below one million. Producers never trim accepted events and warn at
  750,000 outstanding entries.
- The ClickHouse writer consumes via a consumer group and flushes batches of
  1000 events or every 5 s. Redis deliveries are acknowledged only after a
  durable ClickHouse insert or bounded DLQ record, then deleted atomically so
  stream depth remains the admission backlog. Stable client message IDs
  and `ReplacingMergeTree` keys make supported `FINAL` reads idempotent after
  an insert-before-ACK replay.
- The standalone [ETL framework](../pipeline/etl/docs/etl-framework.md) and its
  v2 tables are experimental and are not part of the live developer-preview
  event path.

### 2. Flags & experiments (config path)

```
Admin Browser ──HttpOnly session──→ Admin API ──service key──→ Config / Query
Operator preview ──→ Agents / offline Codegen
Agents ──human-approved inert draft──→ Config ──→ PostgreSQL (canonical) + Redis (60s cache)
Operator activation ──→ Config ──SSE / poll──→ SDKs
```

- PostgreSQL stores canonical flag configs: targeting rules, rollouts,
  lifecycle state (`draft` / `active` / `disabled`, plus archived), guardrails,
  and a full audit log. Updates use optimistic versioning (`version` must match,
  409 on conflict).
- Every write invalidates the Redis cache and broadcasts a `flag_update` /
  `experiment_update` SSE event; the JS SDK applies it live, the Python SDK
  picks it up on its next poll.
- **Evaluation is local.** SDKs bucket users themselves with a shared FNV-1a
  32-bit hash of `{flag_key}:{salt}:{unit_id}`. The config service owns the
  canonical implementation; the JS and Python SDKs are byte-for-byte identical
  (golden values pinned in `fixtures/gates/parity.json` and SDK tests), so a
  user buckets the same way in the browser, on the server, and in the config
  service. No network round-trip on the hot path.

### 3. The optional proposal flow (Agents operator preview)

```
ClickHouse ──→ Query Service ──→ Agents ──→ human approval ──→ disabled Config draft
                                                        └──→ optional treatment changeset

implementation + review + explicit operator activation ──→ SDKs ──→ new events
```

- A supervisor routes enabled runs through behavior analysis and experiment
  design. Experiment evaluation, feature proposals, and personalization are
  disabled; Config has no UI-config storage or delivery API in 0.3.0.
- Agent runs and approval effects are PostgreSQL-backed queues. Replica-safe
  leases recover an interrupted run from persisted agent results, while an
  approval transaction records exact decisions, required audit intents, and
  ordered per-item effects before the worker may call Config or Codegen.
  Effect retries reuse one persisted downstream idempotency key and expose
  partial/manual-intervention state to operators. Operator cancellation clears
  the run lease, prevents queued approval effects from being claimed, and
  fences later LLM egress against the cancelled execution.
- The LLM router treats configured providers as candidates, not authority.
  Every call is project/run scoped and classified; an exact provider/model row,
  residency match, and available run/project budget are required before a
  durable attempt record is marked for egress. The default policy permits only
  local `gemma4` at `http://localhost:11434/v1`, zero paid spend, and no
  cross-vendor retry. Agent memory is embedded into a pgvector table for
  retrieval across runs.
- Agents execution is available only to operator-provisioned projects;
  self-created projects retain read-only history and definitions. Experiment
  proposals receive static shape/blast-radius validation and are audit-logged,
  but every experiment design requires human approval regardless of its stored
  autonomy level. Approval creates a disabled draft; it does not deploy a
  treatment, enable the flag, or enter `running`. No canonical live guardrail
  or rollback contract exists, and automatic evaluation and rollback are
  unavailable.
- Codegen is reachable only through the private service network and remains in
  offline/non-publishing mode in 0.3.0. A project role
  cannot choose a GitHub repository: a trusted operator separately grants one
  immutable repository ID, and each GitHub mutation uses a token restricted to
  that repository.
- `/ready` covers only Agents runtime initialization and PostgreSQL so optional
  dependencies do not block orchestration. `/ready/capabilities` separately
  reports configured and reachable LLM, Query, Config, and Codegen capability.

## Storage

| Store | Holds |
|---|---|
| Redis 7 | Event streams (`events:raw:*`), flag-config cache (60 s TTL), rate-limit counters |
| PostgreSQL 16 + pgvector | Flags, experiments, audit log, agent runs & memory |
| ClickHouse | Core event/session/exposure/health tables and materialized views; disconnected v2 envelope tables remain unsupported ETL scaffolding |

## Deployment boundary

Redis Streams is the only 0.3.0 event bus. The checked-in Kafka/Flink and ETL
v2 files are future design/scaffolding and are not connected to the live
runtime, release artifacts, or support contract. The repository contains no
supported Kubernetes or Terraform deployment.

Compose runs a single instance of each service and uses a local-development
nginx Gateway. Cross-replica cache/SSE behavior, hardened public ingress,
in-place schema upgrades, backup, and restore have not been qualified. Build a
fresh stack and do not expose it as a production service.
