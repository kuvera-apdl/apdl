# Database migration maintenance

PostgreSQL and ClickHouse migrations are checksummed, forward-only operations.
The supported entry points are:

```bash
make migrate-postgres
make migrate-clickhouse
```

`make dev`, `make dev-core`, and `make dev-all` invoke those entry points in
dependency order. A bare `docker compose up` is not a migration entry point.
In particular, ClickHouse maintenance coordinates through PostgreSQL, so a
PostgreSQL outage blocks ClickHouse migration and ClickHouse-only deployment is
not supported.

## PostgreSQL role boundary

Fresh Compose clusters bootstrap four dedicated identities before the schema
migrator runs:

- `apdl_runtime` is the shared login used by ordinary long-running services. It
  is not a superuser, database owner, role creator, or member of another role.
- `apdl_llm_vault` is the dedicated login used only by the project LLM
  credential vault. It alone can read provider ciphertext, can write the
  consumer projections it owns, and cannot update immutable vault audit rows.
- `apdl_audit_operator` is a `NOLOGIN` caller role. A named human or automation
  login may be granted this role only through the deployment's reviewed access
  workflow; it receives narrow audit preview/verification reads plus execute
  authority for the audited purge function, not direct audit-table deletion.
- `apdl_audit_purge_definer` is a `NOLOGIN` function-owner role with only the
  narrow object privileges needed by that `SECURITY DEFINER` function. It is
  never granted to runtime or operator callers.

`apdl_runtime` is a shared non-owner boundary, not per-service least privilege:
ordinary long-running services use the same credential and migration `044`
grants it ordinary application-table access across the APDL schema. A
compromised ordinary service therefore has cross-service database reach, but
cannot read `llm_vault_provider_secrets`, own schema objects, assume another
role, mutate immutable audit history, or invoke audited purge. The credential
vault is the deliberate exception with its narrower dedicated role. Splitting
the remaining ordinary services per service is outside this developer-preview
release.

The `apdl` database owner remains migration-only. Do not place its URL in a
service deployment. If an existing local `.env` still contains an owner-valued
`POSTGRES_URL`, replace it with the `apdl_runtime` URL from `.env.example`
before starting an ordinary host-run service; use the `apdl_llm_vault` URL only
for the vault and its offline key-rotation command.

`infra/docker/postgres/init-apdl-roles.sh` is strictly a fresh-`initdb`
bootstrap. In-place upgrades are unsupported, so do not attempt to retrofit
these roles or apply migration `044` to an existing APDL database. Provision a
fresh database and run the complete checksummed migration sequence instead.

## Before a migration

Drain or stop every APDL process that uses PostgreSQL and stop the singleton
ClickHouse writer. The initialization scripts verify the supported Compose
services are quiescent; the database barriers then protect against a process
starting after that check:

- each runtime PostgreSQL connection holds both shared maintenance advisory
  locks while it can issue work;
- each migration holds both locks exclusively for the complete operation;
- ClickHouse writes also pass a durable `apdl_maintenance_gate` predicate; and
- the migration runner continuously verifies both independent fence-owner
  sessions while a database command is active.

The operator-owned timeout settings are:

| Variable | Default | Range | Meaning |
|---|---:|---:|---|
| `APDL_MAINTENANCE_DRAIN_TIMEOUT_SECONDS` | `30` | 1–900 | Maximum wait to acquire the exclusive runtime barriers |
| `APDL_MAINTENANCE_OPERATION_TIMEOUT_SECONDS` | `3600` | 1–86400 | Maximum duration of one supervised migration operation |

Increase a timeout only after measuring the target table and confirming the
maintenance window.

### Migration 034 sizing and lock risk

Migration `034_agent_project_execution_lane.sql` is classified as
fresh-install-only for the current developer preview. On a populated
`agent_runs` table, adding its stored generated column rewrites existing rows,
the validation queries scan the table and approval effects, and its
non-concurrent unique-index build scans and sorts the full live lane set.
`ALTER TABLE` and non-concurrent index creation also take locks that block
normal writers. The transaction rollback path can take material time after a
large rewrite and does not remove the WAL, temporary-disk, replica-lag, or
backup-window cost already incurred.

Do not apply `034` in place to a populated preview database. Provision a fresh
database and migrate data only under a separately reviewed cutover plan. Before
promoting a future in-place path, rehearse against a production-sized snapshot,
measure `pg_total_relation_size('agent_runs')`, active-lane cardinality, index
build time, peak WAL and temporary disk, replica catch-up, and transaction
rollback time. Reserve capacity for both the table rewrite and new index plus
operational headroom; a schema timeout is a safety bound, not a sizing plan.

### Migration 044 is also fresh-install-only

Migration `044_operator_recovery_and_retention.sql` creates non-concurrent
indexes over `config_outbox` and `experiment_audit_log` inside its migration
transaction. On a populated database those builds can scan and lock retained
rows and consume material WAL, temporary disk, and replica time. This release
does not qualify that upgrade path: the migration is covered only as part of a
fresh, empty canonical sequence. Do not use the fresh-cluster role bootstrap or
migration `044` as an in-place upgrade procedure.

## Failure and rerun

Do not edit an applied migration. Preserve the logs and rerun the same supported
command after the dependency or capacity problem is repaired. The migration
ledger verifies the exact version, name, checksum, and contiguous prefix before
continuing.

A ClickHouse failure can deliberately leave `apdl_maintenance_gate` closed.
That is a fail-closed state, not permission to write around the predicate. A
clean rerun validates the ledger and opens the gate only after the migration
sequence completes. Confirm the final state with:

```sql
SELECT count() = uniqExact(generation) AS unique_generations,
       argMax(writes_blocked, generation) AS writes_blocked
FROM apdl_maintenance_gate
WHERE authority = 'runtime-writes';
```

The expected result after a successful rerun is `1, 0`. Keep the writer stopped
and investigate if the authority row is missing, has duplicate generations, or
reports `writes_blocked = 1`.

## Durable ClickHouse owner recovery

`apdl_active_maintenance` is a crash-surviving ownership marker. Never drop it
just because a migration client disappeared. First read its exact token:

```sql
SELECT toString(run_token) FROM apdl_active_maintenance;
```

Then prove all of the following for that token:

1. no `system.processes.query_id` starts with
   `apdl-maintenance-{run_token}-`;
2. no migrator container or host process for that token is alive;
3. both PostgreSQL exclusive maintenance owners are absent; and
4. the singleton writer and all other runtime services remain stopped.

Only a database operator in the maintenance window may then remove the stale
marker:

```sql
DROP TABLE apdl_active_maintenance;
```

Rerun `make migrate-clickhouse` immediately. Do not manually open
`apdl_maintenance_gate`; the rerun owns that transition and verifies it.

## Verification

The repository exercises the protocol through unit tests, a real PostgreSQL
fence-owner termination/rollback probe in both fresh-install smokes, a real
closed-gate ClickHouse insert rejection in the upgrade smoke, and exact
checksummed fresh migration runs.
