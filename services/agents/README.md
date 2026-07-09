# Agents Service

LLM-powered autonomous agents for the **Autonomous Product Development Loop** —
FastAPI service on port **8083**.

## What it does

Closes the product loop autonomously: agents **read analytics** from the Query
Service (funnels, retention, event counts), **reason** over them with an LLM to
produce insights, and **act** — creating experiments, flags, and server-driven
UI configs through the Config Service. Every action passes a safety validator
and an autonomy gate before deployment, is written to an audit log, and
deployed experiments can be auto-rolled-back on metric degradation. Insights
are persisted to pgvector memory so later runs build on earlier findings.

A run is orchestrated by the **supervisor** (`app/graphs/supervisor.py`): it
resolves the requested agents from a registry, runs them in pipeline order,
skips agents whose `requires` inputs are missing, and threads a shared state
dict between them.

## API

All agent routes require a registered `X-API-Key`. Read, trigger, custom-agent
management, and approval operations use distinct project-scoped roles.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/agents/trigger` | Start an agent run (runs in the background; returns `run_id`) |
| `GET` | `/v1/agents/{run_id}/status` | Run status, phase, insight/experiment counts |
| `POST` | `/v1/agents/{run_id}/approve` | Approve or reject a run waiting at an approval gate |
| `GET` | `/health` | Liveness probe |
| `GET` | `/ready` | Readiness probe (checks PostgreSQL connectivity) |

```bash
curl -X POST localhost:8083/v1/agents/trigger \
  -H 'X-API-Key: proj_default_<secret>' \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id": "default",
    "trigger_type": "manual",
    "analysis_types": ["behavior_analysis", "experiment_design"],
    "time_range_days": 14,
    "autonomy_level": 2
  }'
# → {"run_id": "<uuid>", "status": "started"}
```

`trigger_type` is `scheduled`, `manual`, or `threshold_alert`. To approve a
waiting run: `POST /v1/agents/{run_id}/approve` with
`{"approved": true, "comment": "..."}`.

## Agent graphs

Agents self-register via `@register_agent` and run in `order`:

| Name | Tier | Requires | Produces | What it does |
|------|------|----------|----------|--------------|
| `behavior_analysis` | reasoning | — | `insights` | Plans + runs analytics queries, synthesises insights |
| `experiment_design` | reasoning | `insights` | `experiment_designs` | Designs an A/B experiment and deploys it through the safety gate |
| `personalization` | fast | `insights` | `personalizations` | Generates segment-targeted server-driven UI configs |
| `feature_proposal` | reasoning | `insights` | `feature_proposals` | Proposes new features; always requires human approval |

Scaffold a new agent with `scripts/new_agent.py`.

## LLM router

`app/llm/router.py` routes by tier (`fast` for cheap tasks, `reasoning` for
analysis/design). Each tier tries **OpenAI → Anthropic → Google → local** in
order, skipping providers whose API key is unset, and falls back to the next
provider on failure. Defaults are overridable per slot
(`LLM_FAST_PRIMARY`, `LLM_REASONING_FALLBACK`, `LOCAL_LLM_MODEL`, …).

## Autonomy & safety

Every acting agent funnels its safety result through `gate_action`
(`app/framework/gating.py`):

| Level | Behavior |
|-------|----------|
| L1 | Suggest only — never mutates anything, even if safety passes |
| L2 | Auto-applies safe low-impact actions (e.g. targeted UI configs); everything else is queued for human approval |
| L3 | Auto-deploys validated **low-risk** actions; medium/high risk goes to approval |
| L4 | Full autonomy — still routes non-low-risk and always-approve actions (feature proposals) to approval |

Actions that fail safety validation always halt.

- **Safety validator** (`app/safety/validator.py`) — per-action-type rate
  limits (5 experiments, 20 flag updates, 30 UI configs, 3 proposals per hour),
  conflict detection, blast-radius checks (variant weights ≤ 100%, control
  group ≥ 10%, UI configs must be targeted), and guardrail checks (experiments
  need guardrail metrics, a primary metric, and a hypothesis; proposals need
  documented risks and success criteria). Outputs a `low`/`medium`/`high` risk level.
- **Audit log** (`app/safety/audit.py`) — every step, decision, safety result,
  and approval is written to the `agent_audit_log` table in PostgreSQL.
- **Auto-rollback** (`app/safety/rollback.py`) — monitors deployed experiments
  against a baseline and disables the experiment's flag via the Config Service
  if error rate (+0.5 pp), p95 latency (+20%), or the primary metric (−2 pp)
  degrades past thresholds.

## Memory

`app/memory/pgvector_store.py` stores agent insights/designs as text in the
`agent_memory` table with local fastembed embeddings (384-dim
`BAAI/bge-small-en-v1.5` by default, ivfflat cosine index — no API key
needed). Agents retrieve project-scoped, semantically similar prior memories
at the start of a run, with optional metadata filters.

## Tools

Thin async HTTP wrappers the agents call (`app/tools/`):

- `clickhouse.py` — Query Service: event counts, timeseries, funnels, retention
- `flags.py` — Config Service admin API: list/create/update flags, evaluate gates
- `experiments.py` — list active experiments, create experiment config + its flag, fetch results
- `ui_config.py` — create/update server-driven UI configs

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `POSTGRES_URL` | `postgresql://apdl:apdl_dev@localhost:5432/apdl` | Runs, audit log, pgvector memory |
| `QUERY_SERVICE_URL` | `http://localhost:8082` | Analytics queries |
| `CONFIG_SERVICE_URL` | `http://localhost:8081` | Flag/experiment/UI-config CRUD |
| `OPENAI_API_KEY` | — | OpenAI provider |
| `ANTHROPIC_API_KEY` | — | Anthropic provider |
| `GOOGLE_API_KEY` | — | Google provider |
| `LOCAL_LLM_URL` | — | OpenAI-compatible local server (e.g. Ollama at `http://localhost:11434/v1`) |
| `LOCAL_LLM_MODEL` / `LLM_FAST_*` / `LLM_REASONING_*` | per-tier defaults | Model overrides |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Local fastembed model (dimension must be known or set via `EMBEDDING_DIMENSIONS`) |
| `APDL_SERVICE_API_KEYS` | — | Production project-to-key JSON for scoped Config/Query calls |
| `APDL_DEV_API_KEY` | — | Local-only fallback key when the service-key map is unset |

At least one of the four LLM credentials is required.

## Running locally

```bash
make dev          # start Redis, ClickHouse, PostgreSQL
make run-agents   # uvicorn with hot-reload → localhost:8083
```

Set at least one LLM key in `.env` (created by `make setup`).

## Tests

```bash
make test-agents   # pytest
make lint-agents   # ruff check app/
```
