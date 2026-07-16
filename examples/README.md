# APDL Examples

Runnable end-to-end samples against a local APDL stack.

## 1. Start the stack

```bash
cp .env.example .env  # first time only
make dev-core         # ingestion :8080, config :8081, query :8082, gateway :8000
```

The local bootstrap provisions two deliberately different credentials for the
`demo` project:

- Browser example: `client_demo_0123456789abcdef0123456789abcdef`, restricted
  to exactly `events:write` and `config:read`.
- Server/admin/query examples: `proj_demo_0123456789abcdef0123456789abcdef`, a
  confidential core credential without Agents execution roles. Never copy this
  key into browser code.

Ingestion derives the project from the verified credential record, so no extra
registration is needed locally.

## 2. Create a feature flag

The examples check a gate named `new-checkout`. Create it with a 50% rollout:

```bash
curl -X POST http://localhost:8081/v1/admin/flags \
  -H 'x-api-key: proj_demo_0123456789abcdef0123456789abcdef' \
  -H 'Content-Type: application/json' \
  -d '{
    "key": "new-checkout",
    "name": "New checkout flow",
    "state": "active",
    "enabled": true,
    "owners": ["you@example.com"],
    "default_variant": "control",
    "variants": [
      {"key": "control", "weight": 1},
      {"key": "treatment", "weight": 1}
    ],
    "rules": [],
    "fallthrough": {
      "rollout": {"percentage": 50, "bucket_by": "user_id"}
    },
    "evaluation_mode": "client",
    "auto_disable": false,
    "guardrails": []
  }'
```

## 3. Run an example

### Python (server-side SDK)

Uses the monorepo SDK directly — no publish required:

```bash
cd sdk/python && uv venv && uv pip install -e . && cd ../..
sdk/python/.venv/bin/python examples/python/track_and_gate.py
```

It tracks a few events, evaluates the `new-checkout` gate for several users
(showing the deterministic 50% split), and prints the full gate-evaluation
explanation.

### Browser (JavaScript SDK)

Build the SDK once, then serve this directory (the IIFE bundle is loaded by
relative path, and browsers block SSE/fetch from `file://` pages):

```bash
make build-sdk
cp sdk/javascript/dist/apdl.iife.js examples/browser/apdl.iife.js
python3 -m http.server 4173 --bind 127.0.0.1 --directory examples/browser
```

Open <http://127.0.0.1:4173/>. Only the example directory is served; repository
secrets such as `.env` remain outside the document root. The page auto-captures
clicks and page views, lets you fire a manual event, and shows the live
`new-checkout` gate value — toggle the flag via the admin API and watch it update
over SSE.

## 4. See the data

Events land in ClickHouse via the pipeline (`make run-pipeline` if you started
services individually). Query them:

```bash
TODAY=$(date -u +%F)
curl -X POST http://localhost:8082/v1/query/events/count \
  -H 'x-api-key: proj_demo_0123456789abcdef0123456789abcdef' \
  -H 'Content-Type: application/json' \
  -d "{
    \"project_id\": \"demo\",
    \"start_date\": \"$TODAY\",
    \"end_date\": \"$TODAY\",
    \"selectors\": [{\"event_name\": \"order_completed\"}]
  }"
```
