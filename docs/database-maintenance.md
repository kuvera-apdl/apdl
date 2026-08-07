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

Fresh Compose clusters bootstrap seven dedicated identities before the schema
migrator runs:

- `apdl_runtime` is the shared login used by ordinary long-running services. It
  is not a superuser, database owner, role creator, or member of another role.
- `apdl_agents` is the dedicated non-owner login used by Agents. It is the only
  application identity that can insert or delete short-lived
  `agent_service_capabilities`, and its table, column, and sequence grants are
  limited to the Agents data paths. Ordinary runtime services can validate read
  capabilities and atomically consume request-bound mutation capabilities, but
  cannot mint or reset one.
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
- `apdl_project_authority_definer` is a `NOLOGIN` owner for the exact routines
  that lock project-management authority and add initial owner analysis roles
  with audit evidence. Agents cannot update Admin memberships directly.
- `apdl_capability_consumer_definer` is a `NOLOGIN` owner for the atomic
  mutation-capability consume routine. It can update only `consumed_at` and is
  never granted to a login.

`apdl_runtime` is a shared non-owner boundary, not per-service least privilege:
ordinary long-running services use the same credential, and the migration
sequence grants it ordinary application-table access across the APDL schema. A
compromised ordinary service therefore has cross-service database reach, but
cannot mint an Agents capability, read `llm_vault_provider_secrets`, own schema
objects, assume another role, mutate immutable audit history, or invoke audited
purge. `apdl_agents` has an explicit allowlist of Agents-owned tables, selected
read-only authority projections, three sequences, and exact capability insert
columns. It cannot read Admin session/password/invitation material, mutate
Config or Codegen tables, or rewrite project membership roles. Splitting the
remaining ordinary services per service is outside this developer-preview
release.

The `apdl` database owner remains migration-only. Do not place its URL in a
service deployment. If an existing local `.env` still contains an owner-valued
`POSTGRES_URL`, replace it with the `apdl_runtime` URL from `.env.example`
before starting an ordinary host-run service. Agents must instead use the
`apdl_agents` URL constructed from `APDL_AGENTS_POSTGRES_PASSWORD`; the host
service runner and Compose do this automatically. Use the `apdl_llm_vault` URL
only for the vault and its offline key-rotation command.

Keep `APDL_LLM_VAULT_POSTGRES_PASSWORD` available to every supported
PostgreSQL migration run, not only the initial bootstrap or migration 056. The
migrator reconciles the `apdl_llm_vault` login password before it plans or
applies migrations. Omitting the variable activates only the schema-validation
path, skips password reconciliation, and can leave the database role out of sync
with the vault service's `POSTGRES_URL`. Supply the same explicit password to
both boundaries on every deployment migration.

`infra/docker/postgres/init-apdl-roles.sh` is strictly a fresh-`initdb`
bootstrap. For an existing database with an exact checksummed migration-ledger
prefix, the PostgreSQL migrator provisions and validates `apdl_agents`,
`apdl_project_authority_definer`, and `apdl_capability_consumer_definer` before
applying migration `058_agent_service_capabilities.sql`. The Agents password is
required and must satisfy the same URI-unreserved format as the fresh-cluster
bootstrap. The migrator restores the fixed login and `NOLOGIN` attributes,
rejects role membership in either direction, and revokes `CREATE` authority on
the public schema before it changes the schema.

This exact-prefix path does not turn an unversioned database into a supported
upgrade. It also does not qualify the older high-lock migrations called out
below for populated production databases; those still require the documented
fresh-database or separately reviewed cutover path.

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

### Migration 051 is an explicit Agents setup cutover

Migration `051_agents_project_setup.sql` deliberately deactivates every Agents
project and removes the pre-cutover tier assignments, runtime provider-policy
copies, and model inventories that lack reviewed catalog pricing. It preserves
positive project and per-run limits; only rows retaining a non-positive
bootstrap limit receive the new defaults. The provider connections themselves
remain, but their model projections must be refreshed against the deployed
catalog before an owner can select both tiers and reactivate analysis.

Stop Agents workers before applying this cutover. Record the projects and
providers that must be reconfigured, apply the migration, deploy a release with
current reviewed price metadata, refresh each inventory, reselect both model
tiers, and reactivate deliberately. There is no live provider-pricing lookup:
release maintainers own catalog price updates and operators must keep a project
inactive whenever those values are known to be stale.

Migration checksums are immutable. A development database that applied an
earlier, unmerged form of migration 051 must be rebuilt from an empty database;
the migration ledger intentionally rejects that checksum drift.

### Migration 056 requires a fresh database

Migration `056_project_llm_credential_vault.sql` replaces the separate Agents
and Codegen credential stores with one shared vault. It rejects any row in
either legacy credential table, including replaced or revoked history. Those
rows retain credential lineage after their secret bytes are crypto-shredded,
and the migration cannot rebind that lineage safely to the new empty vault.

Revocation is therefore not a remediation for an existing database. Initialize
a fresh PostgreSQL database, apply the complete canonical migration sequence,
and reconnect provider credentials through the shared vault. There is no
supported in-place conversion or dual-schema compatibility mode.

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
