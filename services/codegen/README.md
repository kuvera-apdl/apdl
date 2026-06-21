# Codegen Service

FastAPI service (`:8084`) — the autonomous-development "hands" of APDL. It
connects to customer repositories, produces **changesets** (branch + commits +
pull request), and — under policy — merges them. It is the only component that
holds the GitHub App credentials and runs untrusted customer/AI-authored code in
a sandbox, isolated from the rest of the platform.

Orchestration, autonomy gating, safety validation, and human approvals live in
the **agents service** (`:8083`), which calls this service over the internal
API. See `local-files/docs/plans/codegen-service-implementation-plan.md` for the
full design and phase plan.

## Status

Phases 0–7 implemented, with the editing engine **reworked from Claude Managed
Agents to a model-agnostic OSS coding agent ([Aider](https://github.com/Aider-AI/aider))**.
The service connects repos (GitHub App), produces changesets by running Aider in
a sandboxed clone, runs deterministic pre-push safety gates, opens draft PRs,
ingests CI status (webhook + poll), merges on green CI under the autonomy gate,
and reverts merged changes. The editor model is a config choice — `CODEGEN_MODEL`
takes any LiteLLM model id (Claude by default, GPT, Gemini, local, …). The real
agent path is **integration-untested** here: it needs `aider` on `PATH`, a model
provider key, and a connected repo. The tested core (job runner, safety gates,
PR/CI/merge, agents queue) runs through a fake editor.

## API

All `/v1` endpoints require the `X-APDL-Internal-Token` header when
`APDL_INTERNAL_TOKEN` is configured (permissive in local dev).

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/connections` | Register/update a project's repo binding (`installation_id`, `repo`) |
| GET | `/v1/connections/{project_id}` | Resolve a project's repo binding |
| POST | `/v1/changesets` | Enqueue a changeset for a connected project |
| GET | `/v1/changesets?project_id=…` | List a project's changesets |
| GET | `/v1/changesets/{id}` | Fetch one changeset |
| POST | `/v1/changesets/{id}/abandon` | Abandon an un-merged changeset |
| POST | `/v1/changesets/{id}/merge` | Merge the PR (green CI required; APDL-gated) |
| POST | `/webhooks/github` | HMAC-verified CI ingestion (`check_run`/`pull_request`) |
| GET | `/health`, `/ready` | Liveness / PostgreSQL readiness |

## Changeset lifecycle

```
queued → cloning → editing → testing ──▶ tests_failed (terminal)
                                  └─▶ pushing → pr_open → ci_running ──▶ ci_failed
                                                              └─▶ ci_passed → (waiting_approval | merged | abandoned)
```

Transitions are enforced by `app/models/changeset.py`; illegal moves raise
`InvalidTransition` (HTTP 409).

## Environment

```
POSTGRES_URL=postgresql://apdl:apdl_dev@localhost:5432/apdl
APDL_INTERNAL_TOKEN=apdl-dev-internal-token
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY=            # PEM inline, or…
GITHUB_APP_PRIVATE_KEY_PATH=       # …a path to the PEM file
GITHUB_API_URL=https://api.github.com
GITHUB_WEBHOOK_SECRET=             # HMAC secret for /webhooks/github
CODEGEN_MODEL=claude-opus-4-8      # editor model — any LiteLLM id
ANTHROPIC_API_KEY=                 # provider key matching CODEGEN_MODEL
                                   #   (or OPENAI_API_KEY / GOOGLE_API_KEY / …)
CODEGEN_KILL_SWITCH=               # "true" halts all changeset jobs
CODEGEN_DISABLED_PROJECTS=         # comma-separated per-project denylist
```

Optional editor tunables: `CODEGEN_AIDER_BIN` (default `aider`), `CODEGEN_WORKDIR`
(throwaway-clone base), and the `CODEGEN_TIMEOUT` / `CODEGEN_TEST_TIMEOUT` /
`CODEGEN_GIT_TIMEOUT` second caps. A repo's test command comes from the connection
`policy.test_cmd`; if unset, the editor auto-detects it (pytest / npm / make / …).

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
4. **Enable branch protection** on the default branch (require PR + green checks)
   as the server-side merge backstop.

Flow: an approved feature proposal enqueues a `code_implementation` run (agents
service) → `POST /v1/changesets` → the job mints a repo token, runs the Aider
editor (implement until tests pass) in a sandboxed clone, runs pre-push gates,
pushes a branch, and opens a **draft PR** → the repo's CI runs →
the webhook advances the changeset to `ci_passed` and promotes the PR to
ready-for-review → merge is gated on green CI + autonomy level.
