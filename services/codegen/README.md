# Codegen Service

FastAPI service (`:8084`, private service network only) — the
autonomous-development "hands" of APDL. It works only in operator-granted
customer repositories and produces **changesets** (branch + commits + pull
request). GitHub is the sole authority for CI verification, review rules, and
merge. APDL observes those results and may push bounded repair commits.

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
and orchestration revision may publish through evaluated rollout stages. APDL
creates PRs and bounded same-branch repairs; GitHub owns CI, review policy, and
merge.

Publication is fail-closed. Offline and shadow deployments have no PR
publication capability. Reviewed and low-risk-canary deployments must load an
operator-controlled evaluation bundle for the exact `CODEGEN_MODEL` and
`CODEGEN_REVISION`; the decision is persisted before any GitHub write token is
minted and is read-only in Admin. The explicit `make dev-all` development
overlay is a separate capability: it uses `development_pr`, records a distinct
local-development authorization, and can open draft PRs only. It does not claim
evaluation evidence and cannot authorize a production rollout. Any real model
path still needs the packaged `aider` worker, a provider key, GitHub App
credentials, and an active operator-verified repository grant; evaluated stages
additionally require the operator-generated rollout bundle.

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

All `/v1` endpoints require the canonical `X-API-Key`. Codegen derives the
project and roles from PostgreSQL and independently checks every body, query,
path, and changeset-owned project. There is no permissive or global internal
bearer token. A project-scoped credential is not repository authority: only an
active operator-verified grant can authorize GitHub access.

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/connections/{project_id}` | Read the active grant projection (`grant_id`, immutable `repository_id`, display-only `repository_full_name`) |
| GET | `/v1/connections/{project_id}/tenant-policy` | Read the strict tenant-owned Codegen preferences |
| PUT | `/v1/connections/{project_id}/tenant-policy` | Replace tenant preferences (tightening only) |
| GET | `/v1/connections/{project_id}/repo-context` | Strict canonical `repo_profile@1` for planning agents |
| POST | `/v1/changesets` | Enqueue a changeset during a PR publication stage |
| GET | `/v1/changesets?project_id=…` | List a project's changesets |
| GET | `/v1/changesets/{id}` | Fetch one changeset |
| GET | `/v1/changesets/{id}/observations` | Read append-only GitHub PR/CI and repair observations |
| GET | `/v1/changesets/{id}/runtime-observations` | Read exact-head GitHub Actions logs/artifact evidence |
| POST | `/v1/changesets/{id}/abandon` | Abandon queued pre-PR work |
| POST | `/webhooks/github` | HMAC-verified recovery trigger (`pull_request`, `check_run`, `check_suite`, `status`) |
| GET | `/health`, `/ready` | Liveness / PostgreSQL readiness |

`/webhooks/github` is the only route outside the project API-key boundary. It
always requires `X-Hub-Signature-256` verified with `GITHUB_WEBHOOK_SECRET`.
When the secret is unset, the endpoint returns `503` before parsing or queuing
the payload; the service, health checks, and CI poller continue to operate.

## Repository authority

Repository onboarding is an operator-only control-plane operation. Tenant API
keys and Admin browser sessions cannot enumerate repositories reachable by the
shared GitHub App, submit an installation ID, or activate a grant. The operator
verifies the exact GitHub repository and records one canonical grant containing:

- the APDL `project_id`;
- the internal GitHub App `installation_id`;
- GitHub's immutable numeric `repository_id`;
- `repository_full_name`, retained only as a display and clone locator;
- grant status and operator audit metadata.

The tenant API has no connection-creation route. The public read-only connection
contract contains `grant_id`, `repository_id`, and `repository_full_name`; it
never exposes `installation_id` and never treats the repository name as
authority. Repository renames may update the display name only when the numeric
ID is unchanged. A transfer, deletion, installation change, revocation, or ID
mismatch fails closed and requires operator reauthorization.

Every changeset snapshots its grant and immutable repository target. Before a
clone, push, PR mutation, poll, or repair, Codegen checks that the snapshot still
belongs to the project and that its grant is active. Installation tokens are
minted for exactly that repository ID with an operation-specific permission
set; a token response that does not match the requested repository and
permissions is rejected. Rebinding a project therefore cannot retarget queued
or open work.

The operator workflow is intentionally local to the trusted Codegen control
plane rather than exposed as a tenant HTTP endpoint. From the repository root,
run:

```bash
make grant-codegen-repository \
  ARGS='--project-id demo --repository owner/name --authorized-by operator@example.com'
```

That command performs the complete trusted binding workflow:

1. verify that the GitHub App installation includes the intended repository;
2. resolve and record the immutable repository ID under the intended APDL
   project;
3. activate the audited grant and bind the project to it.

Before removing or transferring a repository, revoke its exact active grant:

```bash
make revoke-codegen-repository \
  ARGS='--project-id demo --grant-id ghg_id-returned-by-the-grant-command'
```

Revocation is terminal and immediately blocks new token leases, repo-context
reads, CI recovery, repairs, and PR creation for that grant. Codegen attempts to
revoke each leased GitHub token when an operation exits; cleanup failures are
logged without erasing a push or PR GitHub already accepted, and the exact-repo
token remains bounded by GitHub's issued expiry. If immediate cutoff is needed
while an editor operation is already in flight, suspend or uninstall the GitHub
App installation on GitHub as well; GitHub controls the validity of a token it
already issued.

Existing legacy repository/installation rows are not proof of ownership and
must not be automatically promoted to active grants.

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
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY=            # PEM inline (escaped \n accepted), or…
GITHUB_APP_PRIVATE_KEY_BASE64=     # …base64 of the .pem (easiest in Docker), or…
GITHUB_APP_PRIVATE_KEY_PATH=       # …a path to the .pem file (~ expanded)
GITHUB_API_URL=https://api.github.com
GITHUB_WEBHOOK_SECRET=             # required to enable /webhooks/github; empty returns 503
CODEGEN_MODEL=claude-opus-4-8      # editor model — any LiteLLM id
CODEGEN_REVISION=                  # immutable candidate/deployment digest
CODEGEN_ROLLOUT_STAGE=offline      # offline | shadow | reviewed_pr | low_risk_canary
                                   # development_pr is owned only by the dev-all overlay
CODEGEN_DEVELOPMENT_MODE=          # overlay-owned marker; never enable in production
CODEGEN_ROLLOUT_AUTHORIZATION_PATH= # read-only bundle for evaluated PR stages; blank in development_pr
CODEGEN_PLATFORM_SAFETY_POLICY_PATH= # absolute path to operator safety-policy JSON
CODEGEN_SANDBOX=docker             # fail-closed isolated-worker default
CODEGEN_SANDBOX_NETWORK=           # required filtered network for evaluated PR stages
CODEGEN_TRUSTED_REPOS_ONLY=false   # explicit opt-in for local in-process mode
CODEGEN_JOB_BUDGET=3000            # optional lower cap; cannot exceed 50 minutes
ANTHROPIC_API_KEY=                 # provider key matching CODEGEN_MODEL
                                   #   (or OPENAI_API_KEY / GOOGLE_API_KEY / …)
CODEGEN_KILL_SWITCH=               # "true" halts all changeset jobs
CODEGEN_DISABLED_PROJECTS=         # comma-separated per-project denylist
```

Optional editor tunables: `CODEGEN_AIDER_BIN` (default `aider`), `CODEGEN_WORKDIR`
(throwaway-clone base), and the `CODEGEN_TIMEOUT` /
`CODEGEN_GIT_TIMEOUT` second caps. A whole job (clone + retry rounds + push) is
bounded by `codegen_job_budget()`, which also caps the sandbox container and the orphan-sweep
deadline. The derived budget is hard-capped at 3000 seconds so the
credential-bearing container ends with at least a five-minute token-expiry
margin; `CODEGEN_JOB_BUDGET` may lower but cannot raise that cap. Timeout or
shutdown cleanup force-removes the named container before the token lease
exits; an unverifiable removal is retried, logged as critical, and fails the
editor operation. A repo's verification command comes from connection
`tenant_policy.test_cmd`; if unset,
the editor auto-detects it (pytest / npm / make / …) and gives it to the model as
test-generation guidance. APDL does not execute it authoritatively; GitHub CI does.
The pre-push gates run inside the editor on the full diff (a violating branch
never reaches GitHub), and the job runner re-checks the same resolved policy as
a backstop before opening the PR. Orphan recovery: queued changesets are re-enqueued at
startup (the queued → cloning transition is the dedup claim); active-state
orphans are swept to `error` at startup and every `CODEGEN_STALE_SWEEP_INTERVAL`
(default 300s) once older than twice the job budget.

Tenant connection preferences use one strict, versioned contract. Unknown
fields are rejected. Tenant limits can only lower the operator limits, and
tenant protected paths are added to—never substituted for—the built-in and
operator-owned protections. Each additional-path list is capped at 64 entries:

```json
{
  "schema_version": "tenant_codegen_connection_policy@1",
  "test_cmd": null,
  "gates": {
    "max_files": null,
    "max_lines": null,
    "additional_protected_paths": []
  },
  "runtime_acceptance": {
    "schema_version": "runtime_acceptance_request@1",
    "enabled": false
  }
}
```

The operator may mount a strict `platform_codegen_safety_policy@1` JSON file and
set its absolute path with `CODEGEN_PLATFORM_SAFETY_POLICY_PATH`. The built-in
defaults are 50 files, 2,000 changed lines, protected workflow/key/environment
paths, and runtime workflow generation disabled. The effective limits use
`min(operator, tenant)` and protected paths use a union. Each changeset snapshots
the tenant policy and records the effective-policy SHA-256 before GitHub
credentials are minted, so later connection edits cannot change an in-flight
job's safety boundary.

Runtime workflow generation requires both the operator capability and the
tenant request. Its only exemptible path is the fixed
`.github/workflows/apdl-runtime-acceptance.yml`; the editor refuses to overwrite
non-APDL-owned content there. Other workflow edits remain protected. GitHub
executes the generated job and owns its result; absent runs, logs, or required
artifacts are stored as unverified evidence, never as successful CI.

## Local full-stack development (`development_pr`)

Use either canonical command from the repository root:

```bash
make dev-all
# Equivalent wrapper:
scripts/dev.sh up-full
```

Both commands run the base Compose file plus
`infra/docker/docker-compose.codegen-development.yml`. The preparation step
requires a local Docker context backed by a Unix socket, builds
`Dockerfile.worker` as `apdl-codegen-sandbox:local-development`, verifies the
socket and socket-group access, and creates the labeled
`apdl-codegen-development` bridge. The overlay mounts that socket into Codegen,
sets the fixed `local-development` revision and
`CODEGEN_DEVELOPMENT_MODE=true`, selects `development_pr`, and leaves the
evaluated authorization-bundle path empty. Codegen then preflights the daemon,
worker revision label, and named network before reporting ready.

`development_pr` is deliberately narrow. Its strict
`development_publication_authorization@1` may publish a branch and create a pull
request, but `draft_only` is always true and `ready_for_review` is always false.
It contains no corpus result, evaluation report, rollout bundle, evaluated image
identity, or production authority. Migration
`011_codegen_development_publication.sql` admits this distinct development
authorization alongside `publication_authorization@2`; it does not reinterpret
development authorization as evaluated evidence or loosen either schema.

Starting the stack provisions the worker but does not provision external
authority. An actual changeset also requires:

- the model-provider credential matching `CODEGEN_MODEL`;
- `GITHUB_APP_ID` and a GitHub App private key available inside Codegen (base64
  is the simplest Compose setting);
- installation of that App on the target repository; and
- an active exact-repository grant created by a trusted operator with
  `make grant-codegen-repository`.

This overlay is local-development infrastructure, not a production template.
The Docker socket gives the Codegen controller host-level container authority,
and `apdl-codegen-development` is intentionally not egress-filtered. The base
Compose service remains `offline`. For evaluated publication, do not reuse the
development image, network, marker, or authorization; follow
[Deploy the evaluated candidate](#deploy-the-evaluated-candidate) with
`reviewed_pr` and the evaluated rollout overlay.

## Evaluation and publication rollout

The evaluation corpus covers Node, Python, Go, Rust, JVM, and .NET repositories
with digest-bound synthetic defects. A sealed controller owns the corpus,
mutation labels, harness, rollout policy, and oracles. It launches the exact
production sandbox image once per case through Docker. That candidate image
contains the real Aider editor but deliberately excludes the sealed corpus,
fixtures, and oracle files; it receives only an opaque invocation identity,
public task, isolated mutated workspace, behavior configuration, and matching
model-provider credential. Neither side receives GitHub, PostgreSQL, APDL, or
SSH credentials during evaluation.

Rollout stages are strict capabilities:

1. `offline` runs the fixture corpus and is the service default; changeset
   publication endpoints are disabled.
2. `shadow` runs generation without branch or PR capability.
3. `reviewed_pr` requires a valid operator bundle and always opens a draft PR.
4. `low_risk_canary` is reserved for promotion evidence from reviewed PRs.
   `rollout_policy@3` deliberately denies it until real GitHub CI/review
   observations are represented by a later policy contract.

`development_pr` is not a step in this evaluated progression. It exists only in
the explicit local-development overlay described above and cannot produce an
evaluation bundle or satisfy `reviewed_pr` authorization.

### Run the real candidate evaluation

Run the operator workflow from the repository root. Export only the provider
credential matching the selected model; do not pass the repository `.env` into
the evaluation container:

```bash
export CODEGEN_MODEL=claude-opus-4-8
export ANTHROPIC_API_KEY=... # use your secret manager or current shell

# Optional. Defaults to the current Git commit only for a clean worktree. A
# dirty tree must be committed or given a distinct tag-safe revision.
export CODEGEN_REVISION="$(git rev-parse HEAD)"

# Optional but recommended: a named network with operator-enforced egress rules.
export CODEGEN_EVALUATION_NETWORK=codegen-egress-filtered

make evaluate-codegen
```

This command:

1. builds the API image as the sealed evaluation controller;
2. builds `Dockerfile.worker` as the production candidate, labels it with the
   exact revision, and verifies that it contains no corpus/oracle assets;
3. resolves both images to immutable local `sha256:...` image IDs and uses
   those IDs for the evaluation itself;
4. gives only the controller the Docker socket and a host/container same-path
   temporary bind, so sibling candidate mounts resolve through the host daemon;
5. forwards only an explicit model-provider and behavior-setting allowlist; and
6. evaluates and builds the publication bundle in one trusted invocation using
   the checked-in strict `rollout_policy_v3.json`.

The run and bundle content-address the exact controller image ID, candidate
image ID, revision, and normalized non-secret behavior configuration. Provider
credentials are excluded, but model/helper routing, Aider path, prompt/review
toggles, retries, contract settings, timeouts, and provider endpoints are bound.
Changing any bound value makes the service reject the old bundle at startup.

It never builds publication authority from `--results`. Existing result files
may be inspected or re-aggregated, but only a just-completed trusted Docker run
may emit `publication-bundle.json`.

Artifacts default to
`local-files/codegen-rollouts/$CODEGEN_REVISION/` (already ignored by Git):

```text
controller-image-id.txt
candidate-image-id.txt
rollout-policy.json
evaluation-run.json
evaluation-report.json
evaluation-segments.json
publication-bundle.json
```

Override that absolute destination with `CODEGEN_EVALUATION_ARTIFACT_DIR`. The
controller image retains sealed evaluation material, so treat it as an operator
artifact. The production candidate image is the only image that may run model
work against a fixture or customer repository.

`CODEGEN_REVISION` names the evaluated orchestration candidate; the strict
candidate identity additionally binds the exact images and effective behavior.
Use a new revision whenever source or behavior changes so artifacts remain
separate and auditable. Even if an old revision is reused accidentally, the
identity digest prevents its bundle from authorizing different behavior.

The checked-in `rollout_policy@3` gates reviewed draft-PR publication only on
metrics the sealed offline harness can honestly measure. GitHub CI, human
review, merge, revert, and post-merge outcomes remain unavailable during this
run and are never fabricated. Those external observations are required for
later canary/expansion decisions, not for opening a mandatory-review draft.

### Deploy the evaluated candidate

Review the report and bundle, provision a genuinely egress-filtered Docker
network, keep the model-provider and behavior variables identical to the
evaluated values, then use the rollout Compose overlay:

```bash
export CODEGEN_SANDBOX_NETWORK=codegen-egress-filtered
make migrate-postgres
make codegen-reviewed-config
make codegen-reviewed-up
```

The Make target reads both image-ID files, verifies their revision and role
labels, mounts `publication-bundle.json` read-only at
`/run/apdl/codegen/publication-bundle.json`, and recreates Codegen with
`reviewed_pr` without rebuilding or pulling. The exact evaluated controller ID
runs the API and the exact evaluated candidate ID runs each production sandbox.
The overlay derives the current user's UID/GID and Docker socket group, which
supports both Linux Docker hosts and Docker Desktop's user-owned socket.

Migration `010_codegen_publication_identity.sql` archives any persisted v1
authorization JSON and clears it from the active authority column; it never
fabricates the image/config identity that old rows did not record. New writes
are constrained to the strict v2 authorization contract.

The overlay mounts a Docker control socket into the credential-bearing Codegen
API. That is sufficient for a single-operator self-hosted machine but grants
host-root-equivalent container authority. Production deployments should replace
it with a dedicated rootless or policy-constrained worker launcher. Merely
naming a bridge network `codegen-egress-filtered` does not filter traffic: the
operator must enforce the allowlist and block APDL/private/metadata destinations.

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

- **Sandboxed container (`CODEGEN_SANDBOX=docker`, the default)** —
  `ContainerAiderEditor`
  runs each changeset in an ephemeral container from `Dockerfile.worker`
  (read-only root, no-exec tmpfs workspaces, `--cap-drop ALL`,
  `no-new-privileges`, pid/memory/cpu caps, non-root),
  reusing the same `AiderEditor` inside it. Untrusted code never touches the API
  container's secrets; the sandbox only gets the short-lived install token (which
  the runner drops from the env before Aider starts) and the model key. Aider is
  pinned, receives service-owned empty config/env files, and cannot auto-run
  repository lint, test, shell-suggestion, hook, URL, or browser commands.
- **Trusted local in-process (`CODEGEN_SANDBOX=in-process`)** — available only
  with `CODEGEN_TRUSTED_REPOS_ONLY=true` while the rollout is `offline` or
  `shadow`. The service refuses this mode for every PR publication stage.

Enable the sandbox:

```bash
make build-codegen-sandbox        # revision-labeled production candidate
export CODEGEN_SANDBOX=docker
export CODEGEN_SANDBOX_NETWORK=codegen-egress-filtered
# For Compose deployment use docker-compose.codegen-rollout.yml via
# `make codegen-reviewed-up`; it mounts the host's explicit Docker socket path.
```

Every PR stage fails startup unless `CODEGEN_SANDBOX_NETWORK` is a non-default
named network. The local `development_pr` overlay creates an explicitly
development-only bridge that is not egress-filtered. For evaluated PR stages,
the operator must enforce a real egress policy: allow only the required
GitHub/model endpoints and block APDL/private CIDRs plus `169.254.169.254`.
Tunables: `CODEGEN_SANDBOX_IMAGE`, `CODEGEN_SANDBOX_MEMORY`,
`CODEGEN_SANDBOX_CPUS`, `CODEGEN_SANDBOX_PIDS`, `CODEGEN_DOCKER_BIN`. Mounting a
Docker socket still grants the API process host-level Docker authority; deploy
the API and worker launcher on a dedicated host or use a remote worker boundary.

## Going live (end-to-end)

The autonomous loop runs once these external pieces are set up:

1. **Register a GitHub App** (org-level) with minimal permissions — `contents:
   write`, `pull_requests: write`, `checks: read`, `actions: read`, `metadata:
   read`, and `statuses: read`. `actions: read` is required only to collect exact-head workflow jobs,
   bounded failure logs, and runtime artifacts; it does not let APDL approve CI
   or merge. Existing installations must approve the added permission before
   runtime evidence can be collected; until then it remains explicitly
   unverified. Set
   `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY`. Customers install it on their
   repos; a trusted operator then binds each exact repository with
   `make grant-codegen-repository` as described in
   [Repository authority](#repository-authority). Never provision a repository
   through a tenant API key or an unverified installation ID.
2. **Provision and evaluate the coding agent.** Make `aider` available where
   the editor runs
   — `uv pip install -e ".[agent]"` on the codegen host for v1, or build the
   hardened sandbox image (`Dockerfile.worker`) to run one changeset per
   container. Set `CODEGEN_MODEL` and the matching provider key (e.g.
   `ANTHROPIC_API_KEY`). Run the offline/shadow corpus for the exact model and
   immutable `CODEGEN_REVISION`, review the report, and mount the resulting
   operator bundle before selecting a PR rollout stage. Optionally set each
   repo's test command through connection `tenant_policy.test_cmd` (otherwise it is
   auto-detected).
3. **Add a repo webhook** → configure a non-empty `GITHUB_WEBHOOK_SECRET`, then
   point GitHub at `POST /webhooks/github` with events `pull_request`,
   `check_run`, `check_suite`, and `status`. An unset secret disables the
   endpoint; polling remains the recovery path for disabled or missed deliveries.
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
