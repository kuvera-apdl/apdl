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
minted and is read-only in Admin. The OSS developer-preview commands do not
enable any publishing stage: `make dev-all` opts into Codegen only in `offline`
mode, without a Docker socket or branch/PR authority. Publication tooling in the
source tree is experimental operator infrastructure and is outside the supported
release surface.

The 0.3.0 dependency gate covers only the offline API/control-plane dependency
set used by `make dev-all`. It does **not** cover or support the Aider editor,
the `.[agent]` optional dependencies, `Dockerfile.worker`, sandbox execution,
or any publication overlay. Those experimental paths are retained as source
for future hardening and must not be exposed to tenants or treated as a
release-qualified runtime.

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
                                   # release Compose commands force offline
CODEGEN_DEVELOPMENT_MODE=          # experimental internal marker; leave unset
CODEGEN_ROLLOUT_AUTHORIZATION_PATH= # read-only bundle for experimental evaluated stages
CODEGEN_PLATFORM_SAFETY_POLICY_PATH= # absolute path to operator safety-policy JSON
CODEGEN_SANDBOX=docker             # fail-closed isolated-worker default
CODEGEN_SANDBOX_NETWORK=           # development_pr only; evaluated stages require empty
CODEGEN_EGRESS_POLICY_SHA256=      # digest of checked-in proxy policy sources
CODEGEN_EGRESS_PROXY_IMAGE_ID=     # exact evaluated proxy image ID
CODEGEN_EGRESS_SOCKET_VOLUME=      # controller-owned proxy Unix-socket volume
CODEGEN_EGRESS_PROXY_URL=http://127.0.0.1:3128
CODEGEN_TRUSTED_REPOS_ONLY=false   # explicit opt-in for local in-process mode
CODEGEN_JOB_BUDGET=3000            # optional lower cap; cannot exceed 50 minutes
ANTHROPIC_API_KEY=                 # provider key matching CODEGEN_MODEL
                                   #   (or OPENAI_API_KEY / GOOGLE_API_KEY / …)
CODEGEN_KILL_SWITCH=               # "true" halts all changeset jobs
CODEGEN_DISABLED_PROJECTS=         # comma-separated per-project denylist
```

Optional editor tunables: `CODEGEN_AIDER_BIN` (default `aider`), `CODEGEN_WORKDIR`
(throwaway-clone base), and the `CODEGEN_TIMEOUT` /
`CODEGEN_GIT_TIMEOUT` second caps. A whole job (clone + retry rounds + publication) is
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
startup (the queued → cloning transition is the dedup claim). A `pushing`
changeset with an append-only publication intent is resumed by its deterministic
APDL branch before any PR create retry; raw accepted PR identities remain
journaled for validation and cleanup. Other active-state orphans are swept to
`error` at startup and every `CODEGEN_STALE_SWEEP_INTERVAL` (default 300s) once
older than twice the job budget.

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

## Local full-stack development (non-publishing)

Use either canonical command from the repository root:

```bash
make dev-all
# Equivalent wrapper:
scripts/dev.sh up-full
```

Both commands use the base Compose service with the explicit `codegen` profile.
The base service forces `CODEGEN_ROLLOUT_STAGE=offline`, clears the publication
authorization path, exposes no host port, and mounts no Docker socket. It can be
inspected by operators but cannot create a branch or pull request.

The repository retains evaluation and publication components for continued
development, but no supported OSS release command enables them. Do not treat
the development or evaluated overlays as deployment templates for this release.

## Experimental evaluation and publication tooling

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
   `rollout_policy@4` deliberately denies it until real GitHub CI/review
   observations are represented by a later policy contract.

`development_pr` is not a step in this evaluated progression. It remains an
internal development capability and cannot produce an evaluation bundle or
satisfy `reviewed_pr` authorization. It is not enabled by `make dev-core` or
`make dev-all` and is outside the OSS developer-preview support boundary.

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

# Optional name override. Otherwise the workflow creates a unique temporary
# controller-owned proxy-socket volume and removes it after evaluation.
export CODEGEN_EVALUATION_SOCKET_VOLUME=apdl-codegen-evaluation-egress

make evaluate-codegen
```

This command:

1. builds the API image as the sealed evaluation controller;
2. builds `Dockerfile.worker` as the production candidate, labels it with the
   exact revision, and verifies that it contains no corpus/oracle assets;
3. builds the checked-in Squid policy, exports it only through an attested Unix
   socket volume, and actively probes metadata/private/direct bypasses from the
   immutable controller image;
4. resolves all three images to immutable local `sha256:...` image IDs and uses
   those IDs for the evaluation itself;
5. gives only the controller the Docker socket and a host/container same-path
   temporary bind, so sibling candidate mounts resolve through the host daemon;
6. forwards only an explicit model-provider and behavior-setting allowlist; and
7. evaluates and builds the publication bundle in one trusted invocation using
   the checked-in strict `rollout_policy_v4.json`.

Every candidate container runs with Docker `--network none`, mounts the proxy
socket volume read-only, and starts a sealed loopback TCP-to-Unix relay so
standard HTTP proxy clients continue to work. The run persists controller-made,
launch-ID-bound attestation digests for every measured case. The run and bundle
content-address the exact controller image ID, candidate image ID, proxy image
ID, network-none socket transport, reviewed concurrency of one, egress-policy
digest, revision, and normalized non-secret behavior configuration. Provider
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
egress-proxy-image-id.txt
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

The checked-in `rollout_policy@4` gates reviewed draft-PR publication on the
overall metrics and on every risk, ecosystem, and task-type segment produced by
the sealed offline harness. Each segment must meet its minimum sample and
eligible escaped-defect denominator, with zero escaped defects. The current
eight-case corpus intentionally cannot satisfy the default two-sample segment
floor for every slice, so operators must expand the sealed corpus before
reviewed publication can be authorized. GitHub CI, human review, merge, revert,
and post-merge outcomes remain unavailable during this run and are never
fabricated. Those external observations are required for later
canary/expansion decisions.

### Deploy the evaluated candidate

Review the report and bundle, keep the model-provider and behavior variables
identical to the evaluated values, then use the shipped egress and rollout
Compose overlays:

```bash
unset CODEGEN_SANDBOX_NETWORK
# Optional; Make otherwise derives a policy-addressed production volume name.
export CODEGEN_EGRESS_SOCKET_VOLUME=apdl-codegen-reviewed-egress
make migrate-postgres
make codegen-reviewed-config
make codegen-reviewed-up
```

The Make target reads all three image-ID files, verifies revision, role, and
egress-policy labels, mounts `publication-bundle.json` read-only at
`/run/apdl/codegen/publication-bundle.json`, and recreates Codegen with
`reviewed_pr` without rebuilding or pulling. The exact evaluated controller ID
runs the API and the exact evaluated candidate ID runs each production sandbox.
The exact evaluated proxy ID attaches only to the Compose uplink and exports
Squid through the controller-owned Unix socket volume. Workers have no Docker
network, mount that volume read-only, and can reach only the loopback relay.
Startup and pre-launch attestation verifies the exact proxy image, entrypoint,
command, image-defined healthcheck, non-root user, read-only root,
privilege/capability/security settings, tmpfs and socket mounts, volume labels,
absence of host-published ports, and the public uplink. Before and after each
active probe, the proxy must be the socket volume's only running consumer. The
immutable controller probe verifies absolute-form HTTP denials for metadata and
private port-80 URLs, CONNECT denials, an allowed public CONNECT control, and
direct public, private/metadata, and external-DNS isolation. Refusal or reset is
treated as reachability failure, not as proof of blocking.
The overlay derives the current user's UID/GID and Docker socket group, which
supports both Linux Docker hosts and Docker Desktop's user-owned socket.

Migration `026_codegen_egress_publication.sql` archives active evaluated
authorization JSON that predates egress attestation. New evaluated writes are
constrained to `publication_authorization@4` /
`publication_request@3`, where the requested and expected egress-policy digests
must be canonical and equal.

The overlay mounts a Docker control socket into the credential-bearing Codegen
API. That is sufficient for a single-operator self-hosted machine but grants
host-root-equivalent container authority. Production deployments should replace
it with a dedicated rootless or policy-constrained worker launcher. Merely
naming a Docker object is not treated as proof: evaluated startup and every
worker launch revalidate the effective proxy/volume topology and run the
controller-owned active deny probe.
Destination policy lives in `infra/docker/codegen-egress/`; changing any policy
source changes the digest and invalidates prior evaluation evidence.

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

The real editor is outside the 0.3.0 release contract. Developers researching
that unsupported path may install `.[agent]`, but it is not covered by the
release vulnerability gate and must not be used as a release-qualified service.

## Editor execution model (experimental and unsupported in 0.3.0)

The editor sits behind the `Editor` interface; *how/where* it runs is config:

- **Sandboxed container (`CODEGEN_SANDBOX=docker`, the default)** —
  `ContainerAiderEditor`
  runs each changeset in two sequential ephemeral containers from
  `Dockerfile.worker`
  (read-only root, no-exec tmpfs workspaces, `--cap-drop ALL`,
  `no-new-privileges`, private PID namespaces, pid/memory/cpu caps, non-root).
  The first container receives no model-provider or write credential. It clones
  with read authority supplied over consumed stdin, exhaustively rejects
  symlinks/non-regular entries through the no-follow inspector, and returns only
  a strict repository/head/tree attestation. The second container receives the
  model key only after cloning and verifying that exact attested head and tree,
  before any repository-derived prompt input is built. The read token is also
  supplied over stdin and never enters either container's environment. Aider is
  pinned, receives service-owned empty config/env files, and cannot auto-run
  repository lint, test, shell-suggestion, hook, URL, or browser commands. The
  editor returns a patch and exact Git object identities; the controller
  reconstructs and pushes it with a short-lived contents-write token, then
  uses a separate PR-write token with no contents mutation permission for
  pull-request discovery, creation, and cleanup.

  The separate inspection container is the only process allowed to establish
  trust in an untrusted checkout. Later profiling in the editor is consumption
  of that attested tree, not a second trust decision: the editor verifies both
  `HEAD` and `HEAD^{tree}` before `_probe_repo`, brief/workflow/contract reads,
  or any model call. A moved branch or different source Git tree aborts first.
  The attested Git tree includes entry modes, so it cannot substitute a symlink;
  every later focused read still uses the component-wise no-follow inspector,
  and model-created symlinks are rejected before they can enter evidence or
  persisted prompts.
- **Trusted local in-process (`CODEGEN_SANDBOX=in-process`)** — available only
  with `CODEGEN_TRUSTED_REPOS_ONLY=true` while the rollout is `offline` or
  `shadow`. The service refuses this mode for every PR publication stage.

Enable the sandbox:

```bash
make build-codegen-sandbox        # revision-labeled production candidate
export CODEGEN_SANDBOX=docker
unset CODEGEN_SANDBOX_NETWORK
# For Compose deployment use docker-compose.codegen-rollout.yml via
# `make codegen-reviewed-up`; it mounts the host's explicit Docker socket path.
```

The local `development_pr` overlay creates an explicitly development-only bridge
that is not egress-filtered. Evaluated PR stages reject every configured sandbox
network and require `--network none`, the shipped proxy image, exact policy
digest, controller-owned socket volume, exact proxy runtime configuration, and
successful controller probes. The allowlist
covers only the checked-in GitHub, model-provider, and package-registry domains;
private, link-local, metadata, reserved, and direct non-proxy egress are denied.
The same topology and probes are re-attested immediately before every
inspection, editor, and evaluation container. Reviewed deployment also
hard-pins and content-binds `CODEGEN_MAX_CONCURRENT_JOBS=1`.
Tunables: `CODEGEN_SANDBOX_IMAGE`, `CODEGEN_SANDBOX_MEMORY`,
`CODEGEN_SANDBOX_CPUS`, `CODEGEN_SANDBOX_PIDS`, `CODEGEN_DOCKER_BIN`. Mounting a
Docker socket still grants the API process host-level Docker authority; deploy
the API and worker launcher on a dedicated host or use a remote worker boundary.

## Going live (future design; unsupported in 0.3.0)

Nothing in this section is a 0.3.0 deployment procedure. The editor/worker,
publication dependencies, sandbox, and rollout overlays are outside the
release support and vulnerability-audit boundary.

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
editor in a sandboxed clone returns a gated patch and exact tree identity → the
controller reconstructs and publishes that tree with a just-in-time write
credential, then recovers or opens one branch-bound PR (draft when policy or
evidence requires it) →
the repo's CI runs → the webhook or poller records GitHub's exact-head external
CI status and feeds bounded logs/artifacts into same-branch repair → GitHub
reviews/rulesets decide readiness and merge.
