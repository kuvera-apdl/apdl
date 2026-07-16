# Ingestion Service

FastAPI service (port **8080**) that receives event batches from the APDL SDKs,
authenticates and validates them, and enqueues them onto Redis Streams for the
[ClickHouse writer pipeline](../../pipeline/) to consume. See the
[monorepo README](../../README.md) for the big picture.

## What it does

Each `POST /v1/events` request goes through four stages:

1. **Auth** — verifies the `x-api-key` header against the hashed PostgreSQL
   credential registry, then derives project authority and roles from that
   record. The project embedded in `proj_{project_id}_{secret}` is checked for
   consistency but never trusted as authority.
   Invalid, expired, or revoked keys return 401; keys without `events:write`
   return 403. See [authentication](../../docs/authentication.md).
2. **Rate limit** — in-memory token bucket per project (capacity 1000,
   refill 100 tokens/s). Exhausted buckets → `429 {"error": "rate_limited"}`
   with `Retry-After`, `X-RateLimit-Limit`, and `X-RateLimit-Remaining` headers.
3. **Validation** — the batch must be `{"events": [...]}` with 1–500 events.
   Each event needs an `event` name or a `type` (one of `track`, `identify`,
   `group`, `page`, `screen`, `alias`) plus a `user_id`/`userId` or
   `anonymous_id`/`anonymousId`. Limits: event name ≤ 256 chars, property
   keys ≤ 256 chars, string property values ≤ 8192 chars. Reserved events
   (`$feature_flag_exposure`, `$frontend_error`, `$web_vital`) get strict
   envelope/property checks. Failures → `400 {"error": "validation_failed",
   "errors": [{"field", "message"}, ...]}` with every error collected, not
   just the first.
4. **XADD** — each event is enriched with `server_timestamp`, client `ip`
   (from `X-Forwarded-For`/`X-Real-IP`), and `project_id`, then published as
   compact JSON to the Redis Stream `events:raw:{project_id}` with approximate
   `MAXLEN ~ 1000000` trimming. Success → `202 {"accepted": N}` (plus
   `"failed": M` on partial publish failures); if nothing could be enqueued →
   `503 {"error": "service_unavailable"}`.

## API

| Method | Path         | Description                                              |
|--------|--------------|----------------------------------------------------------|
| POST   | `/v1/events` | Ingest a batch of events (returns `202` on acceptance)   |
| GET    | `/health`    | Liveness probe — pings Redis, `200` ok / `503` degraded  |

```bash
curl -X POST http://localhost:8080/v1/events \
  -H "x-api-key: proj_demo_0123456789abcdef" \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {
        "event": "order_completed",
        "type": "track",
        "user_id": "u_123",
        "timestamp": "2026-06-09T12:00:00.000Z",
        "properties": {"total": 42.0, "currency": "USD"},
        "context": {"page": "/checkout"}
      },
      {
        "type": "identify",
        "user_id": "u_123",
        "traits": {"plan": "pro"}
      }
    ]
  }'
# → 202 {"accepted": 2}
```

## Configuration

| Variable    | Default                  | Description                          |
|-------------|--------------------------|--------------------------------------|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection for stream output   |
| `POSTGRES_URL` | `postgresql://apdl:apdl_dev@localhost:5432/apdl` | Hashed credential registry |

Rate-limit and stream-trim settings are compile-time constants
(`app/middleware/rate_limit.py`, `app/streaming/redis_producer.py`).

## Running locally

```bash
make dev            # start Docker deps (Redis, ClickHouse, PostgreSQL)
make run-ingestion  # uvicorn with hot-reload → http://localhost:8080
```

Or run the whole stack: `make dev-all`.

## Tests

```bash
make test-ingestion   # pytest
make lint-ingestion   # ruff

# single test file
cd services/ingestion && .venv/bin/python -m pytest tests/test_events.py -v
```
