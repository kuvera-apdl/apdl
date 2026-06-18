# AGENTS.md

This file provides guidance to Codex, ChatGPT, and other AGENTS-aware coding
agents when working with code in this repository.

## Shared Agent Workflows

- Structured PR workflow: follow `docs/agent-workflows/structured-pr.md` when the
  user asks to create a PR, open a pull request, raise a PR, make commits for a
  PR branch, or ship the current branch or changes.

## What is APDL?

Autonomous Product Development Loop -- a self-optimizing product analytics and
experimentation platform. It ingests user behavior events, runs analytics
queries, evaluates feature flags and A/B experiments, and uses LLM-powered
agents to autonomously generate insights, design experiments, and personalize
user experiences.

## Kanban Task Writing

When asked to write a task about a topic discussed in the conversation or
explained in the user's message, write it as a kanban task with:

- **Name:** A concise task title.
- **Description:** Three sections:
  1. Explain the intended outcome and scope.
  2. **Why this matters:** Explain the value, impact, or reason the task should
     be done.
  3. **Acceptance Criteria:** Define the conditions that must be met for the task
     to be considered complete.

## Pull Request Description Writing

When asked to write a pull request description, use this format:

```markdown
## Summary

<!-- 1-3 bullets describing the change and the motivation. -->
-

## Test plan

<!-- How you verified this works. Tick boxes as you complete them. -->
- [ ]

## Notes

<!-- Optional: migrations, follow-ups, rollback steps, screenshots, or anything reviewers should know. Delete this section if not needed. -->
```

## Build & Development Commands

```bash
make setup              # Full local dev setup (uv venvs, npm install, Docker deps, migrations, .env)
make build              # Build SDK
make test               # Run all tests
make lint               # Run all linters
make dev                # Start Docker deps only (Redis, ClickHouse, PostgreSQL)
make dev-all            # Start full stack via Docker Compose
make dev-down           # Stop all containers
make migrate-clickhouse # Apply ClickHouse SQL migrations
```

### Running individual services with hot reload

```bash
make run-ingestion  # Ingestion Service on localhost:8080
make run-config     # Config Service on localhost:8081
make run-query      # Query Service on localhost:8082
make run-agents     # Agents Service on localhost:8083
make run-codegen    # Codegen Service on localhost:8084
make run-pipeline   # ClickHouse Writer, Redis Streams consumer
make run-admin      # Admin Console (Vite dev server) on localhost:5173
```

### Per-service test/lint

| Service | Test | Lint |
|---------|------|------|
| SDK (JS) | `make test-sdk` | `make lint-sdk` |
| SDK (Python) | `make test-sdk-python` | `make lint-sdk-python` |
| Ingestion | `make test-ingestion` | `make lint-ingestion` |
| Config | `make test-config` | `make lint-config` |
| Query | `make test-query` | `make lint-query` |
| Agents | `make test-agents` | `make lint-agents` |
| Codegen | `make test-codegen` | `make lint-codegen` |
| Admin Console | `make test-admin` | `make lint-admin` |

### Running a single test

```bash
# SDK - JavaScript (Vitest)
cd sdk/javascript && npm test -- core/client.test.ts

# SDK - Python (pytest)
cd sdk/python && .venv/bin/python -m pytest tests/test_evaluator.py -v

# Python services (pytest)
cd services/ingestion && .venv/bin/python -m pytest tests/test_events.py -v
cd services/config && .venv/bin/python -m pytest tests/test_evaluator.py -v
cd services/query && .venv/bin/python -m pytest tests/test_funnels.py -v
cd services/agents && .venv/bin/python -m pytest tests/test_supervisor.py::test_specific -v
```

## Architecture Overview

The system is a monorepo with five Python services, a data pipeline, and two
client SDKs (a browser TypeScript SDK and a server-side Python SDK):

```text
SDK (TypeScript) --POST /v1/events--> Ingestion (Python/FastAPI :8080) --> Redis Streams
                 <--SSE /v1/stream--- Config (Python/FastAPI :8081) <--> PostgreSQL + Redis Cache
                                           ^
Redis Streams --> ClickHouse Writer -------+--> ClickHouse
                                                |
                                                v
                                      Query Service (Python/FastAPI :8082)
                                                |
                                                v
                                      Agents Service (Python/FastAPI :8083)
                                      <--> PostgreSQL (pgvector) for memory
                                                |
                                                v
                                      Codegen Service (Python/FastAPI :8084)
                                      --> GitHub App --> customer repos (autonomous PRs)
```

### Data Flow

1. **Event ingestion:** SDK to Ingestion Service for auth, rate-limit, schema
   validation, then Redis Streams at `events:raw:{project_id}`.
2. **Event pipeline:** ClickHouse Writer consumes Redis Streams in batches of
   1000 events or a 5 second flush, then writes to ClickHouse.
3. **Flag distribution:** Config Service stores flags/experiments in PostgreSQL,
   caches in Redis, and pushes updates via SSE to SDK.
4. **Flag evaluation:** SDKs evaluate flags locally using FNV-1a bucketing, with
   no server round-trip for evaluation. The JS SDK, the Python SDK, and the
   Config Service share a byte-for-byte identical hash, so a user buckets
   identically everywhere.
5. **Analytics:** Query Service queries ClickHouse for funnels, cohorts,
   retention, and experiment stats.
6. **Autonomous agents:** A lightweight graph runner orchestrates LLM-driven
   workflows for behavior analysis, experiment design, personalization, and
   feature proposals. Actions pass through safety validation with audit logging
   and rollback support.
7. **Autonomous code:** The Codegen Service turns approved feature proposals into
   tested-green pull requests on connected customer repos via Claude Managed
   Agents (self-hosted sandbox); merge is gated on green CI plus autonomy level.

### Tech Stack by Service

- **SDK - JS** (`sdk/javascript/`): TypeScript, Rollup, Vitest.
- **SDK - Python** (`sdk/python/`): Python 3.12, server-side client, httpx,
  Pydantic, uv, pytest, ruff.
- **Ingestion** (`services/ingestion/`): Python 3.12, FastAPI, redis, Pydantic,
  uv, pytest, ruff.
- **Config** (`services/config/`): Python 3.12, FastAPI, asyncpg, redis,
  sse-starlette, Pydantic, uv, pytest, ruff.
- **Query** (`services/query/`): Python 3.12, FastAPI, clickhouse-driver/asynch,
  SciPy, NumPy, uv, pytest-asyncio, ruff.
- **Agents** (`services/agents/`): Python 3.12, FastAPI, openai, anthropic,
  google-genai, asyncpg, pgvector, uv, pytest-asyncio, ruff.
- **Codegen** (`services/codegen/`): Python 3.12, FastAPI, asyncpg, httpx, pyjwt
  (GitHub App), anthropic (Managed Agents), uv, pytest-asyncio, ruff.
- **Pipeline** (`pipeline/redis/`): Python 3.12, redis async client,
  clickhouse-driver.

### Key Ports

| Service | Port |
|---------|------|
| Ingestion | 8080 |
| Config | 8081 |
| Query | 8082 |
| Agents | 8083 |
| Codegen | 8084 |
| Redis | 6379 |
| ClickHouse HTTP / Native | 8123 / 9000 |
| PostgreSQL | 5432 |

## Tooling & Conventions

- **Python package management:** `uv` (not pip directly). Each Python service has
  its own `.venv/` directory.
- **Python linting:** `ruff check app/` (default config, no pyproject.toml
  overrides).
- **JS SDK linting:** `tsc --noEmit` (strict mode, no unused locals/params).
- **JS SDK test pattern:** `__tests__/**/*.test.ts`.
- **Python test pattern:** `tests/` directory in each service and in `sdk/python/`.
- **CI runs on push/PR to main:** JS SDK tests + build, Python SDK lint + tests
  (ruff + pytest), and Python linting (ruff) for all five services.
- **Releases:** git tags matching `v*` trigger npm publish + Docker image builds
  to GHCR.

## Strict Schema Rule

When planning or implementing new functionality, enforce one strict canonical
schema for each contract. Avoid aliases, duplicate field names, permissive
fallbacks, and backwards-compatibility shims unless the user explicitly requests
a migration strategy that requires them.

- Do not support multiple names for the same field, such as `default_variant` and
  `default_value`, or `targeting_rules` and `rules`.
- Do not add compatibility aliases for new APIs, SDK methods, event names, or
  database columns.
- Prefer a clear breaking change with an explicit migration plan over ambiguous
  dual-schema support.
- Implementation plans must call out the canonical schema and remove or migrate
  competing shapes before adding dependent features.
- Tests should assert the strict schema and reject unknown or ambiguous fields
  where practical.

## Environment Variables

Infrastructure defaults for local dev, set via `make setup` from `.env.example`:

```text
REDIS_URL=redis://localhost:6379
POSTGRES_URL=postgresql://apdl:apdl_dev@localhost:5432/apdl
CLICKHOUSE_URL=http://localhost:8123 (HTTP) / clickhouse://apdl:apdl_dev@localhost:9000/apdl (native)
```

Agents service requires at least one of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GOOGLE_API_KEY`, or `LOCAL_LLM_URL` for LLM access.
