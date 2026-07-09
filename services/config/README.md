# Config Service

Feature-flag and experiment configuration service for the **Autonomous Product
Development Loop** platform. Stores flags/experiments in PostgreSQL, serves
SDK bootstrap config from a Redis cache, and pushes live updates over SSE.

## What it does

- Stores canonical flag configs (rules, rollouts, guardrails, lifecycle state)
  in PostgreSQL with a per-flag audit log
- Serves the SDK bootstrap payload (`GET /v1/flags`) from Redis (60s TTL,
  invalidated on every admin write; responses carry an `X-Cache: HIT|MISS` header)
- Broadcasts `flag_update` / `experiment_update` SSE events to connected SDKs
  the moment a flag changes (plus a `heartbeat` every 35s)
- Evaluates `server`/`both`-mode gates on behalf of trusted backends
  (`POST /v1/evaluate`), publishing `$feature_flag_exposure` events to Redis Streams
- Owns the canonical FNV-1a 32-bit bucketing implementation: hash of
  `{flag_key}:{salt}:{unit_id}` with a per-flag salt generated at create time.
  The JS and Python SDKs are byte-for-byte compatible, so a user buckets
  identically wherever a gate is evaluated

## API

**Auth:** send a registered credential as `x-api-key`. PostgreSQL supplies the
verified project and roles; a `project_id` query/body field can only assert that
same tenant. Admin routes require `config:write`, SDK reads require
`config:read`, and `/v1/evaluate` requires `config:evaluate`. See
[authentication](../../docs/authentication.md).

### SDK-facing

| Endpoint | Description |
|----------|-------------|
| `GET /v1/flags` | Bootstrap flag config (only `client`/`both` evaluation modes), Redis-cached |
| `GET /v1/stream` | SSE: initial `config` event, then `flag_update`/`experiment_update`/`heartbeat` |
| `GET /v1/auth/me` | Return the verified credential ID, project, and sorted roles |
| `POST /v1/evaluate` | Trusted server-side gate evaluation with optional exposure logging |
| `GET /health` | Liveness probe (PG, Redis, SSE connection count) |

### Admin (`/v1/admin`)

| Endpoint | Description |
|----------|-------------|
| `GET /flags` | List flags (`?include_archived=true` to include archived) |
| `GET /flags/stale` | Flags needing review/cleanup (`?older_than_days`, default 90) |
| `POST /flags` | Create a flag (409 on duplicate key) |
| `PUT /flags/{key}` | Partial update; requires current `version` (optimistic locking, 409 on conflict) |
| `POST /flags/{key}/disable` | Canonical rollback path (guardrail/rollback reasons, audit evidence) |
| `POST /flags/{key}/cleanup` | Archive a fully-rolled-out flag (no rules, fallthrough `true` at 100%) |
| `DELETE /flags/{key}` | Archive a flag (soft delete) |
| `GET /flags/{key}/audit` | Audit history (`?limit`, default 50, max 200) |
| `GET /experiments` / `POST /experiments` | List / create experiments |
| `PUT /experiments/{key}` / `DELETE /experiments/{key}` | Update / delete an experiment |

Create a flag:

```bash
curl -X POST http://localhost:8081/v1/admin/flags \
  -H "x-api-key: proj_demo_0123456789abcdef" \
  -H "content-type: application/json" \
  -d '{
    "key": "new-checkout",
    "name": "New checkout flow",
    "state": "active",
    "enabled": true,
    "owners": ["growth-team"],
    "fallthrough": {"value": true, "rollout": {"percentage": 25, "bucket_by": "user_id"}}
  }'
```

## Flag lifecycle

States: `draft` → `active` → `disabled`, plus `archived` (terminal, via
DELETE/cleanup). `enabled` is derived: a flag is enabled iff `state == "active"`
(enforced by a DB check constraint and request validation). Disabling records
`disabled_reason` / `disabled_by` / `disabled_at`; guardrails (`guardrails`,
e.g. `frontend_error_rate` at `2x_baseline`) let the system auto-disable a flag
through `POST /flags/{key}/disable` with `source: "system"` — refused with 409
if the flag has `auto_disable: false`. Every mutation bumps `version` and
writes an audit entry.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `POSTGRES_URL` | `postgresql://apdl:apdl_dev@localhost:5432/apdl` | Flag/experiment store (schema auto-migrates on startup) |
| `PG_POOL_SIZE` | `4` | Max asyncpg pool size |
| `REDIS_URL` | `redis://localhost:6379` | Flag cache + exposure event stream |

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

Evaluator tests pin against `fixtures/gates/parity.json` — golden hash and
evaluation fixtures also loaded by the JS SDK's tests (the Python SDK pins the
same golden values inline), guaranteeing cross-SDK bucketing parity.
