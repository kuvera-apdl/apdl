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

Phase 0–1 (foundation): service skeleton, the changeset lifecycle state machine,
the repo connection registry, and GitHub App installation-token auth. The
sandboxed job runner (clone → edit → test → push → PR), CI/merge gating, and the
agents-service integration land in later phases.

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
```

## Develop

```bash
make run-codegen     # uvicorn on :8084 (hot reload)
make test-codegen    # pytest
make lint-codegen    # ruff
```
