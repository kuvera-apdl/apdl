# Agents Service

LLM-powered analysis and proposal workflows for the **Autonomous Product
Development Loop** — an opt-in operator preview on port **8083**.

## What it does

Agents **read analytics** from the Query Service (funnels, retention, event
counts) and **reason** over them with an LLM to produce insights and experiment
designs. The 0.3.0 workflow deliberately remains open: every experiment design
requires human approval, an approval creates an inert Config draft with a
disabled backing flag, treatment work follows a separate changeset lifecycle,
and activation remains a separate operator action. Insights are persisted to
pgvector memory so later runs can build on earlier findings.

Experiment results are read-only in the OSS developer preview. Autonomous
evaluation, treatment deployment, activation, stopping, shipping,
feature-proposal generation, personalization, and rollback are disabled.

The `personalization` graph is disabled in 0.3.0. Config has no canonical
UI-config storage/delivery API, so the trigger API rejects that graph, it is
hidden from definitions, and custom agents cannot select UI-config tools.

Execution is enabled only for operator-provisioned projects in the OSS
developer preview. Projects created through the public workspace flow retain
read-only definitions, run history, results, and audit access, but cannot
trigger runs, manage/test custom agents, or approve work. This is enforced from
canonical project provenance even when a credential incorrectly contains an
execution role.

A run is orchestrated by the **supervisor** (`app/graphs/supervisor.py`): it
resolves the requested agents from a registry, runs them in pipeline order,
skips agents whose `requires` inputs are missing, and threads a shared state
dict between them.

## API

All agent routes require a registered `X-API-Key`. Read, trigger, custom-agent
management, and approval operations use distinct project-scoped roles. The
`agents:run`, `agents:manage`, and `agents:approve` roles are honored only for
operator-provisioned projects (`admin_projects.created_by IS NULL`).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/agents/trigger` | Start an agent run (runs in the background; returns `run_id`) |
| `GET` | `/v1/agents/{run_id}/status` | Run status, phase, insight/experiment counts |
| `POST` | `/v1/agents/{run_id}/approve` | Approve or reject a run waiting at an approval gate |
| `GET` | `/health` | Liveness probe |
| `GET` | `/ready` | Core readiness (runtime initialization and PostgreSQL) |
| `GET` | `/ready/capabilities` | Non-blocking configured/reachable report for LLM, Query, Config, and Codegen |

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
`{"decisions": [{"item_id": "exp_checkout", "approved": true}], "comment": "..."}`.

## Agent graphs

Agents self-register via `@register_agent` and run in `order`:

| Name | Tier | Requires | Produces | What it does |
|------|------|----------|----------|--------------|
| `behavior_analysis` | reasoning | — | `insights` | Plans + runs analytics queries, synthesises insights |
| `experiment_design` | reasoning | `insights` | `experiment_designs` | Designs an A/B experiment for mandatory human approval |
| `experiment_evaluation` | reasoning | — | — | **Disabled**; experiment results require human interpretation |
| `personalization` | fast | `insights` | `personalizations` | **Disabled**; no Config storage or delivery contract exists |
| `feature_proposal` | reasoning | — | — | **Disabled**; statistical snapshots do not assess deployment readiness |

Scaffold a new agent with `scripts/new_agent.py`.

## LLM router

`app/llm/router.py` routes by tier (`fast` for cheap tasks, `reasoning` for
analysis/design). Each tier tries **OpenAI → Anthropic → Google → local** in
order, skipping providers whose API key is unset, and falls back to the next
provider on failure. Defaults are overridable per slot
(`LLM_FAST_PRIMARY`, `LLM_REASONING_FALLBACK`, `LOCAL_LLM_MODEL`, …).

## Approval and safety boundary

These levels apply only to operator-provisioned projects. Self-created projects
cannot start or resume an Agents execution at any autonomy level.

`gate_action` (`app/framework/gating.py`) is a generic policy primitive, not a
claim that the enabled product flow is autonomous. Experiment design invokes it
with mandatory approval: L1 performs no Config mutation, while L2 through L4
all stop at the same human gate. No autonomy level enables a flag or starts an
experiment in 0.3.0. Actions that fail static validation halt.

- **Static safety validator** (`app/safety/validator.py`) — canonical-shape,
  Config-conflict, and proposed blast-radius checks (including variant weights
  and a control group). Experiments require one primary metric and a clear
  hypothesis. These are pre-draft checks, not live guardrail monitoring or
  evidence that a treatment is safe to activate.
- **Audit log** (`app/safety/audit.py`) — records workflow steps, decisions,
  validator results, and human approvals in PostgreSQL.
- **Rollback surface** (`app/safety/rollback.py`) — explicitly unavailable and
  fails closed. It neither decides nor executes an automatic rollback.

### Experiment draft, treatment, and activation lifecycle

1. The LLM produces a design and the validator checks the proposal.
2. A human approves or rejects each design.
3. Approval creates an inert experiment draft and disabled backing flag in
   Config. No users are assigned treatment.
4. If code is required, Agents may ask a separately provisioned Codegen worker
   to open a treatment changeset. Creating the draft does not prove that code
   exists, passes review, or has merged.
5. After implementation and review, an operator must explicitly activate the
   experiment through the Config/Admin lifecycle. Agents does not perform or
   infer that activation.

When upgrading an existing deployment to migration 014, stop the Agents
service before applying PostgreSQL migrations. The migration fails existing
self-created-project work and reopens its exact proposal claims, but it cannot
cancel an already in-flight provider HTTP call inside a still-running process.

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

`ui_config.py` remains only as code for the parked personalization prototype;
no enabled built-in or custom-agent catalog entry can invoke it in 0.3.0.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `POSTGRES_URL` | `postgresql://apdl:apdl_dev@localhost:5432/apdl` | Runs, audit log, pgvector memory |
| `QUERY_SERVICE_URL` | `http://localhost:8082` | Analytics queries |
| `CONFIG_SERVICE_URL` | `http://localhost:8081` | Flag and experiment CRUD |
| `CODEGEN_SERVICE_URL` | `http://localhost:8084` | Optional treatment changeset requests |
| `AGENTS_ENABLE_AUTONOMOUS_MUTATIONS` | `false` | Reserved operator switch for eligible future actions; exact `true` only and does not bypass mandatory gates |
| `OPENAI_API_KEY` | — | OpenAI provider |
| `ANTHROPIC_API_KEY` | — | Anthropic provider |
| `GOOGLE_API_KEY` | — | Google provider |
| `LOCAL_LLM_URL` | — | OpenAI-compatible local server (e.g. Ollama at `http://localhost:11434/v1`) |
| `LOCAL_LLM_MODEL` / `LLM_FAST_*` / `LLM_REASONING_*` | per-tier defaults | Model overrides |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Local fastembed model (dimension must be known or set via `EMBEDDING_DIMENSIONS`) |
| `APDL_SERVICE_API_KEYS` | — | Production project-to-key JSON for scoped Config/Query/Codegen calls |
| `APDL_DEV_API_KEY` | — | Local-only fallback key when the service-key map is unset |

At least one provider API key or `LOCAL_LLM_URL` is required to execute an
LLM-backed run. `/ready/capabilities` reports provider and service
availability, but its degraded state does not make the core `/ready` endpoint
fail.

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
