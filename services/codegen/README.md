# Codegen Service

FastAPI service (`:8084`, private service network only) — the
autonomous-development "hands" of APDL. It works only in customer repositories
connected to an APDL project through the GitHub App and produces **changesets**
(branch + commits + pull request). GitHub is the sole authority for CI
verification, review rules, and merge. APDL observes those results and may push
bounded repair commits.

Orchestration, autonomy gating, safety validation, and human approvals live in
the **agents service** (`:8083`), which calls this service over the internal
API. See
[`docs/plans/generalized-codegen-service-improvement-plan.md`](../../docs/plans/generalized-codegen-service-improvement-plan.md)
for the full design and phase plan.

## Status

The generalized pipeline is implemented. A strict repository profile,
exact-version contract evidence, requirement ledger, bounded inspection slices,
risk-based verification plan, semantic review, and GitHub runtime evidence feed
one model-agnostic Aider editor. APDL creates draft PRs and bounded same-branch
repairs; GitHub owns CI, review policy, and merge.

Publication is fail-closed. Offline deployments have no PR capability.
The secure `tenant_draft_pr` deployment requires immutable controller, worker,
and egress-proxy images, an active repository grant, and active project editor
and helper assignments. Runtime model selection comes only from those project
assignments; provider credentials are decrypted just in time and never become
controller or worker environment defaults. `make dev-all` keeps Codegen offline,
without a Docker socket or branch/PR authority. The separate `development_pr`
overlay is an explicit local-only draft workflow.

APDL does not run an operator model-evaluation program or promote generated
changes based on a platform corpus. Every production pull request starts as a
draft. Tenants evaluate it through their own GitHub CI, review rules, branch
protection, and merge process; APDL only observes those results and may perform
bounded same-branch repairs.

The 0.3.0 dependency gate covers both the offline API/control plane and the
published `Dockerfile.worker` Aider dependency graph. The worker uses a
reproducible universal Python 3.12 hash lock, and CI permits only three exact,
expiring no-fix advisories with checked-in reachability evidence. The tenant
runtime pins all Codegen images by local digest and isolates model egress behind
the shipped proxy policy.

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

All `/v1` endpoints require exactly one authority header: the canonical
`X-API-Key`, or a private `X-APDL-Internal-Capability` minted just in time for
one live Agents execution. Codegen derives the project and roles from
PostgreSQL and independently checks every body, query, path, and
changeset-owned project. Internal mutation capabilities are short-lived,
request-bound, and consumed atomically; there is no permissive or global
internal bearer token. Project authority is not repository authority: only an
active grant backed by a GitHub user's repository-admin proof can authorize
GitHub access.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/github/repository-authorizations` | Start a project-scoped GitHub App installation and user-authorization flow |
| GET | `/github/repository-authorization/callback` | Consume one GitHub setup/OAuth callback and return only a trusted redirect |
| GET | `/v1/github/repository-authorizations/{authorization_id}?project_id=...` | Read the caller-owned short-lived authorization and server-discovered repository choices |
| POST | `/v1/github/repository-authorizations/{authorization_id}/complete` | Bind one opaque repository candidate to the project |
| GET | `/v1/connections/{project_id}` | Read the active grant projection (`grant_id`, immutable `repository_id`, display-only `repository_full_name`) |
| GET | `/v1/connections/{project_id}/tenant-policy` | Read the strict tenant-owned Codegen preferences |
| PUT | `/v1/connections/{project_id}/tenant-policy` | Replace tenant preferences (tightening only) |
| GET | `/v1/connections/{project_id}/repo-context` | Strict canonical `repo_profile@1` for planning agents |
| GET | `/v1/llm-connections?project_id=…` | List the project's active provider connections without credential material |
| PUT | `/v1/llm-connections/{provider}` | Owner-controlled create or credential replacement with live model discovery |
| GET | `/v1/llm-connections/{provider}/models?project_id=…` | Read the validated current model inventory |
| POST | `/v1/llm-connections/{provider}/refresh-models` | Owner-controlled credential revalidation and inventory refresh |
| POST | `/v1/llm-connections/{provider}/revoke` | Owner-controlled terminal connection revocation |
| GET | `/v1/capabilities/changeset-creation?project_id=…` | Authenticated project capability and exact blocking reasons |
| POST | `/v1/changesets` | Enqueue a changeset during a PR publication stage |
| GET | `/v1/changesets?project_id=…` | List a project's changesets |
| GET | `/v1/changesets/{id}` | Fetch one changeset |
| GET | `/v1/changesets/{id}/observations` | Read append-only GitHub PR/CI and repair observations |
| GET | `/v1/changesets/{id}/runtime-observations` | Read exact-head GitHub Actions logs/artifact evidence |
| POST | `/v1/changesets/{id}/abandon` | Abandon queued pre-PR work |
| POST | `/v1/changesets/{id}/retry` | Retry an eligible failed changeset after rechecking capability |
| POST | `/v1/changesets/{id}/revert` | Enqueue a revert changeset after rechecking capability |
| POST | `/webhooks/github` | HMAC-verified recovery trigger (`pull_request`, `check_run`, `check_suite`, `status`) |
| GET | `/health`, `/ready` | Liveness / PostgreSQL readiness |

Two GitHub ingress routes sit outside the project API-key boundary. The
short-lived `/github/repository-authorization/callback` accepts only a one-time
state-bound GitHub setup or OAuth response. `/webhooks/github` always requires
`X-Hub-Signature-256` verified with the required `GITHUB_WEBHOOK_SECRET`.

Create, retry, and revert synchronously re-evaluate the same project capability
before writing a new changeset. This is an intentional API change: a project
without an active repository grant previously received `404`; it now receives
`409` with `detail.code: changeset_creation_disabled` and an exact `reasons`
list. The reasons distinguish rollout, automation, repository grant, GitHub App,
model provider, worker, and runtime blockers. Those deployment diagnostics are
intentionally available only to an authenticated, same-project
`agents:manage` principal; they must not be exposed through an unauthenticated
proxy. The Docker runtime result, including a failed result, is single-flight
and cached for at most five seconds for the exact rollout stage, editor
instance, and `CODEGEN_REVISION`. Expiry or any of those identity changes forces
a fresh probe.

## Repository authority

Repository onboarding is a project-scoped, user-authorized GitHub App flow.
The project owner, or a member delegated both `agents:manage` and
`credentials:manage`, starts the flow in Admin. After installing or configuring
the App, the same flow obtains a short-lived GitHub App user token and discovers
only repositories that are both exposed to this App and administered by that
GitHub user. The token is revoked after discovery and is never persisted.

The setup callback's `installation_id`, repository slugs, and arbitrary numeric
repository IDs are never accepted from the browser as authority. Codegen rotates
a one-time hashed state before OAuth, and the Admin callback relay binds both
legs to the initiating browser with a short-lived, callback-scoped `HttpOnly`
cookie. The user OAuth leg uses S256 PKCE. Codegen then verifies the
authenticated GitHub user, stores server-discovered choices behind opaque
candidate IDs, and records one canonical grant containing:

- the APDL `project_id`;
- the internal GitHub App `installation_id`;
- GitHub's immutable numeric `repository_id`;
- `repository_full_name`, retained only as a display and clone locator;
- grant status plus the APDL and GitHub user evidence that authorized it.

If GitHub reports `state` plus `setup_action=request`, the organization requires
an owner to approve the App. Codegen consumes and deletes that one-time flow,
does not advance to OAuth, and redirects Admin with the strict
`installation_approval_required` status and the canonical project ID. A
successful OAuth callback likewise redirects with both the opaque authorization
ID and canonical project ID. Admin accepts that project context only when it is
present in the signed-in user's workspace list, switches to it before querying,
and never treats a callback parameter as repository authority.

Candidate rows are immutable and the Codegen runtime has no `UPDATE` privilege
on them. After live GitHub revalidation, completion locks the selected row and
compares every stored repository coordinate with that verified snapshot before
revoking or replacing the current connection. A changed or delete-and-reinserted
candidate fails with a conflict and leaves the existing connection untouched.

Migration `059_github_repository_user_authorization.sql` does not infer this
evidence for rows created by earlier migrations. Any pre-existing grant carrying
the old `github_oauth` label is relabeled `legacy_unverified` and terminally
revoked because it has no recorded APDL actor or immutable GitHub user ID. Such
a grant cannot authorize Codegen work. The project owner must reconnect the
repository through the Admin GitHub App flow; there is no evidence backfill or
operator override that reactivates the quarantined row.

The browser completes a connection using only the project ID, authorization ID,
and opaque candidate ID. The public connection contract contains `grant_id`,
`repository_id`, and `repository_full_name`; it never exposes `installation_id`
and never treats the repository name as authority. Repository renames may update
the display name only when the numeric ID is unchanged. A transfer, deletion,
installation change, revocation, or ID mismatch fails closed and requires a new
user authorization.

Every changeset snapshots its grant and immutable repository target. Before a
clone, push, PR mutation, poll, or repair, Codegen checks that the snapshot still
belongs to the project and that its grant is active. Installation tokens are
minted for exactly that repository ID with an operation-specific permission
set; a token response that does not match the requested repository and
permissions is rejected. Rebinding a project therefore cannot retarget queued
or open work.

The local operator command remains a break-glass provisioning path for trusted
deployments without Admin user onboarding. From the repository root, run:

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

Only an active repository grant is proof of authority. Repository or
installation metadata alone must never be promoted into authority. Connecting a
repository also does not create the separate project execution authorization
used to gate effectful autonomous runs.

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

The GitHub integration targets GitHub.com and has one canonical configuration:
all seven `GITHUB_*` values below are required. Register the callback value
exactly as both the App setup URL and OAuth callback URL.

```
POSTGRES_URL=postgresql://apdl_runtime:apdl_runtime_dev@localhost:5432/apdl
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY_BASE64=     # standard Base64 of the UTF-8 PEM
GITHUB_APP_SLUG=
GITHUB_APP_CLIENT_ID=
GITHUB_APP_CLIENT_SECRET=
GITHUB_APP_CALLBACK_URL=http://localhost:5173/api/github/codegen/callback
GITHUB_WEBHOOK_SECRET=
LLM_VAULT_URL=http://localhost:8086
LLM_VAULT_CODEGEN_TOKEN=           # Codegen-only JIT credential access token
LLM_VAULT_PROJECTION_TOKEN=        # vault-to-Codegen model projection token
CODEGEN_LLM_BROKER_DIR=/tmp/apdl-codegen-llm-broker # absolute host-visible broker root
CODEGEN_REVISION=                  # immutable controller/worker revision
CODEGEN_ROLLOUT_STAGE=offline      # offline | development_pr | tenant_draft_pr
                                   # base Compose forces offline
CODEGEN_DEVELOPMENT_MODE=          # experimental internal marker; leave unset
CODEGEN_PLATFORM_SAFETY_POLICY_PATH= # absolute path to operator safety-policy JSON
CODEGEN_SANDBOX=docker             # fail-closed isolated-worker default
CODEGEN_SANDBOX_IMAGE=             # exact worker image ID in tenant_draft_pr
CODEGEN_SANDBOX_NETWORK=           # development_pr only; tenant runtime requires empty
CODEGEN_CONTROLLER_IMAGE_ID=       # exact tenant-runtime controller image ID
CODEGEN_EGRESS_POLICY_SHA256=      # digest of checked-in proxy policy sources
CODEGEN_EGRESS_PROXY_IMAGE_ID=     # exact tenant-runtime proxy image ID
CODEGEN_EGRESS_SOCKET_VOLUME=      # controller-owned proxy Unix-socket volume
CODEGEN_EGRESS_PROXY_URL=http://127.0.0.1:3128
CODEGEN_TRUSTED_REPOS_ONLY=false   # explicit opt-in for local in-process mode
CODEGEN_JOB_BUDGET=3000            # optional lower cap; cannot exceed 50 minutes
CODEGEN_KILL_SWITCH=               # "true" halts all changeset jobs
CODEGEN_DISABLED_PROJECTS=         # comma-separated per-project denylist
```

Generate the canonical private-key value as one unwrapped line:

```bash
openssl base64 -A -in path/to/github-app.private-key.pem
```

Paste that output directly after `GITHUB_APP_PRIVATE_KEY_BASE64=` in the
untracked `.env` file. Base64 is transport encoding, not encryption: restrict
access to `.env` like the original PEM and never commit either one. This is a
breaking configuration change; deployments that previously supplied an inline
PEM or a PEM file path must encode the file and migrate to the single setting
above.

Generate the webhook signing secret independently:

```bash
openssl rand -hex 32
```

Generate the Codegen credential-encryption key independently from every provider
credential and keep it in the deployment secret manager:

```bash
openssl rand -base64 32 | tr -d '\n'
```

Codegen tenant serving supports one strict provider set: `anthropic`, `openai`,
`google`, and `xai`. Model IDs must come from the current validated project
inventory and the supported Codegen catalog. Provider API endpoints are fixed;
custom base URLs, arbitrary LiteLLM prefixes, ambient provider credentials,
Vertex AI, and Amazon Bedrock are not accepted. Supporting another provider
requires an explicit credential, routing, isolation, and egress contract.

### Project LLM connections and assignments

Project LLM credentials are created and replaced through the owner-controlled
Admin proxy to the shared LLM Vault. One connection can be granted to Agents,
Codegen, or both. The canonical create body uses an explicit label, provider,
and consumer set:

```json
{
  "project_id": "demo",
  "label": "Primary OpenAI",
  "provider": "openai",
  "api_key": "provider-secret-from-your-secret-manager",
  "consumers": ["agents", "codegen"]
}
```

The vault validates the key against the provider's fixed model-list endpoint,
asks Codegen to project compatible reviewed models, encrypts the key at rest,
and atomically writes the vault record plus Codegen's non-secret projection.
Codegen's `/v1/llm-connections` routes are read-only. Replace, refresh, and
revoke operations remain in Project settings and require project ownership or
both `agents:manage` and `credentials:manage`, plus the exact connection
version.

After creating the required connections, a trusted control-plane operator
atomically assigns exactly one editor model and one helper model:

```bash
cd services/codegen
.venv/bin/python -m scripts.assign_llm_models \
  --project-id demo \
  --editor-provider anthropic \
  --editor-model-id claude-sonnet-5 \
  --helper-provider openai \
  --helper-model-id gpt-5.4-nano \
  --actor operator@example.com
```

The two roles may use different providers, but each selected model must be in
that project's active inventory and support the assigned role. New changesets
snapshot both assignments. Each brief, edit, review, or repair phase then
revalidates current project, repository, deployment, model, connection, and
credential authority before requesting only that phase's exact credential from
the vault through the local broker. Replacing or revoking a credential therefore affects the next
phase that has not started provider egress; no global `CODEGEN_MODEL`,
`CODEGEN_HELPER_MODEL`, or ambient provider-key setting can route tenant work.

Only the LLM Vault deployment receives the platform encryption key. Codegen
receives a scoped workload token and never receives ciphertext-table access or
key material.

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
The base service forces `CODEGEN_ROLLOUT_STAGE=offline`, exposes no host port,
and mounts no Docker socket. It can be inspected by operators but cannot create
a branch or pull request.

## Secure tenant draft runtime

`tenant_draft_pr` is the production draft-publication capability. It accepts no
deployment-level provider model or credential. Every request resolves the
project's active editor and helper assignments and obtains one phase-bound
credential from the local broker.

Build and validate the three runtime images, apply migrations, then start the
tenant overlay:

```bash
export CODEGEN_REVISION="$(git rev-parse HEAD)"
make build-codegen-runtime
make migrate-postgres
make codegen-tenant-config
make codegen-tenant-up
```

The build targets tag local images for convenience. `codegen-tenant-config`
resolves those tags to exact local `sha256:...` image IDs and rejects a mutable
controller, worker, or proxy reference. It also verifies the controller and
worker revision/role labels, the proxy's checked-in policy digest, the Docker
socket and group, the broker directory, and the rendered Compose contract. The
overlay hard-codes `tenant_draft_pr`; ambient host settings cannot promote the
offline base service.

Workers run with Docker `--network none`, mount the controller-owned proxy socket
volume read-only, and use a loopback TCP-to-Unix relay for standard HTTP clients.
Startup and every worker launch revalidate the proxy image, healthcheck,
non-root/read-only security settings, socket topology, public uplink, and active
metadata/private/direct-bypass probes. The allowlist covers only checked-in
GitHub, model-provider, and package-registry destinations.

The overlay mounts a Docker control socket into the credential-bearing Codegen
API. That grants host-root-equivalent container authority; production operators
should prefer a dedicated rootless or policy-constrained worker launcher. The
destination policy lives in `infra/docker/codegen-egress/`; changing it changes
the required `CODEGEN_EGRESS_POLICY_SHA256` and proxy image identity.

For local draft-PR development, `docker-compose.codegen-development.yml` remains
an explicit `development_pr` mode with a mutable worker and unfiltered named
bridge. It is not a production deployment template.

Two auxiliary LLM passes bracket the edit. Low-risk work may skip them when the
model is unavailable; medium/high-risk work fails closed. `CODEGEN_BRIEF` compiles the
approved spec into a repo-grounded engineering brief before the agent runs
(concrete files, explicit descoping of non-repo asks, checkable acceptance
criteria), and `CODEGEN_REVIEW` judges the produced diff against the original
spec before the push. A review rejection re-invokes the agent with feedback
(`CODEGEN_EDIT_RETRIES`, default 1) before the changeset fails; the retry message
re-carries the full work order, since each aider
invocation is a fresh process. Tenant execution runs these passes with the
project's helper assignment; edit and repair use its editor assignment.

GitHub merge observation records the merge commit SHA, and `/revert` uses it deterministically:
the editor fetches the commit into the shallow clone and runs `git revert`
(mainline parent 1 for merge commits). APDL exposes no merge endpoint or tool.

## Develop

```bash
make run-codegen         # uvicorn on :8084 (hot reload)
make test-codegen        # pytest
make lint-codegen        # ruff
```

The release builds the Codegen worker from `Dockerfile.worker` and gates its
universal Python 3.12 hash lock in CI. Aider remains pinned at 0.86.2; its two
development-build-only advisories and diskcache's unreachable pickle-cache
advisory have exact, expiring, machine-validated suppressions. Every advisory
with an available fix is blocked, as is any new, stale, mismatched, or expired
suppression.

### Dependency suppression ownership and renewal

The three approved worker suppressions have one accountable owner and two hard
UTC boundaries:

- **Owner:** `@Sukhikhk`
- **Review-fail boundary:** `review_by = 2026-08-22`; no unreviewed run is
  accepted after that date.
- **Expiry:** `expires_on = 2026-10-21`; the gate rejects the suppressions on
  that date.

There is no JSON-only renewal path. Prefer upgrading or removing the dependency
and deleting the suppression. If a no-fix suppression must be renewed, make one
reviewed change that:

1. revalidates the advisory, installed versions, reachability, and every named
   evidence test;
2. updates the approved owner/review/expiry authority in
   `scripts/audit_worker_dependencies.py`;
3. updates the matching `owner`, `review_by`, and `expires_on` fields for every
   affected entry in `dependency-audit-suppressions.json`;
4. updates constraints, the hashed lock, justification, and evidence references
   when any package or proof changed; and
5. runs `make test-codegen` and
   `services/codegen/scripts/audit_worker_dependencies.sh`.

The Python authority and JSON policy must land together. Any one-sided date,
owner, advisory, version, evidence, or lock change fails closed; neither file is
an override for the other.

### A new advisory on a pinned transitive dependency

This is the most common way the gate turns red, and it answers to no calendar:
the advisory feed publishes a finding against a version this lock already pins.
It has already happened once here — `gitpython==3.1.53` / `GHSA-fjr4-x663-mwxc`
turned the gate red four weeks before the first suppression date. Treat it as
routine maintenance, not as a suppression argument.

The gate reports such a finding as `unexpected fixable vulnerability`.
`fixable` means `pip-audit` returned at least one fix version, so a fixed
release exists upstream and the remedy is an upgrade — never a new suppression.
A finding with no fix version is reported as `unsuppressed` and is the only case
the renewal workflow above applies to.

Aider pins several transitive versions exactly, so the upgrade goes in the
override input rather than the two intentional pins:

1. raise the package in `requirements-agent.overrides.txt` to the lowest
   non-vulnerable release named by the advisory;
2. regenerate the hashed lock with the exact command documented at the top of
   `requirements-agent.constraints.txt`, from `services/codegen`;
3. update the package's entry in `EXPECTED_LOCKED_VERSIONS` and in the security
   input assertion in `tests/test_worker_dependency_audit.py`, which pin the
   lock's contents independently of the lock file itself;
4. run `make test-codegen` and
   `services/codegen/scripts/audit_worker_dependencies.sh`; the gate
   regenerates the lock and fails if the committed file differs.

Do not add the advisory to `dependency-audit-suppressions.json`. A suppression
for a fixable finding is rejected by the gate, and the three approved
suppressions are the complete no-fix set.

## Editor execution model (release-gated worker; publication remains preview)

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
  with `CODEGEN_TRUSTED_REPOS_ONLY=true` while publication is `offline`. The
  service refuses this mode for every PR publication stage.

Enable the sandbox:

```bash
make build-codegen-sandbox        # revision-labeled production worker
export CODEGEN_SANDBOX=docker
unset CODEGEN_SANDBOX_NETWORK
# `make codegen-tenant-up` mounts the explicit Docker socket and immutable images.
```

The local `development_pr` overlay creates an explicitly development-only bridge
that is not egress-filtered. `tenant_draft_pr` rejects every configured sandbox
network and requires `--network none`, the shipped proxy image, exact policy
digest, controller-owned socket volume, exact proxy runtime configuration, and
successful controller probes. The allowlist
covers only the checked-in GitHub, model-provider, and package-registry domains;
private, link-local, metadata, reserved, and direct non-proxy egress are denied.
The same topology and probes are re-attested immediately before every
inspection and editor container. Tenant deployment also hard-pins
`CODEGEN_MAX_CONCURRENT_JOBS=1`.
Tunables: `CODEGEN_SANDBOX_IMAGE`, `CODEGEN_SANDBOX_MEMORY`,
`CODEGEN_SANDBOX_CPUS`, `CODEGEN_SANDBOX_PIDS`, `CODEGEN_DOCKER_BIN`. Mounting a
Docker socket still grants the API process host-level Docker authority; deploy
the API and worker launcher on a dedicated host or use a remote worker boundary.

## Production prerequisites

The autonomous loop runs once these external pieces are set up:

1. **Register a GitHub App** (org-level) with minimal permissions — `contents:
   write`, `pull_requests: write`, `checks: read`, `actions: read`, `metadata:
   read`, and `statuses: read`. `actions: read` is required only to collect exact-head workflow jobs,
   bounded failure logs, and runtime artifacts; it does not let APDL approve CI
   or merge. Existing installations must approve the added permission before
   runtime evidence can be collected; until then it remains explicitly
   unverified. Set
   `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_BASE64`, `GITHUB_APP_SLUG`,
   `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`,
   `GITHUB_APP_CALLBACK_URL`, and `GITHUB_WEBHOOK_SECRET`. Configure the
   callback URL as both the GitHub App setup URL and an OAuth callback URL.
   Leave GitHub's automatic "Request user authorization (OAuth) during
   installation" option disabled; APDL starts its own state-bound OAuth leg
   after setup. Customers install the App and connect an exact repository from
   the project's Admin Codegen page as described in
   [Repository authority](#repository-authority). Never provision a repository
   from an unverified installation ID.
2. **Provision the coding agent.** Make `aider` available where
   the editor runs
   — `uv pip install -e ".[agent]"` on the codegen host for v1, or build the
   hardened sandbox image (`Dockerfile.worker`) to run one changeset per
   container. Create the project's provider connections and atomically assign
   its editor and helper models as described above. Build the immutable runtime,
   validate it with `make codegen-tenant-config`, and start it with
   `make codegen-tenant-up`. Optionally set each repo's test command through
   connection `tenant_policy.test_cmd` (otherwise it is auto-detected).
3. **Configure the App webhook** → point GitHub at `POST /webhooks/github`
   using the configured `GITHUB_WEBHOOK_SECRET` and subscribe to `pull_request`,
   `check_run`, `check_suite`, and `status`. Polling remains the recovery path
   for missed deliveries.
4. **Enable GitHub branch protection/rulesets** on the default branch (require PR,
   reviews, and green checks). GitHub is the enforcement and merge authority.

Flow: an approved feature proposal enqueues a `code_implementation` run (agents
service) → `POST /v1/changesets` → the job recomputes and persists publication
authority → only an allowed decision permits minting a repo token → the Aider
editor in a sandboxed clone returns a gated patch and exact tree identity → the
controller reconstructs and publishes that tree with a just-in-time write
credential, then recovers or opens one branch-bound PR (draft when policy or
evidence requires it) →
the repo's CI runs → the webhook or poller records GitHub's exact-head external
CI status and feeds bounded logs/artifacts into same-branch repair → GitHub
reviews/rulesets decide readiness and merge.
