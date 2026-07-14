# Ingestion Service

FastAPI service (port **8080**) that receives event batches from the APDL SDKs,
authenticates and validates them, and enqueues them onto Redis Streams for the
[ClickHouse writer pipeline](../../pipeline/) to consume. See the
[monorepo README](../../README.md) for the big picture.

## What it does

Each `POST /v1/events` request goes through four stages:

1. **Auth** — verifies the `x-api-key` header against the hashed PostgreSQL
   credential registry, then derives project authority and roles from that
   record. Confidential keys use `proj_{project_id}_{secret}`; browser-safe keys
   use `client_{project_id}_{token}` and are database-limited to exactly
   `events:write` plus `config:read`. The embedded project and stored
   kind/prefix are checked for consistency but never trusted as authority.
   Invalid, expired, or revoked keys return 401; keys without `events:write`
   return 403. See [authentication](../../docs/authentication.md).
2. **Rate limit** — a Redis-backed token bucket shared by every process
   (capacity 1000, refill 100 units/s). Each request costs one unit per event
   plus one unit per KiB received. Exhausted buckets →
   `429 {"error": "rate_limited"}` with `Retry-After`, `X-RateLimit-Limit`, and
   `X-RateLimit-Remaining` headers; an unavailable quota authority fails closed.
3. **Validation** — the batch must be exactly `{"events": [...]}` with 1–100
   events. Every event requires `event`, `type`, a canonical RFC3339 UTC
   `timestamp` (`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`),
   nested `context`, a stable `message_id`, and at least one of `user_id` or
   `anonymous_id`. Types are `track`, `identify`, `group`, and `page` only;
   lifecycle events use the canonical event names `identify`, `group`, and
   `page`. Camel-case aliases and unknown fields are rejected. Limits: event
   name ≤ 256 chars, property keys ≤ 256 chars, string property values ≤ 8192
   chars. The public boundary also rejects duplicate JSON keys, non-finite
   numbers, requests over 512 KiB, events over 64 KiB, nesting deeper than 10,
   containers over 100 entries, and events over 1000 JSON nodes. Reserved events
   (`$feature_flag_exposure`, `$frontend_error`, `$web_vital`) get strict
   envelope/property checks. Optional event/context fields use omission as the
   only absent form; explicit `null` is rejected. Failures →
   `400 {"error": "validation_failed",
   "errors": [{"field", "message"}, ...]}` with every error collected, not
   just the first.
4. **Atomic XADD** — each event is enriched with `server_timestamp`, client `ip`
   (from `X-Forwarded-For`/`X-Real-IP`), and `project_id`, then published as
   compact JSON to the Redis Stream `events:raw:{project_id}` in one Redis
   transaction with approximate `MAXLEN ~ 1000000` trimming. Success →
   `202 {"accepted": N}` only after the complete transaction. Any known or
   ambiguous transaction failure returns `503`; clients retry the same stable
   `message_id` values and ClickHouse deduplicates them.

## API

| Method | Path         | Description                                              |
|--------|--------------|----------------------------------------------------------|
| POST   | `/v1/events` | Ingest a batch of events (returns `202` on acceptance)   |
| GET    | `/health`    | Liveness probe — pings Redis, `200` ok / `503` degraded  |

```bash
curl -X POST http://localhost:8080/v1/events \
  -H "x-api-key: client_apdl_0123456789abcdef0123456789abcdef" \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {
        "event": "order_completed",
        "type": "track",
        "user_id": "u_123",
        "timestamp": "2026-06-09T12:00:00.000Z",
        "message_id": "8e03c6fd-5923-4a08-acbc-02915ed0ab5a",
        "properties": {"total": 42.0, "currency": "USD"},
        "context": {
          "library": {"name": "example", "version": "1.0.0"},
          "page": {
            "url": "https://example.test/checkout",
            "title": "Checkout",
            "path": "/checkout",
            "search": ""
          }
        }
      },
      {
        "event": "identify",
        "type": "identify",
        "user_id": "u_123",
        "timestamp": "2026-06-09T12:00:01.000Z",
        "message_id": "053fa345-8a0c-4bac-9df5-ce973cb0dcd3",
        "traits": {"plan": "pro"},
        "context": {"library": {"name": "example", "version": "1.0.0"}}
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

JSON, rate-limit, and stream-trim settings are compile-time constants under
`app/validation/json_contract.py`, `app/middleware/rate_limit.py`, and
`app/streaming/redis_producer.py`.

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
make test-packed-sdk-contract  # installed npm tarball → real validator

# single test file
cd services/ingestion && .venv/bin/python -m pytest tests/test_events.py -v
```
