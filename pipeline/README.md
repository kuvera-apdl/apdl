# Pipeline

The APDL data pipeline moves events from Redis Streams into ClickHouse and owns
the ClickHouse and PostgreSQL migration paths.

For APDL 0.3.0, the Redis-to-ClickHouse writer and the PostgreSQL and ClickHouse
migrations used by the source-built single-node core are supported. Redis
Streams is the only event bus included in APDL.

## Layout

| Directory | 0.3.0 status | What it is |
|-----------|---|------------|
| `redis/` | Supported core | ClickHouse writer — consumes `events:raw:{project_id}` Redis Streams and batch-inserts into ClickHouse |
| `clickhouse/` | Supported core migrations | SQL migrations and reference schemas (tables + materialized views) |
| `postgres/` | Supported core migrations | The authoritative, versioned PostgreSQL migration sequence |

## ClickHouse writer

`redis/clickhouse_writer.py` is a single-file async consumer
(deps: `redis`, `clickhouse-driver`, `asyncpg`). The synchronous ClickHouse driver is
isolated in one dedicated worker thread, so inserts cannot block Redis reads,
pending claims, monitoring, or signal handling on the asyncio loop. It reads
from every `events:raw:*` stream — discovered via `SCAN`, or pinned with
`PROJECT_IDS` — using the `clickhouse-writer` consumer group (consumer name
`worker-{pid}`) and batch-inserts into the `events` table.

- **Single writer authority:** exactly one process may advance the
  `clickhouse-writer` group. Before reading Redis, startup takes a dedicated
  PostgreSQL session advisory lock and exits if another writer owns it. The
  same checked-out session is heartbeat-verified for the writer lifetime, and
  supported Compose declares one replica. This keeps the process-local
  completeness frontier equal to the group-wide frontier; horizontal writer
  scaling requires a shared frontier redesign.
- **Batching:** rotates fairly across streams and reads one tenant at a time so
  Redis's per-stream `COUNT` behavior cannot exceed the global 1000-event
  buffer (`BUFFER_SIZE`). It flushes when full or every 5 seconds
  (`FLUSH_INTERVAL`), whichever comes first.
- **Delivery:** at-least-once transport with idempotent storage. New consumer
  groups start at `0-0`, so a stream's
  existing backlog is consumed on first discovery. The writer periodically uses
  `XAUTOCLAIM` to take over stale Pending Entries List deliveries from prior
  consumers. Messages are atomically XACKed and XDEL'd only after ClickHouse or
  the DLQ accepts their rows. Durable finalization is isolated per stream:
  PostgreSQL verification/frontier waits are bounded, a blocked stream retains
  only its own IDs, and healthy streams continue to be read and finalized.
  This keeps `XLEN` equal to outstanding work so
  both event producers can enforce the shared 1,000,000-entry capacity without
  trimming accepted data. A crash between insert and ACK may replay an insert,
  but the stable client
  `message_id` and `(project_id, message_id)` replacement key make supported
  `FINAL` reads return that event exactly once. The canonical event tables
  partition by project, while retention dates derive from server-authoritative
  receipt time. Retries must preserve the complete logical event; reusing an ID
  for changed content has undefined winner semantics.
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
- **Experiment boundaries:** marker publication selects at most one due marker
  per project per sweep. Each marker failure is isolated from later tenants and
  persists a server-time exponential-backoff deadline in PostgreSQL. Transient
  failures enter terminal quarantine on the fifth failed publication attempt;
  malformed markers quarantine immediately. Both paths persist a fixed safe
  failure code. Existing Redis dedup IDs are atomically checked against the
  exact stream entry and marker fields before reuse. The first valid observed
  stream ID is retained across retries; a quarantined observed delivery is
  ACKed only after its project completeness frontier is permanently degraded.
  A poisoned dedup ID already owned by another boundary is quarantined without
  stealing that ID, while genuine post-XADD observations remain mandatory.
  Redis insertion remains token-idempotent, and the original project,
  experiment, version, window, stream, token, and observed identity never
  changes. Startup holds the migration guards while proving the exact migration
  041 ledger checksum, columns, canonical constraint definitions, and exact
  monotone/terminal trigger function before taking singleton writer authority.
- **Shutdown:** SIGINT/SIGTERM trigger a bounded final flush and stats log.
  Cancellation never marks a still-running synchronous insert as complete. If
  the shutdown deadline expires, the writer closes the native ClickHouse socket
  and leaves the Redis deliveries pending for replay instead of ACKing an
  unobserved insert result.

The deletion and completeness contract permits exactly one live consumer in
the one required `clickhouse-writer` group and no second durable consumer
group. Adding a consumer requires a shared completeness frontier; adding a
group also requires all-group acknowledgement tracking before entries can be
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
`POSTGRES_URL` (default
`postgresql://apdl:apdl_dev@localhost:5432/apdl`, used for singleton writer and
migration-inhibitor authority),
`CLICKHOUSE_NATIVE_URL` (default
`clickhouse://apdl:apdl_dev@localhost:9000/apdl`), `BUFFER_SIZE`,
`FLUSH_INTERVAL`, `DLQ_MAXLEN` (default 10000 per project),
`PENDING_CLAIM_IDLE_MS` (default 60000),
`PENDING_CLAIM_INTERVAL_SECONDS` (default 30),
`CLICKHOUSE_CONNECT_TIMEOUT_SECONDS` (default 5),
`CLICKHOUSE_SEND_RECEIVE_TIMEOUT_SECONDS` (default 30),
`CLICKHOUSE_SYNC_REQUEST_TIMEOUT_SECONDS` (default 5),
`SHUTDOWN_TIMEOUT_SECONDS` (default 10), and `PROJECT_IDS` (optional
comma-separated allowlist). The writer owns the three native-driver timeout
query parameters and replaces conflicting values embedded in the URL.

## ClickHouse schema

Migrations in `clickhouse/migrations/` are the only executable ClickHouse schema
authority and are applied by `make migrate-clickhouse`. There is no second
schema copy to update independently.

The migrator validates one contiguous `001...N` filename sequence, records the
exact SHA-256 and name of every applied file in
`apdl_schema_migrations`, and rejects missing, reordered, renamed, or modified
history. Applied migration files are immutable; schema changes require a new
numbered migration. SQL is written to be restart-safe because ClickHouse DDL is
not transactional and a process can stop after the DDL succeeds but before its
ledger row is recorded.

Migration 005 upgrades the known pre-ledger `events` and `sessions` shapes
without resetting the volume: legacy-only event columns receive deterministic
defaults, rows move through canonical shadow tables, and `EXCHANGE TABLES`
installs the required string project identifiers plus the event
`ReplacingMergeTree(received_at)` engine and `(project_id, message_id)` sorting
key. Migrations 006 and 007 then rebuild their derived projections from
`events FINAL`. Stop the writer and Query service while applying an upgrade so
no process observes the short projection-rebuild window.

Migration 016 gives every personally attributable base and derived analytics
table the same 12-month server-receipt retention boundary and removes the
irreversible identity aggregate. The supported, fenced project/user purge
workflow and its append-only completion evidence are documented in
[Analytics data retention and deletion](../docs/data-retention.md).

### Canonical developer-preview event contract

The release has one event contract and one analytical source of truth:

1. SDKs send the strict flat event shape validated by
   `services/ingestion/app/models/schemas.py`.
2. Ingestion publishes that complete JSON record to
   `events:raw:{project_id}`.
3. The Redis writer validates the same field set and inserts into `events`.
4. Query SQL and ClickHouse materialized views read `events` (using `FINAL`
   where retry deduplication is required).

There is no envelope alias, v2 dual-write, or fallback loader in the supported
runtime. The unused service envelope models and prototype SQL were removed;
migration 012 also drops their inert tables and views from older volumes. No
reconciliation is performed because those objects never had a deployed writer
or query path. In-place upgrades from the documented pre-ledger event shape are
covered by `make test-clickhouse-upgrade`; unknown third-party ClickHouse
schemas remain outside the developer-preview contract.

**Tables**

- `events` (001/005, ReplacingMergeTree) — raw event stream, idempotent on
  `(project_id, message_id)`; the writer's insert target
- `sessions` (002, MergeTree) — session-level rollups
- `feature_flag_exposures` (006, ReplacingMergeTree) — idempotent flag
  evaluation results projected from events
- `frontend_health_events` (007, ReplacingMergeTree) — idempotent frontend
  errors and web-vitals projected from events
- `identity_alias_assertions` (011/016, ReplacingMergeTree) — retained
  tenant-bound,
  append-only `identify` assertions keyed by the complete claim; exact retries
  collapse, but reusing a message ID cannot retract an accepted identity command

**Materialized views**

- `feature_flag_exposures_mv` (006) — extracts flag fields from `events.properties` into `feature_flag_exposures`
- `frontend_health_events_mv` (007) — extracts error/web-vitals fields from `events.properties` into `frontend_health_events`
- `identity_alias_assertions_mv` (011) — projects only `identify` events that
  contain both canonical identity fields

`resolved_identity_aliases` resolves only when the minimum and maximum claimed
user IDs match. Conflicting claims stay visible with an empty resolved user and
Query leaves those actors separate. Migration 016 computes resolution directly
from retained assertions, so TTL or the supported deletion workflow cannot
leave irreversible aggregate state behind. The alias becomes visible after
writer durability and applies retroactively across retained event history. A
user-only `identify` is a trait update, not an alias assertion.

Historical recovery lives in `pipeline/clickhouse/backfills/`, outside the
replayed schema migrations. The initializer records each backfill's name and
SHA-256 checksum in `apdl_schema_backfills`, serializes runners with a local
single-writer lock, and executes the exact temporary snapshot it hashed. It
submits the retained-history scan only once and preserves distinct checksum
evidence so drift fails closed. The migration can recover only the current
`FINAL` form of identify events still inside the raw-event TTL; older or
already-overwritten pre-migration assertions cannot be reconstructed. A
missing backfill directory is an initialization error rather than an implicit
opt-out.

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
```
