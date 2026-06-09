# APDL Architecture

<p align="center">
  <img src="architecture.svg" alt="APDL Architecture" width="950"/>
</p>

APDL is a self-optimizing product analytics and experimentation platform.
Events flow in from the SDKs, land in ClickHouse for analytics, and LLM-powered
agents read those analytics and act back on feature flags and experiments —
which immediately flow back to the SDKs. That feedback cycle is the "Loop" in
Autonomous Product Development Loop.

## Components

| Component | Tech | Port | Docs |
|---|---|---|---|
| `@apdl/sdk` (browser) | TypeScript, Rollup | — | [README](../sdk/javascript/README.md) |
| `apdl-sdk` (server) | Python 3.12, httpx | — | [README](../sdk/python/README.md) |
| Ingestion Service | FastAPI, Redis Streams | 8080 | [README](../services/ingestion/README.md) |
| Config Service | FastAPI, asyncpg, SSE | 8081 | [README](../services/config/README.md) |
| Query Service | FastAPI, ClickHouse, SciPy | 8082 | [README](../services/query/README.md) |
| Agents Service | FastAPI, LLM SDKs, pgvector | 8083 | [README](../services/agents/README.md) |
| Pipeline (writer, ETL) | Python, clickhouse-driver | — | [README](../pipeline/README.md) |

## The three flows

### 1. Events (write path)

```
SDKs ──POST /v1/events──→ Ingestion ──XADD──→ Redis Streams ──XREAD──→ ClickHouse Writer ──→ ClickHouse
```

- Ingestion authenticates (`proj_{project_id}_{secret}` API keys), rate-limits
  per project (token bucket: 1000 capacity, 100/s refill), validates batches
  (1–500 events), and appends to `events:raw:{project_id}` (`MAXLEN ~1M`).
- The ClickHouse writer consumes via a consumer group and flushes batches of
  1000 events or every 5 s, retrying up to 5 times before dropping a batch.
- The standalone [ETL framework](../pipeline/etl/docs/etl-framework.md) handles
  custom-event records on a canonical envelope (`_schema` discriminator) into
  the v2 tables (`events_v2`, `decisions_v2`, `feeds_v2`) — the same transforms
  run for live traffic, backfills, and replays.

### 2. Flags & experiments (config path)

```
Admin / Agents ──CRUD──→ Config ──→ PostgreSQL (canonical) + Redis (60s cache) ──SSE / poll──→ SDKs
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

### 3. The loop (agents)

```
ClickHouse ──→ Query Service ──→ Agents ──(safety gate)──→ Config Service ──→ SDKs ──→ new events…
```

- A supervisor routes runs through four agent graphs: behavior analysis →
  experiment design → personalization → feature proposals.
- The LLM router tries providers in order (OpenAI → Anthropic → Google →
  local), skipping providers without keys; agent memory is embedded into a
  pgvector table for retrieval across runs.
- Every proposed action passes a safety validator and an autonomy gate
  (L1 suggest-only → L4 full-auto); risky actions queue for human approval,
  everything is audit-logged, and a rollback monitor disables an experiment's
  flag if error-rate/latency/primary-metric guardrails breach.

## Storage

| Store | Holds |
|---|---|
| Redis 7 | Event streams (`events:raw:*`), flag-config cache (60 s TTL), rate-limit counters |
| PostgreSQL 16 + pgvector | Flags, experiments, UI configs, audit log, agent runs & memory |
| ClickHouse | `events`, `sessions`, `experiment_exposures`, `feature_flag_exposures`, `frontend_health_events` + hourly/daily count, experiment-metric, exposure, and health materialized views; v2 envelope tables for ETL |

## Scaling phases

Redis Streams is the Phase 1–2 event bus — simple, durable enough for local
and small-scale deployments. `pipeline/kafka/topics.yaml` sketches the
Phase 3+ migration (Kafka) once sustained throughput exceeds ~10K events/sec
or retention needs exceed 7 days.

## Editing the diagram

`architecture.svg` is hand-maintained — edit the SVG directly (it's
commented and organized by layer band). Preview with any browser or
`qlmanage -p docs/architecture.svg` on macOS.
