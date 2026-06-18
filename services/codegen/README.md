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

Phases 0–6 implemented. The service connects repos (GitHub App), produces
changesets via Claude Managed Agents (self-hosted sandbox worker), runs
deterministic pre-push safety gates, opens draft PRs, ingests CI status (webhook
+ poll), and merges on green CI under the autonomy gate. The live Managed Agents
editor and self-hosted worker are **integration-untested** against the beta API —
they need an Anthropic environment key and a registered GitHub App. Remaining:
merged-change revert-PR rollback and the console changeset view.

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
ANTHROPIC_ENVIRONMENT_KEY=         # self-hosted Managed Agents worker (Phase 3)
CODEGEN_ENVIRONMENT_ID=            # the CMA self-hosted environment id
CODEGEN_KILL_SWITCH=               # "true" halts all changeset jobs
CODEGEN_DISABLED_PROJECTS=         # comma-separated per-project denylist
```

## Develop

```bash
make run-codegen         # uvicorn on :8084 (hot reload)
make run-codegen-worker  # self-hosted Managed Agents sandbox worker
make test-codegen        # pytest
make lint-codegen        # ruff
```

## Going live (end-to-end)

The autonomous loop runs once these external pieces are set up:

1. **Register a GitHub App** (org-level) with minimal permissions — `contents:
   write`, `pull_requests: write`, `checks: read`, `metadata: read`. Set
   `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY`. Customers install it on their
   repos; record each installation via `POST /v1/connections`.
2. **Create a self-hosted Managed Agents environment** and set
   `ANTHROPIC_ENVIRONMENT_KEY` + `CODEGEN_ENVIRONMENT_ID`; run the sandbox worker
   with `make run-codegen-worker` (container: `Dockerfile.worker`).
3. **Add a repo webhook** → `POST /webhooks/github`, secret
   `GITHUB_WEBHOOK_SECRET`, events `check_run` + `pull_request`.
4. **Enable branch protection** on the default branch (require PR + green checks)
   as the server-side merge backstop.

Flow: an approved feature proposal enqueues a `code_implementation` run (agents
service) → `POST /v1/changesets` → the job mints a repo token, runs the Managed
Agents editor (implement until tests pass) in the self-hosted sandbox, runs
pre-push gates, pushes a branch, and opens a **draft PR** → the repo's CI runs →
the webhook advances the changeset to `ci_passed` and promotes the PR to
ready-for-review → merge is gated on green CI + autonomy level.
