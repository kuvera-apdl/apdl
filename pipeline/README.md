# Pipeline

The APDL data pipeline: moves events from Redis Streams into ClickHouse, owns
the ClickHouse schema, and provides the ETL framework for custom event types.

## Layout

| Directory | What it is |
|-----------|------------|
| `redis/` | ClickHouse writer — consumes `events:raw:{project_id}` Redis Streams and batch-inserts into ClickHouse |
| `clickhouse/` | SQL migrations and reference schemas (tables + materialized views) |
| `postgres/` | The authoritative, versioned PostgreSQL migration sequence |
| `etl/` | Standalone custom-events ETL framework (`apdl-etl` package) |
| `kafka/` | Kafka topic definitions for the Phase 3+ migration |
| `flink/` | Flink jobs (sessionization, enrichment, aggregations) for Phase 3+ |

## ClickHouse writer

`redis/clickhouse_writer.py` is a single-file async consumer
(deps: `redis`, `clickhouse-driver`). It reads from every `events:raw:*`
stream — discovered via `SCAN`, or pinned with `PROJECT_IDS` — using the
`clickhouse-writer` consumer group (consumer name `worker-{pid}`) and
batch-inserts into the `events` table.

- **Batching:** rotates fairly across streams and reads one tenant at a time so
  Redis's per-stream `COUNT` behavior cannot exceed the global 1000-event
  buffer (`BUFFER_SIZE`). It flushes when full or every 5 seconds
  (`FLUSH_INTERVAL`), whichever comes first.
- **Delivery:** at-least-once. New consumer groups start at `0-0`, so a stream's
  existing backlog is consumed on first discovery. The writer periodically uses
  `XAUTOCLAIM` to take over stale Pending Entries List deliveries from prior
  consumers. Messages are XACKed only after ClickHouse accepts their rows. A
  crash between insert and ACK may replay a row; the legacy table does not
  provide exactly-once storage semantics.
- **Tenant authority:** the project is derived only from a validated
  `events:raw:{project_id}` stream key. Conflicting project assertions inside a
  Redis message or its event JSON are rejected.
- **Validation and DLQ:** canonical ClickHouse row types are validated before
  buffering. Terminal parse/row rejects write safe metadata (never the event
  payload) to the bounded `events:dlq:{project_id}` Redis stream. The source is
  XACKed only after DLQ persistence; a DLQ failure leaves it in the PEL for
  later reclaim. A terminal row cannot hold valid rows or other tenants behind
  it.
- **Retries:** a failed ClickHouse flush remains buffered and stops further
  reads once the bounded buffer is full. Retries use capped exponential
  backoff shared by the consumer and periodic flusher; events are not dropped
  after an arbitrary retry count. Only narrow client-side row serialization
  errors are terminal—server/schema failures retain the batch.
- **Shutdown:** SIGINT/SIGTERM trigger a final flush and stats log.

Environment variables: `REDIS_URL` (default `redis://localhost:6379`),
`CLICKHOUSE_URL` (default `clickhouse://localhost:9000/apdl`), `BUFFER_SIZE`,
`FLUSH_INTERVAL`, `DLQ_MAXLEN` (default 10000 per project),
`PENDING_CLAIM_IDLE_MS` (default 60000),
`PENDING_CLAIM_INTERVAL_SECONDS` (default 30), and `PROJECT_IDS` (optional
comma-separated allowlist).

## ClickHouse schema

Migrations live in `clickhouse/migrations/` (applied by
`make migrate-clickhouse`); `clickhouse/schemas/events.sql` is a documentation
copy of the events table.

**Tables**

- `events` (001, MergeTree) — raw event stream; the writer's insert target
- `sessions` (002, MergeTree) — session-level rollups
- `experiment_exposures` (003, ReplacingMergeTree) — first exposure per user/experiment/variant
- `feature_flag_exposures` (006, ReplacingMergeTree) — flag evaluation results projected from events
- `frontend_health_events` (007, MergeTree) — frontend errors and web-vitals projected from events

**Materialized views**

- `event_counts_hourly_mv` / `event_counts_daily_mv` (004, SummingMergeTree) — event counts + unique users per project/event per hour/day
- `experiment_metrics_mv` (004, AggregatingMergeTree) — hourly per-variant metric states (count, uniq users, revenue) by joining events to exposures
- `feature_flag_exposures_mv` (006) — extracts flag fields from `events.properties` into `feature_flag_exposures`
- `frontend_health_events_mv` (007) — extracts error/web-vitals fields from `events.properties` into `frontend_health_events`

Every file in `clickhouse/migrations/` must be executable ClickHouse SQL. The
runner fails if a PostgreSQL marker is found there instead of silently skipping
the misplaced migration.

## PostgreSQL schema

PostgreSQL migrations live only in `postgres/migrations/` and are applied by
`make migrate-postgres` in a strict, contiguous, zero-padded filename order.
The runner records each file's version, exact name, and SHA-256 checksum in the
immutable `apdl_schema_migrations` ledger. A file and its ledger insert commit
in the same transaction under an advisory lock. Applied files run exactly once;
renaming, editing, deleting, or inserting an older file fails closed.

- `001_auth_credentials.sql` -- project-scoped service credential registry
- `002_admin_auth.sql` -- admin users, sessions, project grants, and proxy audit
- `003_admin_projects.sql` -- self-service project registry and grant backfill
- `004_agents_core.sql` -- the live Agents tables and pgvector memory shape
- `005_agent_observability.sql` -- agent envelope metadata and `llm_calls`
- `006_config.sql` -- flags, flag audit history, and Config experiments
- `007_codegen.sql` -- connections, changesets, GitHub/CI observations, and claims
- `008_codegen_safety_policy.sql` -- strict tenant preferences and effective safety-policy provenance
- `009_codegen_repository_authority.sql` -- operator-verified repository grants, legacy binding quarantine, and immutable changeset targets

Config, Agents, and Codegen never create or alter tables at process startup.
They verify the required ledger entry and schema columns, then fail with a
`make migrate-postgres` instruction if the database is behind. Docker Compose
gates all PostgreSQL consumers on the one-shot `postgres-migrate` service, so a
plain full-stack Compose start has the same ordering as `make dev-all`.

The obsolete PostgreSQL files formerly numbered 005 and 011 under the
ClickHouse directory are not applied verbatim. Their UUID/`vector(1536)` Agent
tables and integer project identifiers conflict with the running services.
Migration 004 installs the live TEXT/BIGSERIAL/`vector(384)` contracts and, if
someone previously ran the obsolete SQL manually, preserves incompatible rows
in `*_legacy_005` tables. It also preserves embeddings from a non-canonical
vector width in `agent_memory_legacy_vectors` before installing `vector(384)`.
Migration 005 similarly preserves an incompatible `llm_calls` table as
`llm_calls_legacy_011`. The deprecated `ui_configs` scaffold is not recreated;
Config owns the one canonical `experiments` table. Migration 006 preserves an
unprojectable `feature_flags` table as `feature_flags_legacy`, and migration 007
preserves runtime-evidence rows without an exact CI binding in
`codegen_runtime_evidence_observations_legacy_unbound`.

## ETL framework

`etl/` is a standalone, dependency-light package (Pydantic only) that
standardizes how custom records reach the warehouse: every record is wrapped
in a canonical envelope keyed by a `_schema` discriminator, processed through
a `decode → validate → enrich → build_row` Template Method lifecycle with
per-record DLQ isolation, routed by a schema registry, and handed to a
pluggable `Loader`. New event types are scaffolded with `make new-transform`
and need no pipeline changes. Full details: [`etl/docs/etl-framework.md`](etl/docs/etl-framework.md).

## Kafka (Phase 3+)

`kafka/topics.yaml` defines the topic layout (partitions, replication,
retention, keys) for migrating off Redis Streams once sustained throughput
exceeds ~10K events/sec or retention beyond 7 days is needed.

## Running locally

```bash
make dev                 # start Redis, ClickHouse, PostgreSQL (Docker)
make migrate-clickhouse  # apply ClickHouse-only migrations
make migrate-postgres    # transactionally apply the PostgreSQL sequence
make run-pipeline        # start the ClickHouse writer
```

## Tests

```bash
make test-writer # pytest for the Redis ClickHouse writer
make lint-writer # ruff for the writer and its tests
make test-etl   # pytest for the ETL framework (pipeline/etl/tests/)
make lint-etl   # ruff for etl/, scripts/, tests/
```
