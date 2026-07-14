# Config Service

Feature-flag and experiment configuration service for the **Autonomous Product
Development Loop** platform. Stores flags/experiments in PostgreSQL, serves
SDK bootstrap config from a Redis cache, and pushes live updates over SSE.

## What it does

- Stores canonical flag and experiment state in PostgreSQL. Each public write
  commits the record, optimistic version, audit entry, and delivery intent in
  one transaction
- Serves the SDK bootstrap payload (`GET /v1/flags`) from Redis (60s TTL,
  invalidated on every admin write; responses carry an `X-Cache: HIT|MISS` header)
- Delivers cache invalidation and `flag_update` / `experiment_update` SSE events
  at least once from a durable PostgreSQL outbox (plus a `heartbeat` every 35s)
- Evaluates `server`/`both`-mode gates on behalf of trusted backends
  (`POST /v1/evaluate`), durably enqueueing `$feature_flag_exposure` events
- Owns the canonical FNV-1a 32-bit bucketing implementation: hash of
  `{flag_key}:{salt}:{unit_id}` with a per-flag salt generated at create time.
  The JS and Python SDKs are byte-for-byte compatible, so a user buckets
  identically wherever a gate is evaluated

## API

**Auth:** send a registered credential as `x-api-key`. PostgreSQL supplies the
verified project and roles; a `project_id` query/body field can only assert that
same tenant. Admin routes require `config:write`, SDK reads require
`config:read`, and `/v1/evaluate` requires `config:evaluate`. Browser keys use
`client_{project_id}_{token}` and are restricted to exactly `events:write` plus
`config:read`; confidential service keys use `proj_{project_id}_{secret}`. All
credentials, including SSE credentials, are accepted only from `x-api-key` and
never from query parameters. See
[authentication](../../docs/authentication.md).

### SDK-facing

| Endpoint | Description |
|----------|-------------|
| `GET /v1/flags` | Bootstrap flag config (only `client`/`both` evaluation modes), Redis-cached |
| `GET /v1/stream` | SSE: initial `config` event, then `flag_update`/`experiment_update`/`heartbeat` |
| `GET /v1/auth/me` | Return the verified credential ID, project, and sorted roles |
| `POST /v1/evaluate` | Trusted server-side gate evaluation with optional exposure logging |
| `GET /v1/experiments/{key}/analysis` | Tenant-scoped authoritative experiment metadata delegated by Query (`query:read`) |
| `GET /health` | Liveness probe (PG, Redis, SSE connection count) |

### Admin (`/v1/admin`)

| Endpoint | Description |
|----------|-------------|
| `GET /flags` | List flags (`?include_archived=true` to include archived) |
| `GET /flags/stale` | Flags needing review/cleanup (`?older_than_days`, default 90) |
| `POST /flags` | Create a flag (409 on duplicate key) |
| `PUT /flags/{key}` | Partial update; requires current `version` (optimistic locking, 409 on conflict) |
| `POST /flags/{key}/transition` | Move a standalone flag to `draft` or `active`; body requires current `version` |
| `POST /flags/{key}/disable` | Canonical kill-switch path; body requires current `version`, reason, and evidence |
| `POST /flags/{key}/cleanup` | Archive an eligible fully rolled-out flag; body requires current `version` |
| `DELETE /flags/{key}?version=N` | Archive a standalone flag with optimistic locking |
| `GET /flags/{key}/audit` | Audit history (`?limit`, default 50, max 200) |
| `GET /experiments` / `POST /experiments` | List / create experiments |
| `PUT /experiments/{key}` | Atomically update an experiment and its backing flag; body requires current `version` |
| `DELETE /experiments/{key}?version=N` | Atomically delete an experiment and archive its backing flag |

Create a flag:

```bash
curl -X POST http://localhost:8081/v1/admin/flags \
  -H "x-api-key: proj_apdl_0123456789abcdef0123456789abcdef" \
  -H "content-type: application/json" \
  -d '{
    "key": "new-checkout",
    "name": "New checkout flow",
    "state": "active",
    "enabled": true,
    "owners": ["growth-team"],
    "default_variant": "control",
    "variants": [
      {"key": "control", "weight": 1},
      {"key": "treatment", "weight": 1}
    ],
    "fallthrough": {"rollout": {"percentage": 25, "bucket_by": "user_id"}}
  }'
```

## Flag lifecycle

Standalone flag states are `draft`, `active`, `disabled`, and terminal
`archived`. `enabled` is derived from state and is never accepted by generic
update: use the dedicated transition, disable, cleanup, and archive commands.
Every command requires the current version, bumps it, and writes an audit entry.
An experiment owns its backing flag; generic flag commands reject that flag and
only the atomic experiment command may project experiment lifecycle, variants,
targeting, and rollout onto it.

Experiments use timezone-aware `start_date` and `end_date` values and the
`draft`, `scheduled`, `running`, `completed`, or `stopped` states. Authoring
requires 2-10 unique, positive-weight variants and an explicit
`default_variant`; that one field is both the statistical control and the
backing flag's fallback variant. Primary metrics are conversion events only, and a complete
window is limited to 90 days. Scheduled and running experiments require the
metric and window. After an experiment leaves `draft`, its default/control
variant, variants, primary metric, and start date are immutable. The planned
end date is also immutable to callers; an early `completed`/`stopped`
transition atomically shortens it to the actual terminal time so later events
cannot drift terminal results. The lifecycle worker atomically starts and
completes due experiments. Stopping a draft or scheduled experiment clears its
analysis end and remains non-analyzable (409), because it never started. If the
scheduler misses an entire scheduled window, it takes that same fail-closed
path instead of manufacturing a completed run.

Automatic guardrail mutation is unavailable in the OSS developer preview.
`auto_disable` is therefore fixed to `false` in public writes and persisted
state; on-demand guardrail evaluation remains read-only.

`GET /v1/experiments/{key}/analysis` reads only the tenant's authoritative
PostgreSQL experiment record. Its strict response contains exactly `key`,
`flag_key`, `status`, `control_variant`, `variants`, `metric_event`,
`start_date`, `end_date`, and `version`. Missing experiments return 404. Draft
or malformed/incomplete analysis contracts return 409 instead of guessing or
deriving metadata from events.

## Targeting contract

Config, the JavaScript SDK, the Python SDK, and the Admin evaluator execute the
same `fixtures/gates/targeting.json` cases. Supported operators are `equals`,
`not_equals`, `gt`, `gte`, `lt`, `lte`, `contains`, `not_contains`,
`starts_with`, `ends_with`, `in`, `not_in`, `exists`, and `not_exists`.
`exists`/`not_exists` omit `value`; explicit null is invalid. Regex is not part
of the cross-runtime contract. Identifiers, strings, rule counts, condition
counts, and membership lists are bounded and malformed rules fail closed.

## Developer-preview deployment boundary

This release supports one Config process and a fresh PostgreSQL installation.
Config holds a PostgreSQL advisory lock for its lifetime and refuses to start a
second process. The outbox is durable and retryable, but cross-process SSE
fan-out is not implemented; multi-replica Config operation and in-place legacy
database upgrades are unsupported.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `POSTGRES_URL` | `postgresql://apdl:apdl_dev@localhost:5432/apdl` | Authoritative flag/experiment/outbox store (must be migrated before startup) |
| `PG_POOL_SIZE` | `4` | Max asyncpg pool size |
| `REDIS_URL` | `redis://localhost:6379` | Flag cache + exposure event stream |
| `EXPERIMENT_LIFECYCLE_ENABLED` | `true` | Run scheduled-start/completion sweeps |
| `EXPERIMENT_LIFECYCLE_INTERVAL_SECONDS` | `300` | Lifecycle sweep interval |

## Running locally

```bash
make dev          # start Redis, ClickHouse, PostgreSQL
make run-config   # hot-reload server on http://localhost:8081
```

## Tests

```bash
make test-config  # pytest
make lint-config  # ruff
```

Evaluator tests pin bucketing against `fixtures/gates/parity.json` and targeting
semantics against `fixtures/gates/targeting.json`; all four evaluators consume
the shared targeting fixture.
