# Codegen Service

FastAPI service (`:8084`) — the autonomous-development "hands" of APDL. It
connects to customer repositories and produces **changesets** (branch + commits
+ pull request). GitHub is the sole authority for CI verification, review rules,
and merge. APDL observes those results and may push bounded repair commits.

Orchestration, autonomy gating, safety validation, and human approvals live in
the **agents service** (`:8083`), which calls this service over the internal
API. See `local-files/docs/plans/codegen-service-implementation-plan.md` for the
full design and phase plan.

## Status

The Phase 0 trust boundary is implemented, with the editing engine **reworked from Claude Managed
Agents to a model-agnostic OSS coding agent ([Aider](https://github.com/Aider-AI/aider))**.
The service connects repos (GitHub App), produces changesets by running Aider in
a sandboxed clone, runs deterministic pre-push safety gates, opens draft PRs,
ingests GitHub CI/PR status (webhook + poll), and makes bounded same-branch
repairs from failure evidence. The editor model is a config choice — `CODEGEN_MODEL`
takes any LiteLLM model id (Claude by default, GPT, Gemini, local, …). The real
agent path is **integration-untested** here: it needs `aider` on `PATH`, a model
provider key, and a connected repo. The tested core (job runner, safety gates,
PR/CI observation and agents queue) runs through a fake editor.

### Canonical repository profiler

Phase 1 replaces editor/API-specific heuristics with one strict `RepoProfile`
contract. Local clones and bounded GitHub snapshots use the same adapters for
Node/TypeScript, Python, Go, Rust, Gradle/Maven JVM, and .NET repositories. The
profile records package/workspace boundaries, exact lockfile versions when
available, commands, test/browser facilities, routes and entrypoints, services,
deployment and CI files, scoped `AGENTS.md` contents, branch protection, and
high-risk paths. Conflicting package managers, unresolved versions, unavailable
protection metadata, and truncated snapshots are returned as explicit
`uncertainties`; they are never converted into inferred fallback facts.

## API

All `/v1` endpoints require the `X-APDL-Internal-Token` header when
`APDL_INTERNAL_TOKEN` is configured (permissive in local dev).

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/connections` | Register/update a project's repo binding (`installation_id`, `repo`) |
| GET | `/v1/connections/{project_id}` | Resolve a project's repo binding |
| GET | `/v1/connections/{project_id}/repo-context` | Strict canonical `repo_profile@1` for planning agents |
| POST | `/v1/changesets` | Enqueue a changeset for a connected project |
| GET | `/v1/changesets?project_id=…` | List a project's changesets |
| GET | `/v1/changesets/{id}` | Fetch one changeset |
| POST | `/v1/changesets/{id}/abandon` | Abandon an un-merged changeset |
| POST | `/webhooks/github` | HMAC-verified CI ingestion (`check_run`/`pull_request`) |
| GET | `/health`, `/ready` | Liveness / PostgreSQL readiness |

## Changeset lifecycle

```
queued → cloning → editing → testing ──▶ tests_failed (generation/safety failure)
                                  └─▶ pushing → pr_open → ci_running ──▶ ci_passed
                                                              ├─▶ ci_failed → bounded repair → ci_running
                                                              └─▶ unverified_external_ci

GitHub pull-request events move an open changeset to `merged` or `abandoned`.
```

Transitions are enforced by `app/models/changeset.py`; illegal moves raise
`InvalidTransition` (HTTP 409).

## Environment

```
POSTGRES_URL=postgresql://apdl:apdl_dev@localhost:5432/apdl
APDL_INTERNAL_TOKEN=apdl-dev-internal-token
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY=            # PEM inline (escaped \n accepted), or…
GITHUB_APP_PRIVATE_KEY_BASE64=     # …base64 of the .pem (easiest in Docker), or…
GITHUB_APP_PRIVATE_KEY_PATH=       # …a path to the .pem file (~ expanded)
GITHUB_API_URL=https://api.github.com
GITHUB_WEBHOOK_SECRET=             # HMAC secret for /webhooks/github
CODEGEN_MODEL=claude-opus-4-8      # editor model — any LiteLLM id
ANTHROPIC_API_KEY=                 # provider key matching CODEGEN_MODEL
                                   #   (or OPENAI_API_KEY / GOOGLE_API_KEY / …)
CODEGEN_KILL_SWITCH=               # "true" halts all changeset jobs
CODEGEN_DISABLED_PROJECTS=         # comma-separated per-project denylist
```

Optional editor tunables: `CODEGEN_AIDER_BIN` (default `aider`), `CODEGEN_WORKDIR`
(throwaway-clone base), and the `CODEGEN_TIMEOUT` /
`CODEGEN_GIT_TIMEOUT` second caps. A whole job (clone + retry rounds + push) is
bounded by `codegen_job_budget()`, which also caps the sandbox container and the orphan-sweep
deadline; override with `CODEGEN_JOB_BUDGET` if the derivation doesn't fit. A
repo's verification command comes from connection `policy.test_cmd`; if unset,
the editor auto-detects it (pytest / npm / make / …) and gives it to the model as
test-generation guidance. APDL does not execute it authoritatively; GitHub CI does.
The pre-push gates run inside
the editor on the full diff (a violating branch never reaches GitHub), with the
connection `policy.gates` overrides; the job runner re-checks them as a backstop
before opening the PR. Orphan recovery: queued changesets are re-enqueued at
startup (the queued → cloning transition is the dedup claim); active-state
orphans are swept to `error` at startup and every `CODEGEN_STALE_SWEEP_INTERVAL`
(default 300s) once older than twice the job budget.

Two auxiliary LLM passes bracket the edit. Low-risk work may skip them when the
model is unavailable; medium/high-risk work fails closed. `CODEGEN_BRIEF` compiles the
approved spec into a repo-grounded engineering brief before the agent runs
(concrete files, explicit descoping of non-repo asks, checkable acceptance
criteria), and `CODEGEN_REVIEW` judges the produced diff against the original
spec before the push. A review rejection re-invokes the agent with feedback
(`CODEGEN_EDIT_RETRIES`, default 1) before the changeset fails; the retry message
re-carries the full work order, since each aider
invocation is a fresh process. `CODEGEN_HELPER_MODEL` runs these passes on a
different model than the editor (default: `CODEGEN_MODEL`).

GitHub merge observation records the merge commit SHA, and `/revert` uses it deterministically:
the editor fetches the commit into the shallow clone and runs `git revert`
(mainline parent 1 for merge commits). APDL exposes no merge endpoint or tool.

## Develop

```bash
make run-codegen         # uvicorn on :8084 (hot reload)
make test-codegen        # pytest
make lint-codegen        # ruff
```

To exercise the real editor locally, install the agent extra so `aider` is on
`PATH`: `cd services/codegen && uv pip install -e ".[agent]"`.

## Editor execution model

The editor sits behind the `Editor` interface; *how/where* it runs is config:

- **In-process (default)** — `AiderEditor` runs `git`/`aider`/tests as
  subprocesses in the codegen process, in a throwaway workdir. Simplest, but it
  **executes untrusted repo code in the codegen container** — use it only for
  trusted repos / local dev.
- **Sandboxed container (`CODEGEN_SANDBOX=docker`)** — `ContainerAiderEditor`
  runs each changeset in an ephemeral container from `Dockerfile.worker`
  (`--rm`, `--cap-drop ALL`, `no-new-privileges`, pid/memory/cpu caps, non-root),
  reusing the same `AiderEditor` inside it. Untrusted code never touches the API
  container's secrets; the sandbox only gets the short-lived install token (which
  the runner drops from the env before aider/tests run) and the model key.

Enable the sandbox:

```bash
make build-codegen-sandbox        # build apdl-codegen-sandbox:latest
export CODEGEN_SANDBOX=docker
# If codegen itself runs in a container, mount /var/run/docker.sock (see compose)
# so it can launch the sandbox (Docker-out-of-Docker); on a Docker host it just works.
```

Remaining deploy-time hardening: an egress allowlist (GitHub + registries only;
block the internal CIDR + `169.254.169.254`) via `CODEGEN_SANDBOX_NETWORK`, and a
read-only rootfs. Tunables: `CODEGEN_SANDBOX_IMAGE`, `CODEGEN_SANDBOX_MEMORY`,
`CODEGEN_SANDBOX_CPUS`, `CODEGEN_SANDBOX_PIDS`, `CODEGEN_DOCKER_BIN`.

## Going live (end-to-end)

The autonomous loop runs once these external pieces are set up:

1. **Register a GitHub App** (org-level) with minimal permissions — `contents:
   write`, `pull_requests: write`, `checks: read`, `metadata: read`. Set
   `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY`. Customers install it on their
   repos; record each installation via `POST /v1/connections`.
2. **Provision the coding agent.** Make `aider` available where the editor runs
   — `uv pip install -e ".[agent]"` on the codegen host for v1, or build the
   hardened sandbox image (`Dockerfile.worker`) to run one changeset per
   container. Set `CODEGEN_MODEL` and the matching provider key (e.g.
   `ANTHROPIC_API_KEY`). Optionally set each repo's test command via the
   connection `policy.test_cmd` (otherwise it is auto-detected).
3. **Add a repo webhook** → `POST /webhooks/github`, secret
   `GITHUB_WEBHOOK_SECRET`, events `check_run` + `pull_request`.
4. **Enable GitHub branch protection/rulesets** on the default branch (require PR,
   reviews, and green checks). GitHub is the enforcement and merge authority.

Flow: an approved feature proposal enqueues a `code_implementation` run (agents
service) → `POST /v1/changesets` → the job mints a repo token, runs the Aider
editor in a sandboxed clone, runs deterministic pre-push gates,
pushes a branch, and opens a **draft PR** → the repo's CI runs →
the webhook records `ci_passed` or feeds failure annotations into a bounded
same-branch repair → GitHub reviews/rulesets decide readiness and merge.
