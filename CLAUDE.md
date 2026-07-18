# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Shared Agent Workflows

- Structured PR workflow: follow `docs/agent-workflows/structured-pr.md` when the user asks to create a PR, open a pull request, raise a PR, make commits for a PR branch, or ship the current branch or changes.
- This is the canonical version of that workflow. The standalone service repos split out of this monorepo (`kuvera-edi`, `apdl-database-service`, `apdl-experiments-service`, `apdl-agent-service`) each carry their own adapted copy at the same path (`uv`/`ruff`/`mypy`/`pytest` directly instead of `make lint-<area>`, since they're single-package repos) plus the same `.claude/skills/structured-pr/` wrapper — keep them in sync if this workflow's shape changes.
- Secure coding & review: follow `docs/agent-workflows/secure-coding.md` when writing or reviewing code that touches auth, tenant isolation, SQL/queries, request validation, subprocess/git, frontend/UI, outbound HTTP, secrets, deserialization, resource limits, CORS/transport, LLM-agent safety, webhooks, logging, CI/CD, container/infra, consent/privacy/data retention, or capability-readiness/lifecycle integrity — and when the user asks to check security or review for vulnerabilities. It is the canonical security convention for the monorepo, wrapped as the `secure-coding` skill.
- CI/CD safety: follow `docs/agent-workflows/ci-cd-safety.md` when merging or babysitting stacked PRs, retargeting PR bases, resolving merge conflicts against `main`, editing GitHub Actions workflows, interpreting CI results, or cutting a release. It is the canonical CI/CD operating convention for the monorepo, wrapped as the `ci-cd-safety` skill.

## What is APDL?

Autonomous Product Development Loop — a self-optimizing product analytics and experimentation platform. It ingests user behavior events, runs analytics queries, evaluates feature flags and A/B experiments, and uses LLM-powered agents to autonomously generate insights, design experiments, and personalize user experiences.

## Build & Development Commands

```bash
make setup              # Full local dev setup (uv venvs, npm install, Docker deps, migrations, .env)
make build              # Build SDK
make test               # Run all tests
make lint               # Run all linters
make check              # Lint + test every package in parallel (local CI mirror)
make fmt                # Auto-format all packages (ruff format + autofix)
make dev                # Start Docker deps only (Redis, ClickHouse, PostgreSQL)
make dev-core           # Start the supported core stack (default application path)
make dev-all            # Start full stack via Docker Compose (core + Agents + Codegen)
make dev-down           # Stop all containers
make status             # Container status + service health endpoints
make smoke              # End-to-end smoke test against the running stack
make smoke-fresh        # Hermetic fresh-install core proof (clean volumes)
make smoke-experiment-fresh # Hermetic fresh-install experiment lifecycle proof
make test-clickhouse-upgrade # Pinned-image ClickHouse upgrade smoke
make migrate-clickhouse # Apply ClickHouse SQL migrations
make migrate-postgres   # Apply PostgreSQL SQL migrations
```

`scripts/dev.sh` is the master entry point wrapping all of the above
(`setup`, `up`, `up-core`, `up-full`, `smoke-fresh`, `status`, `smoke`,
`test`, `lint`, `fmt`, `check`, `logs`, `down`, `reset`).

### Running individual services (with hot-reload)

```bash
make run-ingestion  # Ingestion Service → localhost:8080
make run-config     # Config Service    → localhost:8081
make run-query      # Query Service     → localhost:8082
make run-agents     # Agents Service    → localhost:8083
make run-codegen    # Codegen Service   → localhost:8084
make run-admin-api  # Admin API (console gateway) → localhost:8085
make run-pipeline   # ClickHouse Writer (Redis Streams consumer)
make run-admin      # Admin Console (Vite dev server) → localhost:5173
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
| Admin API | `make test-admin-api` | `make lint-admin-api` |
| Admin Console | `make test-admin` | `make lint-admin` |
| ClickHouse Writer | `make test-writer` | `make lint-writer` |

### Running a single test

```bash
# SDK — JavaScript (Vitest)
cd sdk/javascript && npm test -- core/client.test.ts

# SDK — Python (pytest)
cd sdk/python && .venv/bin/python -m pytest tests/test_evaluator.py -v

# Python services (pytest)
cd services/ingestion && .venv/bin/python -m pytest tests/test_events.py -v
cd services/config && .venv/bin/python -m pytest tests/test_evaluator.py -v
cd services/query && .venv/bin/python -m pytest tests/test_funnels.py -v
cd services/agents && .venv/bin/python -m pytest tests/test_supervisor.py::test_specific -v
cd services/codegen && .venv/bin/python -m pytest tests/test_job_runner.py -v
cd services/admin-api && .venv/bin/python -m pytest tests/test_proxy.py -v
```

## Architecture Overview

The system is a monorepo with six Python services, a data pipeline, and two client SDKs (a browser TypeScript SDK and a server-side Python SDK):

```
SDK (TypeScript) ──POST /v1/events──→ Ingestion (Python/FastAPI :8080) ──→ Redis Streams
                 ←─SSE /v1/stream──── Config (Python/FastAPI :8081) ←───→ PostgreSQL + Redis Cache
                                              ↑
Redis Streams ──→ ClickHouse Writer (Python) ──→ ClickHouse
                                                      ↓
                                              Query Service (Python/FastAPI :8082)
                                                      ↓
                                              Agents Service (Python/FastAPI :8083)
                                              ↕ PostgreSQL (pgvector) for memory
                                                      ↓
                                              Codegen Service (Python/FastAPI :8084)
                                              → GitHub App → customer repos (autonomous PRs)

Admin Console (browser) ──same-origin /api──→ Admin API (Python/FastAPI :8085)
                                              → project-scoped credentials → core services
```

### Data Flow

1. **Event ingestion:** SDK → Ingestion Service (auth, rate-limit, schema validation) → Redis Streams (`events:raw:{project_id}`)
2. **Event pipeline:** ClickHouse Writer consumes Redis Streams in batches (1000 events or 5s flush) → ClickHouse (MergeTree tables, materialized views)
3. **Flag distribution:** Config Service stores flags/experiments in PostgreSQL, caches in Redis, pushes updates via SSE to SDK
4. **Flag evaluation:** SDKs evaluate flags locally using FNV-1a bucketing (no server round-trip for evaluation). The JS SDK, the Python SDK, and the Config Service share a byte-for-byte identical hash so a user buckets identically everywhere
5. **Analytics:** Query Service queries ClickHouse for funnels, cohorts, retention, experiment stats (frequentist/Bayesian/sequential)
6. **Autonomous agents:** Lightweight graph runner orchestrates LLM-driven workflows — behavior analysis, experiment design, personalization, feature proposals. Actions pass through safety validation with audit logging and rollback support
7. **Autonomous code:** Codegen Service turns approved feature proposals into tested-green pull requests on connected customer repos via a sandboxed, model-agnostic OSS coding agent (Aider); merge is gated on green CI + autonomy level, audited like every other action
8. **Admin operations:** the browser console talks only to the Admin API gateway (same-origin `/api`), which authenticates hashed sessions with CSRF/origin enforcement and proxies each call to the core services using server-selected, project-scoped credentials, writing proxy audit records

### Tech Stack by Service

- **SDK — JS** (`sdk/javascript/`): TypeScript, Rollup (ESM/CJS/IIFE), Vitest (jsdom)
- **SDK — Python** (`sdk/python/`): Python 3.12, server-side client, httpx, Pydantic — uv, pytest, ruff
- **Ingestion** (`services/ingestion/`): Python 3.12, FastAPI, redis (hiredis), Pydantic — uv, pytest, ruff
- **Config** (`services/config/`): Python 3.12, FastAPI, asyncpg, redis (hiredis), sse-starlette, Pydantic — uv, pytest, ruff
- **Query** (`services/query/`): Python 3.12, FastAPI, clickhouse-driver/asynch, SciPy, NumPy — uv, pytest-asyncio, ruff
- **Agents** (`services/agents/`): Python 3.12, FastAPI, openai, anthropic, google-genai, asyncpg, pgvector — uv, pytest-asyncio, ruff
- **Codegen** (`services/codegen/`): Python 3.12, FastAPI, asyncpg, httpx, pyjwt (GitHub App), Aider (model-agnostic editor via LiteLLM) — uv, pytest-asyncio, ruff. The "hands" of the autonomous loop: opens/merges PRs on customer repos
- **Admin API** (`services/admin-api/`): Python 3.12, FastAPI, asyncpg, httpx — uv, pytest, ruff. Security gateway for the Admin Console: hashed sessions, CSRF/origin enforcement, login lockouts, memberships, and audited proxying to the core services
- **Pipeline** (`pipeline/redis/`): Python 3.12, redis async client, clickhouse-driver

### Key Ports

| Service | Port |
|---------|------|
| Gateway (SDK front door) | 8000 |
| Ingestion | 8080 |
| Config | 8081 |
| Query | 8082 |
| Agents | 8083 |
| Codegen | 8084 |
| Admin API (console gateway) | 8085 |
| Admin Console | 5173 |
| Redis | 6379 |
| ClickHouse HTTP / Native | 8123 / 9000 |
| PostgreSQL | 5432 |

## Tooling & Conventions

- **Python package management:** `uv` (not pip directly). Each Python service has its own `.venv/` directory
- **Python linting:** `ruff check app/` (default config, no pyproject.toml overrides)
- **JS SDK linting:** `tsc --noEmit` (strict mode, no unused locals/params)
- **JS SDK test pattern:** `__tests__/**/*.test.ts`
- **Python test pattern:** `tests/` directory in each service and in `sdk/python/`
- **CI runs on push/PR to main:** lint, tests, builds, package contracts, dependency audits, isolated core/experiment smokes for the declared developer-preview surface, and a pinned-image ClickHouse upgrade smoke
- **Dependency updates:** manual, per `docs/dependency-policy.md` — Dependabot version updates are not enabled on this repository
- **Releases:** the tag must match `release-manifest.json`; `v0.3.0` publishes the JavaScript SDK to npm, the Python SDK to PyPI, and source/checksum assets to GitHub Releases. No GHCR images are published for this release line

## Environment Variables

Infrastructure defaults for local dev (set via `make setup` from `.env.example`):

```
REDIS_URL=redis://localhost:6379
POSTGRES_URL=postgresql://apdl:apdl_dev@localhost:5432/apdl
CLICKHOUSE_URL=http://localhost:8123
CLICKHOUSE_NATIVE_URL=clickhouse://apdl:apdl_dev@localhost:9000/apdl
```

Agents service requires at least one of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, or `LOCAL_LLM_URL` for LLM access.
