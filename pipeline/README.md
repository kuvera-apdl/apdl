# Pipeline

The APDL data pipeline: moves events from Redis Streams into ClickHouse, owns
the ClickHouse schema, and provides the ETL framework for custom event types.

For APDL 0.3.0, only the Redis-to-ClickHouse writer and the PostgreSQL and
ClickHouse migrations used by the source-built single-node core are supported.
ETL v2, Kafka, and Flink are disconnected future/experimental surfaces: they
are not started by the supported stack, published as release artifacts, or
covered by the runtime support contract.

## Layout

| Directory | 0.3.0 status | What it is |
|-----------|---|------------|
| `redis/` | Supported core | ClickHouse writer — consumes `events:raw:{project_id}` Redis Streams and batch-inserts into ClickHouse |
| `clickhouse/` | Supported core migrations | SQL migrations and reference schemas (tables + materialized views) |
| `postgres/` | Supported core migrations | The authoritative, versioned PostgreSQL migration sequence |
| `etl/` | Unsupported experiment | Standalone custom-events ETL framework (`apdl-etl` package), not wired to live events |
| `kafka/` | Unsupported design | Future Kafka topic definitions, not a runtime |
| `flink/` | Unsupported design | Future Flink jobs, not a runtime |

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
- **Delivery:** at-least-once transport with idempotent storage. New consumer
  groups start at `0-0`, so a stream's
  existing backlog is consumed on first discovery. The writer periodically uses
  `XAUTOCLAIM` to take over stale Pending Entries List deliveries from prior
  consumers. Messages are atomically XACKed and XDEL'd only after ClickHouse or
  the DLQ accepts their rows. This keeps `XLEN` equal to outstanding work so
  both event producers can enforce the shared 1,000,000-entry capacity without
  trimming accepted data. A crash between insert and ACK may replay an insert,
  but the stable client
  `message_id` and `(project_id, message_id)` replacement key make supported
  `FINAL` reads return that event exactly once. Retries must preserve the
  complete logical event, especially its original timestamp: ClickHouse does
  not merge replacement keys across monthly partitions, so changing the
  timestamp while reusing an ID violates the idempotency contract.
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

The deletion contract permits any number of consumers in the one required
`clickhouse-writer` group, but no second durable consumer group. Adding another
group requires all-group acknowledgement tracking before entries can be
deleted. Redis must use non-evicting memory policy plus durable persistence;
the supported Compose stack uses AOF (`appendfsync everysec`) and
an explicit aggregate memory ceiling with `maxmemory-policy noeviction`. Route
`event_stream_pressure`,
`event_stream_overloaded`, `redis_memory_pressure`, and
`lost_or_deleted_pending` logs to alerts and monitor Redis memory/disk capacity.
Logging is only the checked-in signal; it does not become an operational alert
until the deployment routes it.

Persistent pre-policy streams are reconciled by the writer before consumption:
an exact `XTRIM MINID` removes only legacy entries proven acknowledged before
the earliest pending delivery. This makes old acknowledged history stop
consuming the new outstanding-entry capacity. The writer performs this trim
before attempting consumer-group creation and avoids group-creation writes for
groups that already exist, so an existing group can recover when Redis starts
above its new memory ceiling. Before the first rollout of `maxmemory`, verify
that the configured ceiling exceeds current `used_memory` plus operating
headroom. If it does not, temporarily start above current usage, confirm writer
reconciliation has completed, and only then lower the ceiling; a stream with no
consumer group cannot be trimmed safely to manufacture that headroom.

An upgrade must be coordinated:
stop every Ingestion and Config outbox producer that can still issue legacy
`XADD MAXLEN`, start the new writer and confirm reconciliation, then start only
bounded producers. Mixed old/new producers are unsupported because one legacy
producer can still trim entries admitted by another process.

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

- `events` (001, ReplacingMergeTree) — raw event stream, idempotent on
  `(project_id, message_id)`; the writer's insert target
- `sessions` (002, MergeTree) — session-level rollups
- `feature_flag_exposures` (006, ReplacingMergeTree) — idempotent flag
  evaluation results projected from events
- `frontend_health_events` (007, ReplacingMergeTree) — idempotent frontend
  errors and web-vitals projected from events

**Materialized views**

- `feature_flag_exposures_mv` (006) — extracts flag fields from `events.properties` into `feature_flag_exposures`
- `frontend_health_events_mv` (007) — extracts error/web-vitals fields from `events.properties` into `frontend_health_events`

Supported Query Service analytics read each ReplacingMergeTree with `FINAL` so
retries are deduplicated before aggregation. Migration 004 removes the legacy
SummingMergeTree count views because materialized views process retried insert
blocks before source-table replacement and would permanently double count them.

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
- `010_codegen_publication_identity.sql` -- v1 publication audit archive and strict image/config-bound v2 authority
- `011_codegen_development_publication.sql` -- schema support for a draft-only development authorization; the 0.3.0 runtime still keeps Codegen offline
- `012_config_atomic_mutations.sql` -- transactional Config mutations and durable change outbox
- `013_disable_automatic_guardrails.sql` -- release fence for automatic experiment decisions
- `014_disable_self_registered_agents.sql` -- immutable project provenance and execution fence for self-registered projects

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

## ETL framework (unsupported in 0.3.0)

`etl/` is a standalone, dependency-light experimental package (Pydantic only) that
standardizes how custom records reach the warehouse: every record is wrapped
in a canonical envelope keyed by a `_schema` discriminator, processed through
a `decode → validate → enrich → build_row` Template Method lifecycle with
per-record DLQ isolation, routed by a schema registry, and handed to a
pluggable `Loader`. No supported producer, consumer, loader process, Compose
service, or release artifact connects it to the 0.3.0 runtime. New event types
can be scaffolded for research with `make new-transform`; this is not a
supported extension contract. Full design details:
[`etl/docs/etl-framework.md`](etl/docs/etl-framework.md).

## Kafka and Flink (unsupported designs)

`kafka/topics.yaml` and `flink/` preserve future scaling ideas. No default
runtime, build artifact, CI integration test, or supported deployment connects
them to APDL. Do not treat the files as migration guidance or an available
alternative to Redis Streams.

## Running locally

```bash
make dev                 # start Redis, ClickHouse, PostgreSQL (Docker)
make migrate-clickhouse  # apply ClickHouse-only migrations
make migrate-postgres    # transactionally apply the PostgreSQL sequence
make run-pipeline        # start the ClickHouse writer
```

These commands operate on a fresh local development stack. Multi-replica
operation, in-place upgrades, backup, restore, Kubernetes, and Terraform are
outside the 0.3.0 support boundary.

## Tests

```bash
make test-writer # pytest for the Redis ClickHouse writer
make lint-writer # ruff for the writer and its tests
make test-etl   # pytest for the ETL framework (pipeline/etl/tests/)
make lint-etl   # ruff for etl/, scripts/, tests/
```
