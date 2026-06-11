# Query Service

Analytics query engine for the **Autonomous Product Development Loop** platform.
Runs funnels, retention, cohort comparisons, and experiment statistics directly
against ClickHouse over FastAPI (port **8082**).

## What it does

- Translates declarative event selectors into parameterized ClickHouse SQL
  against the `events` table (funnels use `windowFunnel`)
- Computes experiment results from `$experiment_exposure` events with
  frequentist, Bayesian, or sequential statistics (SciPy/NumPy)
- Evaluates feature-flag guardrails (frontend error rate/count) against the
  `feature_flag_exposures` and `frontend_health_events` tables, with an
  optional background monitor that polls and reports to the config service
- Maintains an async ClickHouse connection pool (native protocol via `asynch`)

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/query/events/count` | Event + unique-user counts for 1–20 selectors |
| `POST` | `/v1/query/events/timeseries` | Time-bucketed counts (`1 HOUR`/`1 DAY`/`1 WEEK`/`1 MONTH`) |
| `POST` | `/v1/query/events/breakdown` | Top property values for a selector |
| `POST` | `/v1/query/funnel` | N-step funnel (2–20 steps, 1–90 day window) |
| `POST` | `/v1/query/cohort` | Compare a metric across values of a user property |
| `POST` | `/v1/query/retention` | Day/week retention from a cohort selector to a return selector |
| `GET`  | `/v1/query/experiment/{id}?metric=...&method=...` | Statistical experiment analysis |
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

### Example: event counts

```bash
curl -s http://localhost:8082/v1/query/events/count \
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

`GET /v1/query/experiment/{id}` joins `$experiment_exposure` assignments with
post-exposure metric events (zero-filling exposed non-converters) and runs one
of three implemented methods:

- **`frequentist`** — Welch's t-test with Cohen's d effect size and a CI for
  the difference in means
- **`bayesian`** — Beta-Binomial conversion model (metric binarized as
  converted/not), Monte Carlo `P(treatment > control)`, expected loss;
  significant past 95%
- **`sequential`** — mixture sequential probability ratio test (mSPRT) with an
  always-valid p-value, safe to peek at any time

The analyzer also ships CUPED variance reduction and sample-size calculation
helpers (not yet exposed over HTTP).

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host (native protocol) |
| `CLICKHOUSE_PORT` | `9000` | ClickHouse native port |
| `CLICKHOUSE_USER` | `default` | ClickHouse user |
| `CLICKHOUSE_PASSWORD` | `""` | ClickHouse password |
| `CLICKHOUSE_DB` | `apdl` | Database name |
| `CLICKHOUSE_POOL_SIZE` | `10` | Connection pool size |
| `DEFAULT_PROJECT_ID` | `default` | Fallback project for experiment queries |
| `GUARDRAIL_MONITOR_ENABLED` | `false` | Enable the background guardrail monitor |
| `GUARDRAIL_PROJECT_IDS` | `""` | Comma-separated projects to monitor |
| `GUARDRAIL_MONITOR_INTERVAL_SECONDS` | `60` | Monitor poll interval |
| `CONFIG_SERVICE_URL` | `http://localhost:8081` | Config service for guardrail actions |

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
