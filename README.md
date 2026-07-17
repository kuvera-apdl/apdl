<h1 align="center">APDL</h1>

<p align="center">
  <b>Autonomous Product Development Loop</b> — product analytics and
  experimentation with an optional agent operator preview.
</p>

<p align="center">
  <a href="https://github.com/kuvera-apdl/apdl/actions/workflows/ci.yml"><img src="https://github.com/kuvera-apdl/apdl/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/node-20.19%2B-339933.svg?logo=node.js&logoColor=white" alt="Node 20.19+">
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#using-the-sdks">SDKs</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#api-reference">API Reference</a> ·
  <a href="#agents-operator-preview">Agents</a> ·
  <a href="examples/">Examples</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

APDL ingests user behavior events, runs analytics queries, evaluates feature
flags and A/B experiments, and can use opt-in LLM workflows to generate
insights and propose experiment designs. In 0.3.0 those workflows do not close
the product loop: experiment designs require human approval, approval creates
an inert Config draft with a disabled flag, treatment implementation is a
separate changeset lifecycle, and an operator must separately activate any
completed treatment. Personalization, autonomous evaluation, and rollback are
disabled. The intended data flow is:

> **events in → analytics out → agent proposal → human approval → disabled
> draft + treatment work → separate operator activation → SDKs → new events**.
> The *Loop* is the product direction, not a claim of closed automation in this
> developer preview.

## Release status

APDL 0.3.0 is an OSS **developer preview**, not a production release. Its
supported deployment is a fresh, single-node, source-built Docker Compose
installation. The supported core consists of Ingestion, Config, Query, the
Redis-to-ClickHouse writer, Gateway, Admin API, and Admin Console, together
with Redis, ClickHouse, and PostgreSQL.

The 0.3.0 release publishes exactly these installable artifacts from one tested
revision:

- GitHub source archives for this repository;
- [`@apdl-oss/sdk`](https://www.npmjs.com/package/@apdl-oss/sdk) on npm; and
- [`apdl-sdk`](https://pypi.org/project/apdl-sdk/) on PyPI.

APDL does **not** publish GHCR or other container images for 0.3.0. Compose
builds the core images from the checked-out source. Agents is an opt-in,
operator-provisioned preview. Only the Codegen API/control plane is available
as a source-only, non-publishing `offline` preview; its Aider editor/worker and
`agent` dependency extra are unsupported and excluded from release audits.
Kubernetes, Terraform, multi-replica operation, upgrades, backup, and restore
are unsupported. Redis Streams is the only event bus included in the repository.
See [Support](SUPPORT.md) for the complete boundary.

## Quick Start

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, Node.js 20.19+, Python 3.12+.

```bash
git clone https://github.com/kuvera-apdl/apdl.git && cd apdl
cp .env.example .env
make dev-core            # supported core + local Admin console
make smoke               # strict event → flag evaluation → exact query result
```

This developer preview supports fresh, single-node databases only. Do not run
`make dev-core` or the initialization scripts against an existing APDL
deployment: in-place upgrades, backup, and restore are not supported or tested
in this release.

The same fresh-install proof CI runs is available as `make smoke-fresh`. It uses
an isolated Compose project and fresh volumes, initializes both databases,
provisions the canonical `demo` project with separate confidential and browser
credentials, starts only the services needed for the core proof, sends and
queries exactly one event, evaluates a flag, and removes every container and
volume when it finishes.

Agents and Codegen are opt-in Compose profiles. `make dev-core` leaves both off;
`make dev-all` starts Agents plus the offline Codegen API/control plane. It does
not install or launch the Aider editor/worker. Autonomous branch or PR
publication is not part of this OSS developer-preview release.

`scripts/dev.sh` is the master script for everything local:

| Command | What it does |
|---|---|
| `scripts/dev.sh setup` | Full local setup (same as `make setup`) |
| `scripts/dev.sh up` | Start infra deps only (Redis, ClickHouse, PostgreSQL) + migrations |
| `scripts/dev.sh up-core` | Start the supported core stack (same as `make dev-core`) |
| `scripts/dev.sh up-full` | Explicitly add optional Agents and offline Codegen (same as `make dev-all`) |
| `scripts/dev.sh status` | Container status + service health endpoints |
| `scripts/dev.sh smoke` | End-to-end smoke test against the running stack |
| `scripts/dev.sh check` | Lint + test every package in parallel |
| `scripts/dev.sh logs [svc]` | Tail Docker logs |
| `scripts/dev.sh down` / `reset` | Stop everything / also wipe data volumes |

To work on one service with hot-reload, start the deps and run it directly:

```bash
make dev            # infra deps only
make run-ingestion  # :8080   (also: run-config :8081, run-query :8082,
                    #          run-agents :8083, run-pipeline)
```

## Using the SDKs

Browser SDK keys follow `client_{project_id}_{token}` and are restricted to
event writes plus client-visible config reads. Server SDKs and trusted services
use confidential `proj_{project_id}_{secret}` keys. Services verify the full key
against a hashed PostgreSQL record and derive project/role authority from that
record; see [authentication and tenant authorization](docs/authentication.md).
Both SDKs evaluate feature flag variants **locally** with a byte-for-byte identical
FNV-1a hash — a user buckets the same way in the browser, on your server, and
in the config service. Runnable samples live in [`examples/`](examples/).

### JavaScript (browser) — [`@apdl-oss/sdk`](sdk/javascript/README.md)

For full SDK usage, see [`sdk/javascript/README.md`](sdk/javascript/README.md).

```typescript
import { APDL } from '@apdl-oss/sdk';

const apdl = APDL.init({
  endpoint: 'http://localhost:8000',
  auth: {
    clientKey: 'client_demo_0123456789abcdef0123456789abcdef',
  },
  autoCapture: true,                     // clicks, page views, forms, scroll depth, rage clicks
  privacyMode: 'standard',              // 'standard' | 'cookieless'
});

apdl.track('purchase_completed', { product_id: 'sku-123', revenue: 49.99 });
apdl.identify('user-42', { email: 'user@example.com', plan: 'pro' });

const checkoutVariant = apdl.getVariant('new-checkout-flow');

if (checkoutVariant === 'treatment') {
  // Show the treatment experience.
}
```

→ [Full JS SDK docs](sdk/javascript/README.md): configuration, privacy
controls, local UI rendering APIs, and real-time flag subscriptions. The 0.3.0
backend does not store or deliver UI configurations.

### Python (server-side) — [`apdl-sdk`](sdk/python/README.md)

```python
from apdl import APDL

with APDL.init(
    api_key="proj_demo_0123456789abcdef0123456789abcdef",
    endpoint="http://localhost:8000",
) as client:
    client.track("order_completed", {"total": 42.0}, user_id="u_123")
    client.identify("u_123", {"plan": "pro"})

    if client.get_variant("new-checkout", user_id="u_123") == "treatment":
        ...  # treatment experience
```

→ [Full Python SDK docs](sdk/python/README.md): batching, variant-result
explanations, configuration.

## Architecture

Written walkthrough of the components and the three data flows (events, flags,
the agent loop): [docs/architecture.md](docs/architecture.md).

| Container | Port | Release status | Description | Docs |
|---|---|---|---|---|
| `ingestion` | 8080 | Core | Event ingestion → Redis Streams | [README](services/ingestion/README.md) |
| `config` | 8081 | Core | Feature flags & experiments, SSE | [README](services/config/README.md) |
| `query` | 8082 | Core | Analytics queries on ClickHouse | [README](services/query/README.md) |
| `agents` | 8083 | Operator preview | Opt-in LLM workflows; self-registered projects are read-only | [README](services/agents/README.md) |
| `codegen` | 8084 (internal) | Offline preview | Source-only; publication is disabled | [README](services/codegen/README.md) |
| `admin-api` | 8085 (internal) | Core | Human sessions, tenant authorization, secure service proxy | [README](services/admin-api/README.md) |
| `admin` | 5173 | Core | Browser admin console | [README](services/admin/README.md) |
| `clickhouse-writer` | — | Core | Redis Streams → ClickHouse pipeline | [README](pipeline/README.md) |
| `gateway` | 8000 | Local development | nginx routing for the source-built stack; not production ingress | [Compose](infra/docker/docker-compose.yml) |
| `redis` | 6379 | Core dependency | Event streams + cache | — |
| `clickhouse` | 8123 / 9000 | Core dependency | Analytics store (HTTP / native) | — |
| `postgres` | 5432 | Core dependency | Config store + pgvector | — |

<details>
<summary><b>Tech stack by layer</b></summary>

| Layer | Technology |
|---|---|
| Browser SDK | TypeScript, Rollup, Vitest |
| Python SDK | Python 3.12, httpx, Pydantic |
| Ingestion Service | Python 3.12, FastAPI, Redis Streams, Pydantic |
| Config Service | Python 3.12, FastAPI, asyncpg, Redis, SSE, Pydantic |
| Query Service | Python 3.12, FastAPI, ClickHouse, SciPy, NumPy |
| Agents Service | Python 3.12, FastAPI, OpenAI/Anthropic/Google GenAI SDKs, pgvector |
| Event Pipeline | Redis Streams writer |
| Analytics Store | ClickHouse (MergeTree, materialized views) |
| Config Store | PostgreSQL 16 + pgvector |
| Infrastructure | Docker Compose, GitHub Actions |

</details>

<details>
<summary><b>Project layout</b></summary>

```
apdl/
├── sdk/javascript/          # @apdl-oss/sdk — TypeScript client SDK
│   └── src/                 # core, capture, flags, sse, ui, privacy
├── sdk/python/              # apdl-sdk — server-side Python client SDK
│   └── apdl/                # client, batching event queue, flags
│
├── services/
│   ├── ingestion/           # Event ingestion + validation → Redis Streams
│   ├── config/              # Flags & experiments CRUD, Redis cache, SSE
│   ├── query/               # Funnels, cohorts, retention, experiment stats
│   ├── agents/              # Agent graphs, LLM router, memory, tools, safety
│   └── codegen/             # Offline source preview; publication disabled
│
├── pipeline/
│   ├── redis/               # Redis Streams → ClickHouse event writer
│   └── clickhouse/          # Schemas + migrations
│
├── examples/                # Runnable browser + Python end-to-end samples
├── fixtures/                # Cross-SDK golden values (gate bucketing parity)
├── scripts/                 # dev.sh (master setup/run/test), check.sh, fmt.sh
├── infra/docker/            # Docker Compose (deps + full stack)
├── .github/workflows/       # CI gates and Release (GitHub + npm + PyPI)
└── Makefile                 # Build, test, lint, migrate, dev orchestration
```

</details>

## Development

| Task | Command |
|---|---|
| Lint + test everything in parallel (CI mirror) | `make check` |
| All tests / all linters | `make test` / `make lint` |
| Auto-format all packages | `make fmt` |
| One package | `make test-<pkg>` / `make lint-<pkg>` — `sdk`, `sdk-python`, `ingestion`, `config`, `query`, `agents` |
| Build the JS SDK | `make build` |
| ClickHouse migrations | `make migrate-clickhouse` |
| Health overview / smoke test | `make status` / `make smoke` |
| Stop containers | `make dev-down` |

Run a single test while iterating:

```bash
cd sdk/javascript && npm test -- core/client.test.ts
cd services/query && .venv/bin/python -m pytest tests/test_funnels.py -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions, the PR workflow, and
the cross-SDK parity rules.

## API Reference

Condensed reference — each service README has full request/response details
and `curl` examples.

### Ingestion (`:8080`) — [full docs](services/ingestion/README.md)

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/events` | Ingest strict event batch (1–100 events, returns `202`) |
| `GET` | `/health`, `/ready` | Process liveness / dependency readiness |

### Config (`:8081`) — [full docs](services/config/README.md)

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/flags` | Flags for a project (SDK bootstrap, Redis-cached) |
| `GET` | `/v1/stream` | SSE stream for real-time flag updates |
| `POST` | `/v1/evaluate` | Server-side gate evaluation (project-scoped API key) |
| `GET/POST` | `/v1/admin/flags` | List / create flags |
| `PUT/DELETE` | `/v1/admin/flags/:key` | Update / archive flag |
| `GET/POST` | `/v1/admin/experiments` | List / create experiments |
| `PUT/DELETE` | `/v1/admin/experiments/:key` | Update / delete experiment |
| `GET` | `/v1/experiments/:key/analysis` | Immutable metadata for authoritative analysis |
| `GET` | `/health` | Health check |

The admin API also covers stale-flag reports, system disable, cleanup, and
per-flag audit history — see the [config service README](services/config/README.md).

### Query (`:8082`) — [full docs](services/query/README.md)

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/query/events/count` | Count one or more event selectors |
| `POST` | `/v1/query/events/timeseries` | Time-bucketed counts for one selector |
| `POST` | `/v1/query/events/breakdown` | Typed scalar property breakdown for one selector |
| `POST` | `/v1/query/funnel` | N-step funnel analysis (windowFunnel) |
| `POST` | `/v1/query/cohort` | Cohort analysis |
| `POST` | `/v1/query/retention` | Window-relative retention (`first_match_in_window`) |
| `GET` | `/v1/query/experiment/:key` | Config-owned conversion experiment analysis |
| `POST` | `/v1/query/guardrails/evaluate` | Evaluate flag guardrails |
| `GET` | `/health`, `/ready` | Health / readiness |

Query endpoints use a strict `EventSelector` shape — filters within one
selector are `AND`-combined; operators are `eq`, `neq`, `in`, `not_in`,
`exists`, `not_exists`, `contains`, `gt`, `gte`, `lt`, `lte`:

```json
{
  "event_name": "$click",
  "filters": [
    {"property": "href", "operator": "eq", "value": "/pricing"}
  ]
}
```

<details>
<summary><b>More query examples</b> — count, timeseries, breakdown, funnel, retention, cohort</summary>

Count clicks to a specific URL:

```json
{
  "project_id": "apiasport",
  "start_date": "2025-01-01",
  "end_date": "2025-01-31",
  "selectors": [
    {
      "event_name": "$click",
      "filters": [{"property": "href", "operator": "eq", "value": "/catalog"}]
    }
  ]
}
```

Timeseries for one CTA:

```json
{
  "project_id": "apiasport",
  "start_date": "2025-01-01",
  "end_date": "2025-01-31",
  "interval": "1 DAY",
  "selector": {
    "event_name": "$click",
    "filters": [{"property": "text", "operator": "eq", "value": "Start free trial"}]
  }
}
```

Breakdown of filtered clicks:

```json
{
  "project_id": "apiasport",
  "start_date": "2025-01-01",
  "end_date": "2025-01-31",
  "selector": {
    "event_name": "$click",
    "filters": [{"property": "page.path", "operator": "eq", "value": "/pricing"}]
  },
  "property": "href",
  "limit": 20
}
```

Breakdown rows include `property_type` (`string`, `integer`, `float`, or
`boolean`) plus a canonical string `property_value`. Type is part of the bucket,
so values such as the integer `1` and string `"1"` remain distinct. Missing,
null, array, and object properties are excluded.

Page/click-path funnel:

```json
{
  "project_id": "apiasport",
  "start_date": "2025-01-01",
  "end_date": "2025-01-31",
  "steps": [
    {
      "event_name": "$pageview",
      "filters": [{"property": "path", "operator": "eq", "value": "/catalog"}]
    },
    {
      "event_name": "$click",
      "filters": [{"property": "href", "operator": "eq", "value": "/checkout"}]
    }
  ],
  "window_days": 7
}
```

Property-filtered retention:

Retention is intentionally window-relative. Actors enter on their first
matching cohort event in the selected dates, so existing actors may re-enter;
this is not lifetime acquisition retention. The required `cohort_mode` makes
that behavior explicit in every request and response.

```json
{
  "project_id": "apiasport",
  "start_date": "2025-01-01",
  "end_date": "2025-01-31",
  "cohort_selector": {
    "event_name": "$pageview",
    "filters": [{"property": "path", "operator": "eq", "value": "/pricing"}]
  },
  "return_selector": {
    "event_name": "$click",
    "filters": [{"property": "href", "operator": "eq", "value": "/signup"}]
  },
  "cohort_mode": "first_match_in_window",
  "period": "day"
}
```

Filtered cohort comparison:

```json
{
  "project_id": "apiasport",
  "start_date": "2025-01-01",
  "end_date": "2025-01-31",
  "cohort_property": "plan",
  "metric_selector": {
    "event_name": "$click",
    "filters": [{"property": "href", "operator": "eq", "value": "/checkout"}]
  }
}
```

</details>

### Agents (`:8083`) — [full docs](services/agents/README.md)

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/agents/trigger` | Start an agent run |
| `GET` | `/v1/agents/:run_id/status` | Check run status |
| `POST` | `/v1/agents/:run_id/cancel` | Durably cancel an active run and fence further work |
| `POST` | `/v1/agents/:run_id/approve` | Queue strict per-item approval decisions (`202`) |
| `GET` | `/v1/agents/:run_id/approvals/:command_id` | Approval command and effect status |
| `GET` | `/health` | Process liveness |
| `GET` | `/ready` | Core Agents runtime and PostgreSQL readiness |
| `GET` | `/ready/capabilities` | Non-blocking LLM, Query, Config, and Codegen capability report |

## Agents operator preview

Agents execution is an operator-authorized capability in the OSS developer
preview. Projects created through public registration keep `agents:read` for
definitions, history, results, and audit records by default. Execution requires
either operator-provisioned project provenance or an explicit immutable
self-registration override with operator actor and reason evidence. Agents,
Codegen, role storage, and execution-bearing database tables all enforce that
canonical authorization.

The agents service runs operator-triggered analysis and proposal workflows
powered by policy-governed LLM reasoning. The safe default permits only the
exact local `gemma4` model at `http://localhost:11434/v1`; external providers, data classifications,
residency, prices, spend ceilings, and cross-vendor retry require explicit
per-project policy:

- **Behavior Analysis** — queries ClickHouse to identify trends, anomalies, and conversion patterns
- **Experiment Design** — proposes A/B tests for human approval; an approval
  creates only a disabled experiment draft and may request separate treatment work
- **Personalization** — disabled in 0.3.0; no canonical Config storage or SDK
  delivery path exists yet
- **Experiment Evaluation and Feature Proposals** — disabled in 0.3.0; current
  statistical snapshots do not establish deployment readiness

For eligible operator projects, proposed experiment drafts receive static
validation and are recorded in the audit trail. Experiment design is hard-gated
for human approval at every configured autonomy level. Approval does not deploy
treatment code, enable its backing flag, or start an experiment; those are
separate implementation, review, and activation steps outside the Agents run.

The validator checks the proposed action's shape and bounded blast radius. It
does not monitor live guardrail metrics or prove treatment/deployment readiness.
Automatic experiment evaluation, activation, stopping, shipping, and rollback
are unavailable in this release.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup,
conventions, and the PR workflow. Participation is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md) and [Governance](GOVERNANCE.md).
[Support](SUPPORT.md) defines the supported release boundary, and
[SECURITY.md](SECURITY.md) explains how to report vulnerabilities privately.
Notable changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
