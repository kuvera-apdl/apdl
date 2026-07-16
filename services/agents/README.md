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

Execution is enabled only for operator-provisioned projects or self-created
projects with an explicit, audited operator override in the OSS developer
preview. Projects created through the public workspace flow retain read-only
definitions, run history, results, and audit access by default. This is
enforced from canonical project execution authority even when a credential
incorrectly contains an execution role.

A run is orchestrated by the **supervisor** (`app/graphs/supervisor.py`): a
PostgreSQL-backed dispatcher leases queued runs on any replica, the supervisor
resolves agents in pipeline order, and each result is persisted before
post-result bookkeeping or a later approval effect. Expired ownership is
requeued at the persisted phase rather than terminalized as a failed run.

## API

All agent routes require a registered `X-API-Key`. Read, trigger, custom-agent
management, and approval operations use distinct project-scoped roles. The
`agents:run`, `agents:manage`, and `agents:approve` roles are honored only for
projects with a canonical `admin_project_execution_authorizations` row.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/agents/trigger` | Durably queue an agent run; returns `run_id` |
| `GET` | `/v1/agents/{run_id}/status` | Run status, phase, insight/experiment counts |
| `POST` | `/v1/agents/{run_id}/cancel` | Durably cancel an active run and fence further work |
| `POST` | `/v1/agents/{run_id}/approve` | Validate exact per-item decisions and queue an approval command (`202`) |
| `GET` | `/v1/agents/{run_id}/approvals/{command_id}` | Command and per-effect retry/manual-intervention status |
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
The request accepts no whole-gate alias or unknown fields. Decisions must name
every persisted gate item exactly once. Its response is a queued-command
envelope containing `command_id`, gate/count fields, timestamps, and an
`effects` array. Command status is `queued`, `processing`, `succeeded`, or
`manual_intervention`; each effect also exposes `retryable_failed` and its
attempt/error/result fields. Config and Codegen calls run only in the durable
effect worker with a persisted idempotency key and PostgreSQL quota reservation.

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
analysis/design), but environment variables only define candidate models; they
do not authorize tenant data egress. Every call carries an explicit project,
run, purpose, execution kind, and data classification. Before network egress,
the router loads the project's exact provider/model/residency policy, reserves
the worst-case run and daily cost atomically, and persists the logical call and
provider attempt. It then records provider/model, prompt hash, usage, cost,
latency, outcome, and retry classification without storing prompt content.

Migration 023 creates one safe default policy for every project: only the exact
local model `gemma4` at `http://localhost:11434/v1`, local residency, zero paid
spend, and no cross-vendor retry. Enabling OpenAI, Anthropic, or Google requires an operator to update
`llm_project_policies` and insert the exact provider/model row in
`llm_project_provider_policies`, including allowed data classifications,
residency, current input/output prices, and positive project-daily and per-run
ceilings. A provider failure crosses to another vendor only when the project
policy explicitly permits it and the failure is classified as retryable.

## Approval and safety boundary

These levels apply only to projects with canonical execution authority.
Self-created projects cannot start or resume an Agents execution at any
autonomy level unless an operator has recorded the explicit override.

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
- **Audit log** (`app/safety/audit.py`) — authoritative human decisions and
  mutation intents are committed with their command/outbox transaction before
  external work. Best-effort logging is reserved for non-authoritative
  workflow telemetry.
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
| `LOCAL_LLM_MODEL` / `LLM_FAST_*` / `LLM_REASONING_*` | per-tier defaults | Candidate model names; each exact provider/model must also be authorized by project policy |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Local fastembed model (dimension must be known or set via `EMBEDDING_DIMENSIONS`) |
| `APDL_SERVICE_API_KEYS` | — | Production project-to-key JSON for scoped Config/Query/Codegen calls |
| `APDL_DEV_API_KEY` | — | Local-only fallback key when the service-key map is unset |

At least one policy-authorized provider must also be configured and reachable
to execute an LLM-backed run. API keys alone grant no project permission.
`/ready/capabilities` reports process-level provider and service availability,
but its degraded state does not make the core `/ready` endpoint fail and does
not assert that any particular project's policy permits egress.

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
