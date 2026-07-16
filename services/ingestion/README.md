# Ingestion Service

FastAPI service (port **8080**) that receives event batches from the APDL SDKs,
authenticates and validates them, and enqueues them onto Redis Streams for the
[ClickHouse writer pipeline](../../pipeline/) to consume. See the
[monorepo README](../../README.md) for the big picture.

## What it does

Each `POST /v1/events` request goes through six stages:

1. **Auth** — verifies the `x-api-key` header against the hashed PostgreSQL
   credential registry, then derives project authority and roles from that
   record. Confidential keys use `proj_{project_id}_{secret}`; browser-safe keys
   use `client_{project_id}_{token}` and are database-limited to exactly
   `events:write` plus `config:read`. The embedded project and stored
   kind/prefix are checked for consistency but never trusted as authority.
   Invalid, expired, or revoked keys return 401; keys without `events:write`
   return 403. See [authentication](../../docs/authentication.md).
2. **Early request admission** — immediately after authentication and role
   checks, one Redis Lua operation checks and debits the global, project,
   credential, and canonical client-IP request buckets together. This happens
   before `Content-Length` inspection, body reads, privacy processing, and
   schema work, so malformed authenticated traffic consumes quota. No bucket
   is changed unless every bucket can pay the request cost. Redis keys contain
   hashes of credential IDs and IP addresses, never their raw values.
3. **Bounded body and byte admission** — `Content-Length` and the body itself
   are capped at 512 KiB. After a body passes that hard bound, but before JSON
   parsing, privacy processing, or schema work, another atomic Lua operation
   charges `max(1, ceil(bytes / 1024))` units across global, project,
   credential, and client-IP byte buckets. A fresh browser bucket accepts one
   maximum-sized request while remaining subordinate to the project bucket.
4. **Validation** — the batch must be exactly `{"events": [...]}` with 1–100
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
5. **Hierarchical event admission** — after strict validation, one atomic Lua
   operation checks and debits global, project, credential, client-IP, and
   identity buckets. Identity costs are aggregated within the batch by
   `anonymous_id` (falling back to `user_id`); the identity hash includes the
   authenticated project, so tenants never share identity quota. Only hashes
   reach Redis. Rejection of any child leaves parent and sibling balances
   unchanged. Exhausted buckets →
   `429 {"error": "rate_limited"}` with `Retry-After`, `X-RateLimit-Limit`, and
   `X-RateLimit-Remaining` headers; an unavailable quota authority fails closed
   with `503 {"error": "service_unavailable"}` before any stream publication.
6. **Bounded durable admission** — each event is enriched with `server_timestamp`, client `ip`
   (the socket peer, or one canonical `X-Forwarded-For` address from a
   configured trusted proxy), and `project_id`, then published as compact JSON
   to the Redis Stream `events:raw:{project_id}`. One Lua operation checks the
   exact outstanding depth and appends the complete batch without trimming.
   The 1,000,000-entry capacity is accepted only because the writer deletes
   entries after ClickHouse or DLQ durability; a batch that would cross it gets
   retryable `503 service_overloaded` with `Retry-After`. Pressure warnings begin
   at 750,000 outstanding entries. Success →
   `202 {"accepted": N}` only after the complete atomic operation. Any known or
   ambiguous operation failure returns `503`; clients retry the same stable
   `message_id` values and ClickHouse deduplicates them.

The three quota stages use these exact capacity/refill-per-second pairs:

| Stage (cost unit) | Global | Project | Confidential credential | Browser credential | IP | Identity |
|---|---:|---:|---:|---:|---:|---:|
| Request (1/request) | 100,000 / 10,000 | 1,000 / 100 | 1,000 / 100 | 200 / 20 | 500 / 50 | — |
| Byte (started KiB) | 100,000 / 10,000 | 1,000 / 100 | 1,000 / 100 | 512 / 20 | 1,000 / 50 | — |
| Event (1/event) | 100,000 / 10,000 | 1,000 / 100 | 1,000 / 100 | 200 / 20 | 500 / 50 | 200 / 20 |

Each stage is one check-all/debit-all Redis operation: a rejection at any level
does not debit its parent or siblings. Every quota rejection uses the same
retryable `429` headers above; every Redis quota-authority error fails closed as
`503`.

## API

| Method | Path         | Description                                              |
|--------|--------------|----------------------------------------------------------|
| POST   | `/v1/events` | Ingest a batch of events (returns `202` on acceptance)   |
| GET    | `/health`    | Liveness probe — pings Redis, `200` ok / `503` degraded  |

```bash
curl -X POST http://localhost:8080/v1/events \
  -H "x-api-key: client_demo_0123456789abcdef0123456789abcdef" \
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
| `INGESTION_TRUSTED_PROXY_CIDRS` | empty | Comma-separated canonical CIDRs allowed to supply the single-hop `X-Forwarded-For` contract |

JSON, rate-limit, and stream-admission settings are compile-time constants under
`app/validation/json_contract.py`, `app/middleware/rate_limit.py`, and
`app/streaming/redis_producer.py`.

The Redis durability contract requires AOF or equivalent managed persistence,
an explicit aggregate memory ceiling with `maxmemory-policy noeviction`, and
memory/disk alerting. Route
`event_stream_pressure`, `event_stream_overloaded`, and
`redis_memory_pressure`, and `lost_or_deleted_pending` log events to an
operational alerting system. The
checked-in Compose stack uses AOF with `appendfsync everysec`; that policy can
lose roughly the latest second during a host-level failure, so deployments
requiring power-loss durability must use `appendfsync always` or a managed
durable log.

## Running locally

```bash
make dev            # start Docker deps (Redis, ClickHouse, PostgreSQL)
make run-ingestion  # uvicorn with hot-reload → http://localhost:8080
```

Or run the supported core stack: `make dev-core`.

## Tests

```bash
make test-ingestion   # pytest
make lint-ingestion   # ruff
make test-packed-sdk-contract  # installed npm tarball → real validator

# single test file
cd services/ingestion && .venv/bin/python -m pytest tests/test_events.py -v
```
