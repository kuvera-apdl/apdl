# Pipeline

The APDL data pipeline: moves events from Redis Streams into ClickHouse, owns
the ClickHouse schema, and provides the ETL framework for custom event types.

## Layout

| Directory | What it is |
|-----------|------------|
| `redis/` | ClickHouse writer — consumes `events:raw:{project_id}` Redis Streams and batch-inserts into ClickHouse |
| `clickhouse/` | SQL migrations and reference schemas (tables + materialized views) |
| `postgres/` | Versioned PostgreSQL migrations, including the credential registry |
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
- **Delivery:** at-least-once. Consumer groups start at `$` (no historical
  replay); the writer periodically uses `XAUTOCLAIM` to take over stale Pending
  Entries List deliveries from prior consumers. Messages are XACKed only after
  ClickHouse accepts their rows. A crash between insert and ACK may replay a
  row; the legacy table does not provide exactly-once storage semantics.
- **Validation and DLQ:** canonical ClickHouse row types are validated before
  buffering. Terminal parse/row rejects write safe metadata (never the event
  payload) to the bounded `events:dlq:{project_id}` Redis stream. The source is
  XACKed only after DLQ persistence; a DLQ failure leaves it in the PEL for
  later retry. A terminal row cannot hold valid rows or other tenants behind
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

Note: `005_pgvector_setup.sql` runs against **PostgreSQL**, not ClickHouse
(agent memory, audit log, runs, experiments, ui_configs tables + pgvector).

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
make migrate-clickhouse  # apply clickhouse/migrations/*.sql
make migrate-postgres    # apply postgres/migrations/*.sql
make run-pipeline        # start the ClickHouse writer
```

## Tests

```bash
make test-writer # pytest for the Redis ClickHouse writer
make lint-writer # ruff for the writer and its tests
make test-etl   # pytest for the ETL framework (pipeline/etl/tests/)
make lint-etl   # ruff for etl/, scripts/, tests/
```
