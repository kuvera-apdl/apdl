# APDL

**Autonomous Product Development Loop** — a self-optimizing product analytics and experimentation platform. APDL ingests user behavior events, runs analytics queries, evaluates feature flags and A/B experiments, and uses LLM-powered agents to autonomously generate insights, design experiments, and personalize user experiences.

## Architecture

<p align="center">
  <img src="docs/architecture.svg" alt="APDL Architecture" width="900"/>
</p>

For a written walkthrough of the components and the three data flows (events,
flags, and the agent feedback loop), see [docs/architecture.md](docs/architecture.md).

## Project Structure

```
apdl/
├── sdk/javascript/          # @apdl/sdk — TypeScript client SDK
│   ├── src/
│   │   ├── core/            # Config, transport, event queue, storage
│   │   ├── capture/         # Auto-capture (clicks, pages, forms) + manual tracking
│   │   ├── flags/           # Client-side feature gate evaluation (FNV-1a bucketing)
│   │   ├── sse/             # Real-time flag update stream
│   │   ├── ui/              # Server-driven UI components (banner, modal, toast, etc.)
│   │   └── privacy/         # Consent management, PII scrubbing, cookieless mode
│   └── package.json
│
├── sdk/python/              # apdl-sdk — server-side Python client SDK
│   ├── apdl/                # Client, batching event queue, transport
│   │   └── flags/           # Local feature gate evaluation (FNV-1a bucketing)
│   ├── tests/               # pytest unit tests
│   └── pyproject.toml
│
├── services/
│   ├── ingestion/           # Python (FastAPI) — event ingestion + validation
│   │   ├── app/             # HTTP handlers, schema validation, Redis Streams producer
│   │   └── tests/           # pytest unit tests
│   │
│   ├── config/              # Python (FastAPI) — feature flags & experiment configuration
│   │   ├── app/             # Flags CRUD, SSE broadcaster, PostgreSQL store, Redis cache
│   │   └── tests/           # pytest unit tests
│   │
│   ├── query/               # Python (FastAPI) — analytics query engine
│   │   └── app/
│   │       ├── clickhouse/  # ClickHouse client + query builders
│   │       ├── routers/     # Funnels, cohorts, retention, experiments
│   │       └── models/      # Pydantic schemas, statistical analysis (freq/bayesian/sequential)
│   │
│   └── agents/              # Python (FastAPI) — autonomous AI agents
│       └── app/
│           ├── graphs/      # Agent workflows (supervisor, behavior, experiments, etc.)
│           ├── llm/         # LLM router + prompt templates
│           ├── memory/      # pgvector-backed agent memory
│           ├── tools/       # Agent tools (ClickHouse queries, flag/experiment CRUD, UI config)
│           └── safety/      # Action validation, rollback, audit logging
│
├── pipeline/
│   ├── redis/               # Redis Streams → ClickHouse event writer
│   ├── etl/                 # Custom-events ETL framework (canonical envelope → v2 tables)
│   ├── kafka/               # Kafka topic definitions (Phase 3+ migration)
│   └── clickhouse/          # Schemas + migrations (events, sessions, experiments, materialized views)
│
├── examples/                # Runnable browser + Python end-to-end samples
├── fixtures/                # Cross-SDK golden values (gate bucketing parity)
├── scripts/                 # dev.sh (master setup/run/test), check.sh, fmt.sh, migrations
│
├── infra/
│   └── docker/              # Docker Compose (deps + full stack)
│
├── .github/workflows/       # CI (lint + test) and Release (npm + PyPI publish + Docker images)
└── Makefile                 # Build, test, lint, migrate, and dev orchestration
```

Each service and the pipeline have their own README:
[ingestion](services/ingestion/README.md) ·
[config](services/config/README.md) ·
[query](services/query/README.md) ·
[agents](services/agents/README.md) ·
[pipeline](pipeline/README.md)

## Tech Stack

| Layer | Technology |
|---|---|
| Client SDK | TypeScript, Rollup, Vitest |
| Ingestion Service | Python 3.12, FastAPI, Redis Streams, Pydantic |
| Config Service | Python 3.12, FastAPI, asyncpg, Redis, SSE, Pydantic |
| Query Service | Python 3.12, FastAPI, ClickHouse, SciPy, NumPy |
| Agents Service | Python 3.12, FastAPI, OpenAI SDK, Anthropic SDK, Google GenAI SDK, pgvector, asyncpg |
| Event Pipeline | Redis Streams (Phase 1–2), Kafka (Phase 3+) |
| Analytics Store | ClickHouse (MergeTree, materialized views) |
| Config Store | PostgreSQL 16 + pgvector |
| Cache | Redis 7 |
| Infrastructure | Docker, Docker Compose |
| CI/CD | GitHub Actions |

## Getting Started

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Docker & Docker Compose
- Node.js 20+
- Python 3.12+

### Quick Start

```bash
make setup
```

This single command will:
1. Create isolated Python virtualenvs (via `uv`) for every service, SDK, and pipeline package
2. Install all Python and Node.js dependencies
3. Start infrastructure containers (Redis, ClickHouse, PostgreSQL)
4. Run ClickHouse migrations
5. Copy `.env.example` → `.env` (edit to add API keys)

Everything for local development is also available through one script:

```bash
scripts/dev.sh setup     # same as make setup
scripts/dev.sh up-full   # full stack in Docker (detached) + migrations
scripts/dev.sh status    # container status + service health endpoints
scripts/dev.sh smoke     # end-to-end smoke test: ingest event → create flag → query
scripts/dev.sh check     # lint + test every package in parallel
scripts/dev.sh down      # stop everything   (reset also wipes volumes)
```

### Running Services

After setup, start individual services locally with hot-reload:

```bash
make run-ingestion  # Ingestion Service → http://localhost:8080
make run-config     # Config Service    → http://localhost:8081
make run-query      # Query Service     → http://localhost:8082
make run-agents     # Agents Service    → http://localhost:8083
make run-pipeline   # ClickHouse Writer (Redis Streams consumer)
```

Or start everything via Docker:

```bash
make dev-all
```

### Build

```bash
make build          # Build SDK
make build-sdk      # SDK only
```

### Test & Lint

```bash
make test           # Run all tests
make lint           # Run all linters
make check          # Lint + test every package in parallel (local CI mirror)
make fmt            # Auto-format all packages
make smoke          # End-to-end smoke test against the running stack
make status         # Container + service health overview

make test-sdk       # SDK unit tests
make test-ingestion # Ingestion service tests (pytest)
make test-config    # Config service tests (pytest)
make test-query     # Query service tests
make test-agents    # Agents service tests

make lint-sdk       # TypeScript type check
make lint-ingestion # ruff check on ingestion service
make lint-config    # ruff check on config service
make lint-query     # ruff check on query service
make lint-agents    # ruff check on agents service
```

### Database Migrations

```bash
make migrate-clickhouse
```

### Teardown

```bash
make dev-down       # Stop all Docker containers
```

## SDK Usage

API keys follow `proj_{project_id}_{secret}` (secret: 16+ alphanumeric characters).
Runnable end-to-end samples live in [`examples/`](examples/).

### JavaScript (browser) — [`@apdl/sdk`](sdk/javascript/)

```typescript
import { APDL } from '@apdl/sdk';

const apdl = APDL.init({
  apiKey: 'proj_demo_0123456789abcdef',
  autoCapture: true,                     // clicks, page views, forms, scroll depth, rage clicks
  privacyMode: 'standard',               // 'standard' | 'cookieless' | 'strict'
});

// Manual event tracking
apdl.track('purchase_completed', {
  product_id: 'sku-123',
  revenue: 49.99,
});

// Feature gates (client-side evaluation)
apdl.identify('user-42', {
  email: 'user@example.com',
  plan: 'pro',
});

if (apdl.checkGate('new-checkout-flow')) {
  // Show the gated experience.
}
```

See the [JavaScript SDK README](sdk/javascript/README.md) for configuration,
privacy controls, server-driven UI, and real-time flag subscriptions.

### Python (server-side) — [`apdl-sdk`](sdk/python/)

```python
from apdl import APDL

with APDL.init(api_key="proj_demo_0123456789abcdef") as client:
    client.track("order_completed", {"total": 42.0}, user_id="u_123")
    client.identify("u_123", {"plan": "pro"})

    if client.check_gate("new-checkout", user_id="u_123"):
        ...  # gated experience
```

See the [Python SDK README](sdk/python/README.md) for batching, gate-result
explanations, and configuration. Both SDKs and the Config Service share a
byte-for-byte identical FNV-1a bucketing hash, so a user buckets identically
everywhere a gate is evaluated.

## Agents

The agents service runs autonomous analysis workflows powered by LLM reasoning (via OpenAI, Anthropic, Google, and local model SDKs).

**Agent graphs:**
- **Behavior Analysis** — queries ClickHouse to identify trends, anomalies, and conversion patterns
- **Experiment Design** — proposes A/B tests based on behavioral insights, creates flags and experiments
- **Personalization** — configures server-driven UI components per user segment
- **Feature Proposals** — generates product feature suggestions backed by data

**Autonomy levels:**
| Level | Behavior |
|---|---|
| L1 | Suggest only — surfaces insights for human review |
| L2 | Auto-safe — auto-deploys low-risk changes (e.g., <5% rollout) |
| L3 | Auto + approve risky — auto-deploys safe changes, queues risky ones for approval |
| L4 | Full auto — executes all actions with audit logging |

All agent actions go through a safety validator and are recorded in the audit log. Rollback is supported for any agent-initiated change.

## API Endpoints

### Ingestion Service (`:8080`) — [full docs](services/ingestion/README.md)
| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/events` | Ingest event batch (1–500 events, returns `202`) |
| `GET` | `/health` | Health check |

### Config Service (`:8081`) — [full docs](services/config/README.md)
| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/flags` | Get flags for a project (SDK polling, Redis-cached) |
| `GET` | `/v1/stream` | SSE stream for real-time flag updates |
| `POST` | `/v1/evaluate` | Server-side gate evaluation (internal token) |
| `GET/POST` | `/v1/admin/flags` | List / create flags |
| `PUT/DELETE` | `/v1/admin/flags/:key` | Update / archive flag |
| `GET/POST` | `/v1/admin/experiments` | List / create experiments |
| `PUT/DELETE` | `/v1/admin/experiments/:key` | Update / delete experiment |
| `GET` | `/health` | Health check |

The admin API also covers stale-flag reports, system disable, cleanup, and
per-flag audit history — see the [config service README](services/config/README.md).

### Query Service (`:8082`) — [full docs](services/query/README.md)
| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/query/events/count` | Count one or more event selectors |
| `POST` | `/v1/query/events/timeseries` | Time-bucketed counts for one event selector |
| `POST` | `/v1/query/events/breakdown` | Property breakdown for one event selector |
| `POST` | `/v1/query/funnel` | N-step funnel analysis (windowFunnel) |
| `POST` | `/v1/query/cohort` | Cohort analysis |
| `POST` | `/v1/query/retention` | Retention curves |
| `GET` | `/v1/query/experiment/:id` | Experiment results with statistical tests |
| `POST` | `/v1/query/guardrails/evaluate` | Evaluate flag guardrails (error rate / health) |
| `GET` | `/health`, `/ready` | Health / readiness |

Query endpoints use a strict `EventSelector` shape:

```json
{
  "event_name": "$click",
  "filters": [
    {"property": "href", "operator": "eq", "value": "/pricing"}
  ]
}
```

Filters inside one selector are combined with `AND`. Supported operators are
`eq`, `neq`, `in`, `not_in`, `exists`, `not_exists`, `contains`, `gt`, `gte`,
`lt`, and `lte`.

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

### Agents Service (`:8083`) — [full docs](services/agents/README.md)
| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/agents/trigger` | Start an agent run |
| `GET` | `/v1/agents/:run_id/status` | Check run status |
| `POST` | `/v1/agents/:run_id/approve` | Approve a run's pending actions |
| `GET` | `/health`, `/ready` | Health / readiness |

## Infrastructure

All services and dependencies run via Docker Compose:

```bash
# Dependencies only (Redis, ClickHouse, PostgreSQL)
make dev

# Full stack (deps + all application services)
make dev-all
```

Both commands initialize the local ClickHouse schema before ClickHouse-dependent
services process requests or events.

| Container | Port | Description |
|---|---|---|
| `ingestion` | 8080 | Event ingestion (Python/FastAPI) |
| `config` | 8081 | Feature flags & experiments (Python/FastAPI) |
| `query` | 8082 | Analytics queries (Python/FastAPI) |
| `agents` | 8083 | Autonomous AI agents (Python/FastAPI) |
| `clickhouse-writer` | -- | Redis Streams to ClickHouse pipeline |
| `redis` | 6379 | Event streams + cache |
| `clickhouse` | 8123 / 9000 | Analytics store (HTTP / native) |
| `postgres` | 5432 | Config store + pgvector (pgvector/pgvector:pg16) |

See `infra/docker/` for the full configuration.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup,
conventions, and the PR workflow, and [SECURITY.md](SECURITY.md) for how to
report vulnerabilities. Notable changes are tracked in
[CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
