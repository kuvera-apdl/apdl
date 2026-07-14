# Codegen Service

FastAPI service (`:8084`) — the autonomous-development "hands" of APDL. It
connects to customer repositories and produces **changesets** (branch + commits
+ pull request). GitHub is the sole authority for CI verification, review rules,
and merge. APDL observes those results and may push bounded repair commits.

Orchestration, autonomy gating, safety validation, and human approvals live in
the **agents service** (`:8083`), which calls this service over the internal
API. See
[`docs/plans/generalized-codegen-service-improvement-plan.md`](../../docs/plans/generalized-codegen-service-improvement-plan.md)
for the full design and phase plan.

## Status

The generalized Phase 0–9 pipeline is implemented. A strict repository profile,
exact-version contract evidence, requirement ledger, bounded inspection slices,
risk-based verification plan, semantic review, and GitHub runtime evidence feed
one model-agnostic Aider editor. Continuous evaluation gates which exact model
and orchestration revision may publish. APDL creates PRs and bounded same-branch
repairs; GitHub owns CI, review policy, and merge.

Publication is fail-closed. Offline and shadow deployments have no PR
publication capability. Reviewed and low-risk-canary deployments must load an
operator-controlled evaluation bundle for the exact `CODEGEN_MODEL` and
`CODEGEN_REVISION`; the decision is persisted before any GitHub write token is
minted and is read-only in Admin. The real model path still needs `aider`, a
provider key, a connected repository, and an operator-generated rollout
bundle before it can publish.

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
| POST | `/v1/changesets` | Enqueue a changeset during a PR publication stage |
| GET | `/v1/changesets?project_id=…` | List a project's changesets |
| GET | `/v1/changesets/{id}` | Fetch one changeset |
| GET | `/v1/changesets/{id}/observations` | Read append-only GitHub PR/CI and repair observations |
| GET | `/v1/changesets/{id}/runtime-observations` | Read exact-head GitHub Actions logs/artifact evidence |
| POST | `/v1/changesets/{id}/abandon` | Abandon queued pre-PR work |
| POST | `/webhooks/github` | HMAC-verified recovery trigger (`pull_request`, `check_run`, `check_suite`, `status`) |
| GET | `/health`, `/ready` | Liveness / PostgreSQL readiness |

## Changeset lifecycle

```text
changeset_status: queued → cloning → editing → pushing → pr_open
                                                        ├─GitHub merge──→ merged
                                                        └─GitHub close──→ abandoned

external_ci_status: pending | passed | failed | unverified_external_ci
ci_remediation_status: idle | diagnosing | repairing | awaiting_ci | resolved | exhausted
github_pr_status: draft | open | merged | closed
```

CI is not a changeset lifecycle state. A repository with no configured CI stays
an observable open PR and settles as `unverified_external_ci`; it is never
reported as passed and never remains in an indefinite `ci_running` state.

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
CODEGEN_REVISION=                  # immutable candidate/deployment digest
CODEGEN_ROLLOUT_STAGE=offline      # offline | shadow | reviewed_pr | low_risk_canary
CODEGEN_ROLLOUT_AUTHORIZATION_PATH= # read-only operator JSON bundle for PR stages
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

Runtime workflow generation is opt-in per repository. The only policy shape is:

```json
{
  "runtime_acceptance": {
    "schema_version": "runtime_acceptance_policy@1",
    "workflow_changes_authorized": true,
    "generated_workflow_path": ".github/workflows/apdl-runtime-acceptance.yml"
  }
}
```

When authorized, only that exact workflow path is exempted from the general
protected-path gate. Other workflow edits remain protected. GitHub executes the
generated job and owns its result; absent runs, logs, or required artifacts are
stored as unverified evidence, never as successful CI.

## Evaluation and publication rollout

The evaluation corpus covers Node, Python, Go, Rust, JVM, and .NET repositories
with digest-bound synthetic defects. Executor invocations receive only an
opaque invocation identity, public task, and isolated mutated workspace;
evaluator oracles, mutation labels, GitHub credentials, and publication controls
remain outside the executor boundary. Metrics are finite, evidence-backed, and
retain explicit numerators, denominators, exclusions, and
model/ecosystem/task/risk slices.

Rollout stages are strict capabilities:

1. `offline` runs the fixture corpus and is the service default; changeset
   publication endpoints are disabled.
2. `shadow` runs generation without branch or PR capability.
3. `reviewed_pr` requires a valid operator bundle and always opens a draft PR.
4. `low_risk_canary` additionally requires stable cohort eligibility and may
   mark only an otherwise verified low-risk PR ready for review.

Use `python -m app.evaluations.cli --help` from `services/codegen` to validate a
corpus, execute a credential-scrubbed offline/shadow evaluator command, aggregate
a content-addressed report, and build a rollout bundle from an explicit strict
policy. Run that command in a separate credential-minimal worker/container: it
may retain the selected model-provider key, but it must not receive the GitHub
App key, installation tokens, internal service token, database URL, or SSH agent.
Mount the resulting bundle read-only and set its path through
`CODEGEN_ROLLOUT_AUTHORIZATION_PATH`.

`CODEGEN_REVISION` identifies the complete evaluated orchestration candidate,
not merely a source commit. Change it whenever prompts, helper-model routing,
contract/review toggles, retry policy, or any other behavior-affecting deployment
setting changes; otherwise the deployment no longer matches its evaluation.

```bash
apdl-codegen-eval \
  --executor /opt/apdl/bin/evaluate-candidate \
  --model "$CODEGEN_MODEL" \
  --codegen-revision "$CODEGEN_REVISION" \
  --run-output /artifacts/evaluation-run.json \
  --report-output /artifacts/evaluation-report.json \
  --segmented-output /artifacts/evaluation-segments.json

apdl-codegen-eval \
  --results /artifacts/evaluation-run.json \
  --rollout-policy /artifacts/rollout-policy.json \
  --bundle-output /artifacts/publication-bundle.json
```

The executor is an argv path, not a shell command. It receives one strict public
invocation on stdin, edits the current fixture workspace, and must return one
strict `evaluation_execution@2` JSON object on stdout. Use repeated
`--executor-arg` options for arguments.

The included corpus is intentionally smaller than the default production
minimum sample size. It cannot silently unlock PR publication: expand the corpus
or create an explicit operator policy with justified migration thresholds.

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
   write`, `pull_requests: write`, `checks: read`, `actions: read`, `metadata:
   read`. `actions: read` is required only to collect exact-head workflow jobs,
   bounded failure logs, and runtime artifacts; it does not let APDL approve CI
   or merge. Existing installations must approve the added permission before
   runtime evidence can be collected; until then it remains explicitly
   unverified. Set
   `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY`. Customers install it on their
   repos; record each installation via `POST /v1/connections`.
2. **Provision and evaluate the coding agent.** Make `aider` available where
   the editor runs
   — `uv pip install -e ".[agent]"` on the codegen host for v1, or build the
   hardened sandbox image (`Dockerfile.worker`) to run one changeset per
   container. Set `CODEGEN_MODEL` and the matching provider key (e.g.
   `ANTHROPIC_API_KEY`). Run the offline/shadow corpus for the exact model and
   immutable `CODEGEN_REVISION`, review the report, and mount the resulting
   operator bundle before selecting a PR rollout stage. Optionally set each
   repo's test command through connection `policy.test_cmd` (otherwise it is
   auto-detected).
3. **Add a repo webhook** → `POST /webhooks/github`, secret
   `GITHUB_WEBHOOK_SECRET`, events `pull_request`, `check_run`, `check_suite`,
   and `status`. Polling remains the recovery path for missed deliveries.
4. **Enable GitHub branch protection/rulesets** on the default branch (require PR,
   reviews, and green checks). GitHub is the enforcement and merge authority.

Flow: an approved feature proposal enqueues a `code_implementation` run (agents
service) → `POST /v1/changesets` → the job recomputes and persists the rollout
decision → only an allowed decision permits minting a repo token → the Aider
editor in a sandboxed clone, runs deterministic pre-push gates,
pushes a branch, and opens a PR (draft when policy or evidence requires it) →
the repo's CI runs → the webhook or poller records GitHub's exact-head external
CI status and feeds bounded logs/artifacts into same-branch repair → GitHub
reviews/rulesets decide readiness and merge.
