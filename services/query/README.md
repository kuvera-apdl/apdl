# Query Service

Analytics query engine for the **Autonomous Product Development Loop** platform.
Runs funnels, retention, cohort comparisons, and experiment statistics directly
against ClickHouse over FastAPI (port **8082**).

## What it does

- Translates declarative event selectors into parameterized ClickHouse SQL
  against the `events` table (funnels use `windowFunnel`)
- Computes conversion experiment results from authoritative Config metadata
  and first-exposure attribution, including crossover diagnostics and
  Bonferroni-corrected comparisons against the declared control
- Evaluates feature-flag guardrails read-only against the
  `feature_flag_exposures` and `frontend_health_events` tables
- Enforces bounded execution time, ClickHouse read/result/memory limits, and
  fail-fast per-project concurrency across every analytics route

## API

Every analytics route requires a registered `X-API-Key` credential with
`query:read`. The requested `project_id` must match the project on the verified
credential; health and readiness probes remain public.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/query/events/count` | Event + unique-user counts for 1–20 selectors |
| `POST` | `/v1/query/events/timeseries` | Time-bucketed counts (`1 HOUR`/`1 DAY`/`1 WEEK`/`1 MONTH`) |
| `POST` | `/v1/query/events/breakdown` | Top property values for a selector |
| `POST` | `/v1/query/funnel` | N-step funnel (2–20 steps, 1–90 day window) |
| `POST` | `/v1/query/cohort` | Compare a metric across values of a user property |
| `POST` | `/v1/query/retention` | Day/week retention from a cohort selector to a return selector |
| `GET`  | `/v1/query/experiment/{key}` | Config-owned conversion experiment analysis |
| `POST` | `/v1/query/guardrails/evaluate` | Evaluate a feature-flag guardrail on demand |
| `GET`  | `/health` / `/ready` | Liveness / ClickHouse readiness probes |

## Event selectors

Every analytics endpoint takes the same `EventSelector` shape: an `event_name`
plus up to 25 property filters, AND-combined. Supported operators: `eq`, `neq`,
`in`, `not_in`, `exists`, `not_exists`, `contains`, `gt`, `gte`, `lt`, `lte`.

```json
{
  "event_name": "$click",
  "filters": [
    { "property": "href", "operator": "eq", "value": "/pricing" }
  ]
}
```

See the [main README](../../README.md) for more selector examples across
endpoints.

## Actor identity and totals

All user-counting queries use the tenant-bound canonical actor contract. A
direct `user_id` wins; otherwise an anonymous identity with one unambiguous,
writer-durable alias resolves to that user; otherwise the namespaced anonymous
identity remains separate. Alias assertions are irreversible and apply
retroactively across retained history. Conflicting aliases are never guessed
or merged: the actors remain split, and experiment responses report degraded
identity quality instead of a decision snapshot. Events with neither identity
do not contribute to unique-user counts. Range-wide totals use `uniqExact`
directly and are never derived by summing per-bucket unique counts.

### Example: event counts

```bash
curl -s http://localhost:8082/v1/query/events/count \
  -H 'X-API-Key: proj_myproject_<secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "my-project",
    "start_date": "2026-05-01",
    "end_date": "2026-05-31",
    "selectors": [
      {
        "event_name": "$click",
        "filters": [{ "property": "href", "operator": "eq", "value": "/pricing" }]
      }
    ]
  }'
```

## Experiment statistics

`GET /v1/query/experiment/{key}` accepts only the experiment key and optional
tenant-matching `project_id`. Query obtains the flag key, declared variants,
control, conversion metric, lifecycle state, immutable analysis window, and
version from Config; caller-supplied analysis metadata is rejected.

The query assigns each namespaced actor to its first exposure, counts only
post-exposure conversions inside the authoritative window, zero-fills exposed
non-converters, reports crossover and unknown-variant actors, and compares
every treatment with the declared control. Responses are a strict
`analysis_status` union: `ready` contains finite two-proportion statistics with
Bonferroni correction; `insufficient_data` contains a machine-readable reason.
Config timestamps are converted to explicit UTC epoch-millisecond boundaries
before querying ClickHouse's `DateTime64(3)` columns, preserving the declared
half-open `[start, end)` window across offsets and fractional seconds.
Scheduled experiments do not query ClickHouse. Draft or malformed experiments
are rejected by Config. Automatic stopping, shipping, and rollback are not
supported in the OSS developer preview.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host (native protocol) |
| `CLICKHOUSE_PORT` | `9000` | ClickHouse native port |
| `CLICKHOUSE_USER` | `default` | ClickHouse user |
| `CLICKHOUSE_PASSWORD` | `""` | ClickHouse password |
| `CLICKHOUSE_DB` | `apdl` | Database name |
| `CLICKHOUSE_POOL_SIZE` | `10` | Connection pool size |
| `CONFIG_SERVICE_URL` | `http://localhost:8081` | Authoritative experiment metadata service |
| `POSTGRES_URL` | `postgresql://apdl:apdl_dev@localhost:5432/apdl` | Hashed credential registry |
| `QUERY_TIMEOUT_SECONDS` | `10` | Wall-clock and ClickHouse execution limit (1–30s) |
| `QUERY_MAX_CONCURRENT_PER_PROJECT` | `2` | Fail-fast active-query limit per project (1–10) |
| `QUERY_MAX_ROWS_TO_READ` | `5000000` | ClickHouse rows-read limit |
| `QUERY_MAX_BYTES_TO_READ` | `536870912` | ClickHouse bytes-read limit |
| `QUERY_MAX_RESULT_ROWS` | `10000` | Maximum result rows |
| `QUERY_MAX_RESULT_BYTES` | `16777216` | Maximum result bytes |
| `QUERY_MAX_MEMORY_BYTES` | `536870912` | Per-query ClickHouse memory limit |
| `QUERY_MAX_THREADS` | `4` | Per-query ClickHouse thread limit |

Automatic guardrail enforcement is disabled in the OSS developer preview.
Guardrail queries are read-only and never mutate Config state.

Experiment analysis synchronously delegates the already authenticated
project-scoped `X-API-Key` to Config. Config independently reauthenticates it
and permits only its read-only analysis projection to credentials carrying
`query:read`; no second service credential or caller-selected project is used.

## Running locally

```bash
make dev         # start Redis, ClickHouse, PostgreSQL (Docker)
make run-query   # hot-reload server → http://localhost:8082
```

Queries need events in ClickHouse — run `make run-pipeline` so the ClickHouse
Writer consumes Redis Streams and populates the `events` table.

## Tests

```bash
make test-query   # pytest (pytest-asyncio)
make lint-query   # ruff
```
