# APDL Examples

Runnable end-to-end samples against a local APDL stack.

## 1. Start the stack

```bash
cp .env.example .env  # first time only
make dev-core         # ingestion :8080, config :8081, query :8082, gateway :8000
```

The normal bootstrap is empty. Open <http://localhost:5173/register>, create a
local account, and create a project from **Workspace settings**. From the same
page, create two deliberately different reveal-once credentials:

- A browser credential, restricted to exactly `events:write` and `config:read`.
- A confidential credential with `events:write`, `config:read`,
  `config:evaluate`, and `query:read`. Never copy it into browser code.

Save the values when they are revealed; APDL stores only their hashes. Export
the confidential key and your project ID for the commands below:

```bash
export APDL_PROJECT_ID=yourproject
export APDL_API_KEY=proj_yourproject_replacewithrevealedkey
```

## 2. Create a feature flag

The examples check a gate named `new-checkout`. In the Admin Console, open
**Flags → New flag** and create it with `control` and `treatment` variants, a
50% `user_id` rollout, client evaluation mode, and active state.

## 3. Run an example

### Python (server-side SDK)

Uses the monorepo SDK directly — no publish required:

```bash
cd sdk/python && uv venv && uv pip install -e . && cd ../..
sdk/python/.venv/bin/python examples/python/track_and_gate.py
```

The script reads `APDL_API_KEY`, tracks a few events, evaluates the
`new-checkout` gate for several users (showing the deterministic 50% split),
and prints the full gate-evaluation explanation.

### Browser (JavaScript SDK)

Build the SDK once, then serve this directory (the IIFE bundle is loaded by
relative path, and browsers block SSE/fetch from `file://` pages):

```bash
make build-sdk
cp sdk/javascript/dist/apdl.iife.js examples/browser/apdl.iife.js
python3 -m http.server 4173 --bind 127.0.0.1 --directory examples/browser
```

Open <http://127.0.0.1:4173/> and paste the reveal-once browser credential when
prompted. The page keeps it only in memory. Only the example directory is
served; repository secrets such as `.env` remain outside the document root.
The page auto-captures clicks and page views, lets you fire a manual event, and
shows the live `new-checkout` gate value—toggle the flag in the Admin Console
and watch it update over SSE.

## 4. See the data

Events land in ClickHouse via the pipeline (`make run-pipeline` if you started
services individually). Query them:

```bash
TODAY=$(date -u +%F)
curl -X POST http://localhost:8082/v1/query/events/count \
  -H "x-api-key: $APDL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d "{
    \"project_id\": \"$APDL_PROJECT_ID\",
    \"start_date\": \"$TODAY\",
    \"end_date\": \"$TODAY\",
    \"selectors\": [{\"event_name\": \"order_completed\"}]
  }"
```
