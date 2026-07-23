# APDL Current-Branch Code and OSS Release Audit

**Audit date:** 2026-07-22  
**Snapshot:** `67a7e6c9ad5bbeba280c21b8fae9eb131271d568`  
**Branch treatment:** Per the audit request, the checked-out snapshot is treated as `main`.  
**Verdict:** **No-go for a production-ready OSS/GA release.** A deliberately scoped developer preview is feasible after the critical experiment defects and unsafe defaults are fixed or the affected features are disabled and clearly marked experimental.

## Executive assessment

APDL has a credible, well-tested core data plane. Fresh PostgreSQL and ClickHouse migrations work, the SDK-to-Ingestion-to-Redis-to-ClickHouse path works, flag creation/evaluation works on its happy path, and the authoritative experiment-analysis smoke succeeds. The codebase also contains unusually strong tenant scoping, request validation, mutation auditing, optimistic concurrency, durable delivery patterns, and release-contract verification.

The release cannot currently make a trustworthy production claim because:

1. Experiment targeting does not restrict enrollment.
2. Actors excluded by experiment traffic allocation are recorded and analyzed as control/default actors.
3. Unrelated identity conflicts can prevent valid experiment decisions.
4. The default LLM governance setup cannot use the cloud credentials the distribution asks users to configure, and its local endpoint is wrong inside the container network.
5. Three advertised autonomous workflows are intentionally disabled, so the autonomous product loop is not closed.
6. Codegen's secret detector misses current OpenAI, Anthropic, and Google credential formats, while repository-selected code executes in a worker that also holds live credentials.
7. Public registration and Ingestion authentication both expose pre-rate-limit resource-exhaustion paths.
8. Service images are not released, the most sensitive Codegen runtime is not reproducibly locked, and the full Agents/Codegen path is absent from release smokes.
9. The repository's own `make check` command is not reliable under its parallel workload because Admin tests retain a 5-second timeout.

The appropriate public positioning today is “core analytics/feature-management developer preview,” not “production-ready autonomous product-development loop.”

## Scope and method

The audit covered all non-Markdown implementation and release surfaces:

- Admin console and Admin API
- Ingestion, Config, Query, Agents, and Codegen services
- Redis-to-ClickHouse writer
- JavaScript and Python SDKs
- PostgreSQL and ClickHouse migrations
- Docker Compose, Dockerfiles, gateway configuration, CI, release workflow, package metadata, locked dependencies, and operational scripts
- Cross-service schemas and end-to-end event, flag, experiment, agent, and code-publication paths

No existing `.md` file was opened, read, or searched. Documentation content was therefore deliberately excluded. The repository contains files named `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `SUPPORT.md`, `GOVERNANCE.md`, and `CHANGELOG.md`; only their presence was checked, not their contents or quality.

Severity meanings:

- **Critical:** can silently corrupt a primary product result or defeats a central safety boundary; blocks any release containing the feature.
- **High:** material security, reliability, operability, or advertised-product gap; blocks a production-ready release.
- **Medium:** meaningful correctness, scaling, contract, or maintainability weakness; should be fixed or documented before broad adoption.
- **Low:** localized defect, hardening opportunity, or developer-experience problem.

## Verification results

| Check | Result | Notes |
|---|---:|---|
| Individually configured test suites | 3,288 passed, 6 skipped | Covers scripts, both SDKs, Admin, all services, and writer. |
| Repository `make check` | Failed | 20 of 21 parallel jobs passed. Admin had 8 timeouts out of 360 tests at its default 5-second limit. |
| Admin coverage run | 360 passed | Passed with the coverage configuration's 10-second timeout. Global coverage: 72.91% statements, 61.74% branches, 67.84% functions, 74.72% lines. |
| JavaScript SDK package checks | Passed | TypeScript, 418 tests, browser build, ESM/CJS/IIFE build, `publint`, and pack dry-run. |
| Admin production build | Passed | Bundle-size gate passed; initial and total budgets retain only about 10% headroom. |
| Python lint | Passed | Ruff passed for every Python service, SDK, writer, tests, and relevant scripts. |
| Locked dependency audit | Passed with scope caveat | No known vulnerability was reported, but the Codegen `agent` extra/runtime is explicitly excluded. |
| Fresh core install smoke | Passed | Fresh volumes, all migrations, core containers, one browser event, query, and flag lifecycle. |
| Fresh experiment smoke | Passed | Production Config/Query/ClickHouse happy path with 72 events. It does not test partial traffic or nonmatching targeting rules. |
| Release contract verifier | Passed | Release contract reports version `0.3.0`. |
| Script contract tests | 36 passed | Release/setup/migration helper coverage. |
| Compose validation | Passed | Full and dependency-only Compose configurations parse successfully. |
| Agents live-Postgres execution tests | 3 skipped | CI does not provide the required Agents Postgres integration environment. |
| Query real-ClickHouse selector tests | 2 skipped | Normal Query tests mock results or inspect SQL; the fresh experiment smoke covers only one production SQL path. |

The failed `make check` result is a test-harness defect rather than evidence that eight Admin behaviors are functionally broken: the same 360 Admin tests pass under the CI coverage configuration's 10-second timeout, and isolated failed tests pass. It still matters because the advertised repository-wide local check is load-sensitive and non-deterministic.

## Service relevance and completeness scorecard

| Area | Product role | Relevance | Completeness / release state |
|---|---|---|---|
| Admin console | Operator control plane | Essential | Broad UI exists, but permissions, cross-record form state, operational visibility, and high-value test coverage need work. |
| Admin API | Session, tenant, credential, and service proxy boundary | Essential | Strong authorization design; unsafe public registration and missing role-management path block a safe turnkey release. |
| Ingestion | Authenticated event entry point | Essential | Strong event validation and atomic publishing; pre-auth abuse and incomplete reserved-event privacy enforcement remain. |
| Config | Flags, experiments, evaluation, SSE, audit/outbox | Essential | Flag core is credible; experiment enrollment is critically incorrect and the service is single-replica by design. |
| Query | Analytics and experiment decisions | Essential | Rich bounded analytics; experiment identity/completeness and guardrail SQL defects can invalidate production decisions. |
| Agents | LLM orchestration and governed autonomous workflows | Strategic differentiator | Governance foundations are strong, but defaults cannot perform LLM work and several advertised workflows are disabled. |
| Codegen | Governed repository modification/publication | Strategic, optional | Sophisticated gates and publication recovery exist; secret detection, trust boundaries, reproducibility, and live integration testing are not release-grade. |
| ClickHouse writer | Durable event projection | Essential | Delivery semantics are strong; discovery has an O(projects) hot-path cost that will limit scale. |
| JavaScript SDK | Primary browser integration | Essential adoption surface | Feature-rich and strict; environment discovery, flag freshness, identity updates, cookieless mode, SPA capture, and unload delivery have gaps. |
| Python SDK | Server integration | Important adoption surface | Good queue semantics; canonical validation drifts from Ingestion and process-lifetime exposure state is unbounded. |
| Storage/migrations | Durable contract authority | Essential | Ordering, locking, checksums, and constraints are strong; retention and operational maintenance are incomplete in places. |
| CI/release/containers | OSS delivery and trust | Essential | Excellent core checks and package provenance; no application-image release, SBOM/signing, full-stack gate, or consistently hardened/reproducible containers. |

## Critical release blockers

### C-01 — Experiment targeting rules do not restrict enrollment

**Evidence**

- `services/config/app/flags/experiment_flag.py:59-74` puts targeting rules on the backing flag but also leaves the experiment traffic rollout as the flag fallthrough.
- `services/config/app/flags/evaluator.py:300-340` skips a nonmatching rule and then evaluates that fallthrough.
- `services/admin/src/features/experiments/ExperimentForm.tsx:694-708` tells operators that leaving targeting empty targets everyone, implying nonempty rules restrict the audience.
- A direct reproduction with `plan == pro`, `traffic_percentage=100`, and a `plan=basic` actor returned `treatment` with reason `fallthrough`.

**Impact**

Every experiment with a targeting rule can enroll actors outside the intended population. Results are contaminated silently, and potentially sensitive or risky treatments can reach explicitly nonmatching users.

**Required fix**

Define one canonical enrollment result distinct from the default variant. A nonmatching actor must return no experiment assignment and must not emit an exposure. Backing-flag generation should express “target match AND traffic allocation,” not “target match OR fallthrough traffic.” Add cross-runtime tests for matching, nonmatching, partial-traffic, and disabled experiments.

### C-02 — Traffic-excluded actors are counted as control/default actors

**Evidence**

- `services/config/app/flags/experiment_flag.py:59-73` models experiment traffic as a fallthrough rollout.
- `services/config/app/flags/evaluator.py:271-340` retains `default_variant` when rollout fails and returns `rule_rollout` or `fallthrough_rollout`.
- `sdk/javascript/src/core/client.ts:559-598` and `sdk/python/apdl/client.py:403-447` log every non-null variant as an exposure.
- `pipeline/clickhouse/migrations/006_feature_flag_exposures.sql:7-78` projects the exposure reason.
- `services/query/app/clickhouse/queries.py:492-520` filters assignments by flag and time but not by enrollment reason.

**Impact**

For every experiment below 100% traffic, excluded users inflate the default/control arm. Sample sizes, conversion rates, effect sizes, stopping behavior, and decisions can all be biased. This is silent analytics corruption in a primary product capability.

**Required fix**

Return an explicit no-assignment result for traffic exclusion, do not emit experiment exposure for it, and make Query accept only canonical enrollment reasons. Backfill or quarantine already contaminated exposure data. Add a fresh-stack smoke at 10–50% traffic proving excluded actors appear in no arm.

## High-severity findings

### H-01 — Unrelated identity conflicts can invalidate an experiment

`services/query/app/clickhouse/queries.py:522-547` puts all project actors firing the primary metric into `metric_events`, then counts conflicted metric actors without joining them to experiment assignments. `services/query/app/routers/experiments.py:402-407` refuses a decision when that count is nonzero. One conflicted actor who was never exposed can therefore block every experiment using a common metric. Scope conflict detection to assigned actors and the experiment's analysis window.

### H-02 — “Decision snapshots” do not establish data completeness

`services/query/app/routers/experiments.py:344-372` waits only for a configured settlement delay. It does not verify Ingestion acceptance, Redis pending depth, writer lag, Config outbox lag, or a ClickHouse watermark. `services/query/app/models/schemas.py:597-615` permanently reports `data_completeness="not_verified"` and `deployment_readiness="not_assessed"`. Late/backlogged data can therefore produce a stable-looking final decision. Either implement authoritative watermarks or stop calling the result final/decision-ready.

### H-03 — Frontend guardrails scan the project's entire retained health history

`services/query/app/clickhouse/queries.py:610-641` filters the `health_events` CTE only by project and optional page. Time, `$frontend_error`, and active-flag predicates are applied after the join and `countIf`. The projection in `pipeline/clickhouse/migrations/007_frontend_health_events.sql:7-31,56-57` also contains web vitals and retains project/month data. A 10-minute guardrail can scan a year's data and exceed Query's 5-million-row/10-second budgets. Push selective predicates into the CTE and add a large-history execution test.

### H-04 — Config's outbox grows without bound and readiness scans it

Every server exposure inserts an outbox payload in `services/config/app/store/mutations.py:1228-1269`. Delivery only sets `processed_at` in `services/config/app/outbox.py:150-158`; the schema in `pipeline/postgres/migrations/012_config_atomic_mutations.sql:173-191` retains the row and unique deduplication entry indefinitely. Readiness aggregates the table at `services/config/app/outbox.py:477-520`. Long-running installations accumulate unbounded table/index storage and progressively more expensive readiness checks. Add partitioning/retention, bounded deduplication semantics, indexed partial summaries, and maintenance telemetry.

### H-05 — Stock LLM configuration cannot execute governed cloud calls

`.env.example:53-61` and `infra/docker/docker-compose.yml:203-206` solicit cloud-provider credentials. The default policy migration, `pipeline/postgres/migrations/023_llm_governance.sql:80-144`, provisions only local `gemma4`, exact endpoint `http://localhost:11434/v1`, local residency, and zero budgets. `services/agents/app/store/llm_governance.py:423-455,537-548` enforces exact provider/model/endpoint matching and rejects paid calls at zero budget. Inside Docker, `localhost` points at the Agents container, not a host model server. No non-Markdown API or shipped in-image CLI was found for configuring policy. The setup can appear provider-ready while every real call fails governance.

Provide a first-run governance workflow, a valid container-network local endpoint, explicit cloud templates, and readiness that performs a policy-admissible dry run rather than merely detecting a key.

### H-06 — The advertised autonomous loop is not implemented end to end

Experiment evaluation, feature proposal, and personalization are disabled in:

- `services/agents/app/graphs/experiment_evaluation.py:1-27`
- `services/agents/app/graphs/feature_proposal.py:132-177`
- `services/agents/app/graphs/personalization.py:1-47`

`services/agents/app/routers/triggers.py:82-97` rejects them. Code implementation consumes approved proposals, but no enabled workflow produces those proposals. This is a product-completeness gap, not merely a documentation issue. Either implement and release-gate the workflows or remove them from public UI/schema/product claims and define the preview around behavior analysis and experiment design.

### H-07 — Codegen's secret gate misses current provider credentials

`services/codegen/app/safety/gates.py:30-36` detects legacy `sk-...`, GitHub, AWS, and Slack formats but misses modern `sk-proj-*`, `sk-ant-api03-*`, and `AIza*`. Direct checks confirmed all three bypass it. `services/codegen/app/editor/container_editor.py:293-295,361-364,451-456` injects provider credentials into workers. A more complete pattern list already exists in `services/codegen/app/editor/prompts.py:21-40`, demonstrating internal contract drift. Centralize one tested detector, scan inputs/diffs/logs/artifacts, and use structured provider-specific validation where possible.

### H-08 — Repository-selected execution shares a trust boundary with live credentials

`services/codegen/app/contracts/installer.py:215-330,563-683` installs repository lockfile dependencies and runs TypeScript/compiler and Python analyzer entry points. The parent worker holds provider keys and GitHub read credentials. Child-environment sanitization at `:84-109` is defense in depth, not a security boundary, because code still executes under the same container/user/PID namespace. A malicious dependency or build hook can target the worker itself. Run repository-controlled evaluation in a provider-free subordinate sandbox and broker only narrowly scoped model/GitHub operations from a separate process.

### H-09 — Public registration can exhaust the Admin API

`services/admin-api/app/auth.py:368-408` exposes always-on unauthenticated registration. It opens a transaction and runs Argon2 hashing synchronously on the async event loop at line 392; login correctly uses `asyncio.to_thread` at lines 295-299. There is no application-level registration rate limit, account quota, invite/deployment switch, or project quota (`services/admin-api/app/projects.py:26-61`). Even duplicate-email attempts pay the hash cost. Concurrent requests can block the event loop while holding DB connections and create unlimited accounts/projects.

Move hashing off-loop before the transaction, add edge and application rate limits, cap concurrency, and make public registration an explicit deployment option.

### H-10 — Invalid Ingestion credentials reach PostgreSQL before rate limiting

Every syntactically valid random `proj_...` or `client_...` key executes a database query in `services/ingestion/app/auth.py:89-104`. Authentication is a dependency at `services/ingestion/app/main.py:244`; rate limiting occurs later in `services/ingestion/app/routers/events.py:47-56`. The auth pool max is five connections (`services/ingestion/app/main.py:182-189`). An unauthenticated attacker can saturate it with random keys. Add an edge/pre-auth IP limit, bounded auth concurrency, statement timeouts, and a safe negative cache.

### H-11 — Python SDK accepts events that Ingestion permanently rejects

`sdk/python/apdl/types.py:140-159,240-264` lacks the server's event/identity bounds. `services/ingestion/app/models/schemas.py:105-115,135-144` restricts event names to 256 characters and IDs to 128. A 257-character event and 129-character user ID were reproduced as SDK-valid and server-invalid. `sdk/python/apdl/transport.py:97-104` and `queue.py:199-205` then permanently discard the whole 4xx batch, including neighboring valid events. Mirror every canonical server constraint in the SDK and add SDK-to-Ingestion contract tests generated from one schema source.

### H-12 — The ClickHouse writer performs O(project-count) work per stream read

`pipeline/redis/clickhouse_writer.py:499-522,575-620,771-787` scans the keyspace and calls `XINFO GROUPS` for every project on each single-stream consume iteration. `_known_stream_keys` is populated but does not avoid repeated checks. At 1,000 projects, each read can incur roughly 1,000 control-plane round trips per replica. Move discovery and group reconciliation to a bounded background refresh, cache verified streams, and instrument scan/read costs.

### H-13 — Default dependency-only development exposes databases on the LAN

`infra/docker/docker-compose.deps.yml:3-55`, used by `make dev` (`Makefile:396-400`), publishes unauthenticated Redis and known-development-credential ClickHouse/PostgreSQL on all interfaces. The full Compose correctly binds dependencies to loopback. Bind dependency-only ports to `127.0.0.1` by default and require an explicit opt-in for remote exposure.

### H-14 — Container-only users cannot unlock autonomous execution

New projects omit `agents:run`, `agents:manage`, and `agents:approve` in `services/admin-api/app/projects.py:15-23`; the proxy requires them at `services/admin-api/app/proxy.py:347-387`. The operator provisioning script can grant them (`services/admin-api/scripts/create_admin_user.py:38-64,155-187`), but `services/admin-api/Dockerfile:3-7` and `.dockerignore:4-5` exclude scripts from the image. There is no UI/API role-management path. A stock container user can install APDL but cannot activate its defining workflows.

Ship a secured operator workflow or explicitly separate core and autonomous deployment profiles with clear bootstrap commands available in the image.

### H-15 — Codegen images and runtime dependencies are not reproducible

`services/codegen/Dockerfile`, `services/codegen/Dockerfile.worker`, and `infra/docker/codegen-egress/Dockerfile` use mutable base tags, live apt/NodeSource installation, broad Python ranges, and unpinned worker tooling. The controller runs Python 3.14 while CI tests Python 3.12 (`.github/workflows/ci.yml:197-211`). The dependency audit intentionally covers only the offline API and excludes the `agent` extra. Lock and hash the complete worker graph, pin image digests and downloaded artifacts, align CI/runtime versions, and build/scan the exact release image in CI.

### H-16 — Admin editors can retain one record's state under another record's route

`services/admin/src/features/experiments/ExperimentDetailPage.tsx:39-97` initializes form state only while `values === null`; it never resets when `:key` changes on the same mounted route. `services/admin/src/features/flags/editor/FlagEditorPage.tsx:71-124` similarly initializes `baseRef` only when null. React Router can reuse the route element when only a parameter changes. The mutation hooks use the new key while the form/base can still belong to the old key, creating a cross-record overwrite risk. Reset all record-scoped state in a key-dependent effect or key/remount the editor route, and add route-A-to-route-B tests.

### H-17 — Python SDK exposure deduplication grows for process lifetime

`sdk/python/apdl/client.py:71-73,420-453` stores every identity × flag × version × variant in `_exposure_keys` with no bound, TTL, version cleanup, or reset. A long-lived server handling many users grows indefinitely and also deduplicates across unrelated pages/components. Use bounded TTL/LRU state, caller-defined session semantics, or durable server-side idempotency.

## Medium-severity findings by component

### Admin console

1. **Permissions are enforced by the proxy but not represented consistently in the UI.** `services/admin/src/components/layout/AppShell.tsx:66-161` advertises all surfaces to every authenticated member. Flag and experiment create/edit/delete controls do not consistently gate on the membership roles that the backend requires. Read-only users encounter avoidable 403s rather than a coherent least-privilege interface.
2. **System health omits deployed components.** `services/admin/src/api/health.ts:5-30` and `features/system/HealthPage.tsx:50-79` show only Ingestion, Config, Query, and Agents while claiming responses from “each service.” Admin API, Codegen, writer health/lag, gateway, and database dependencies are absent.
3. **Backward-compatible Agent schemas violate the strict canonical-schema rule.** `services/admin/src/api/schemas/agents.ts:57-74,180-193` makes fields optional specifically for older backends. `features/agents/TriggerPage.tsx:30-55,94-110` silently substitutes hard-coded capabilities when the live endpoint is unavailable, masking server/schema drift.
4. **Server-returned Codegen URLs are rendered as links without one canonical URL policy.** Examples include `features/codegen/ChangesetsPage.tsx:160-203` and `ChangesetDetailPage.tsx:316,741`; corresponding schemas in `api/schemas/codegen.ts:739-779`, `codegen-observations.ts:39-51,194`, and `codegen-runtime.ts:268-280` mostly accept arbitrary strings. Require HTTPS and expected hosts before rendering external links.
5. **The Admin schema cannot clear a flag review date.** Config accepts explicit `review_by: null` (`services/config/app/models/schemas.py:459-489`), but `services/admin/src/api/schemas/flags.ts:374-388` permits only string/undefined and `features/flags/editor/formModel.ts:350-368` drops empty values.
6. **Coverage thresholds hide important untested surfaces.** `src/core/live.tsx` has 5.17% statement coverage, `OverviewPage.tsx` 3.84%, `HealthPage.tsx` 11.11%, `VerificationPage.tsx` and `LearnPage.tsx` 0%, `CohortsPage.tsx` 3.33%, and `HygienePage.tsx` 8.33%. Per-file thresholds protect only a few infrastructure modules.
7. **The local all-project test command is flaky under its own parallel load.** The repository runner invokes plain `npm test` with a 5-second timeout while CI coverage uses 10 seconds. One standalone run timed out once; the 21-job run timed out eight tests. Fix test isolation/performance or use a justified consistent timeout.
8. **There is no real-browser end-to-end/accessibility release gate.** The current suite is jsdom-centric. Router v7 future warnings are also unresolved.
9. **Saved-view persistence does not catch write failures.** `localStorage.setItem` in `features/analytics/SavedViews.tsx:91-103` can throw for quota/privacy reasons after UI state changes.
10. **Integration verification trusts an unparsed Ingestion response.** `features/verify/verification.ts:114-124` casts the response and checks only `accepted === 1`, bypassing the otherwise strict Zod boundary.

### Admin API

1. **The general proxy buffers complete upstream responses.** `services/admin-api/app/proxy.py:577-615` calls `await response.aread()` without a response-size limit. Concurrent large analytics or error responses can exhaust memory. Stream bounded responses or enforce a cap.
2. **Role/bootstrap management is operationally incomplete.** Authorization itself is strong, but an OSS operator needs a supported, auditable in-image path to grant/revoke project and execution roles.
3. **The service container runs as root and lacks an image-level health check.** Compose probes partly compensate, but this remains a deployment-hardening gap.

### Ingestion

1. **Reserved-event privacy enforcement is incomplete server-side.** Browser rules cover page, click, form, input, scroll, errors, and vitals (`sdk/javascript/src/privacy/auto-capture-safety.ts:70-116`), while `services/ingestion/app/privacy.py:35-47,74-122` sanitizes only click/rage-click and `validation/schema.py:16-18,193-202` strictly defines only exposure/error/vital events. A browser credential can submit other reserved events with arbitrary sensitive properties/context.
2. **The atomic rate-limit Lua is incompatible with Redis Cluster.** `middleware/rate_limit.py:140-177,244-300` uses global, project, credential, IP, and identity keys in one EVAL across different hash slots. Explicitly support standalone Redis only or redesign key authority/hash tags.
3. **Quotas and stream thresholds are hard-coded.** `middleware/rate_limit.py:35-58` and `streaming/redis_producer.py:12-15` limit operator tuning.
4. **First-class metrics, tracing, and request/correlation IDs are absent.** Health endpoints alone are not enough to diagnose auth saturation, quota pressure, or producer latency.

### Config

1. **Pool configuration can produce a live but unusable service.** `services/config/app/main.py:168-197` accepts `PG_POOL_SIZE=2` while two connections are reserved for maintenance and singleton locking. Validate a minimum with headroom.
2. **Default SSE revalidation can overwhelm the default pool.** `main.py:291-323` and `routers/stream.py:153-185` allow 1,000 clients revalidating every five seconds—roughly 200 auth queries/second—through only a few normally usable connections.
3. **Config is intentionally single-replica.** `main.py:136-149` rejects a second process; the in-memory broadcaster also assumes one owner. This prevents horizontal scaling and zero-downtime rolling replacement.
4. **Flag keys allow `/` but management routes cannot address them.** `models/schemas.py:438-450` omits the resource-key pattern used by experiments at `:729-732`; routes use `/flags/{key}` in `routers/admin.py:290-380`.
5. **Config and Query disagree on guardrail scope.** Config accepts arbitrary unbounded scope (`models/schemas.py:138-155`); Query accepts at most 512 characters and only empty or `page:` (`services/query/app/models/schemas.py:492-509`). Config can persist a guardrail Query refuses.
6. **Guardrails are not an automated safety loop.** `auto_disable` is fixed false and Query exposes caller-triggered read-only evaluation; there is no monitor-to-disable/rollback authority.
7. **Short scheduled experiments can be skipped entirely.** Schema permits a duration shorter than the scheduler's default five-minute poll (`models/schemas.py:835-868`, `main.py:326-344`), and `store/mutations.py:1185-1192` can move the experiment directly to stopped.
8. **SSE retry filtering can emit duplicate updates.** `routers/stream.py:187-197` does not advance `snapshot_version` after delivery.
9. **A theoretical 100% rollout excludes hash `0xFFFFFFFF`.** `flags/evaluator.py:29-49,216-227` can calculate exactly `100.0`, then compares with `< 100`.
10. **Malformed stored JSON is silently replaced by fallbacks.** `store/postgres.py:31-39` can mask corruption instead of failing closed and surfacing repair work.

### Query

1. **Cohorts corrupt numeric and boolean property values.** `services/query/app/models/schemas.py:458-480` accepts arbitrary properties, but `clickhouse/queries.py:405-453` uses `JSONExtractString`; non-string values collapse into an empty bucket. Reuse the typed-scalar extraction already present at `queries.py:136-202`.
2. **Most production ClickHouse SQL lacks execution coverage.** The two selector tests in `tests/test_selector_clickhouse.py:33-39,85-140` skip without an opt-in host. Funnel, retention, cohort, experiment edge cases, and guardrail scale are otherwise predominantly mocked or string-inspected.
3. **The guardrail contract is not owned end to end.** Besides schema drift, it lacks a scheduler, decision authority, and remediation audit path.
4. **`app/models/statistics.py` exposes an extensive unused statistical engine.** Runtime experiment analysis uses separate Fisher/Newcombe logic. Remove dead surface or consolidate one authoritative implementation to avoid divergent scientific claims.

### Agents

1. **Capability readiness can never report the expected available state.** Codegen returns `tenant_scoped` or `disabled` (`services/codegen/app/main.py:186,588-605`), while `services/agents/app/readiness.py:222-229` requires `changeset_creation == "available"`.
2. **Unauthenticated capability readiness performs outbound work per request.** `readiness.py:115-234` probes configured providers and downstream services without caching or a rate limit, enabling traffic amplification and leaking deployment details.
3. **Archiving a custom agent can silently strand dependents.** Save-time checks exist in `routers/custom_agents.py:166-200`, but archive at `:496-507` does not restrict, cascade, or disable dependents; runtime silently skips them.
4. **Legacy custom-agent parsing remains permissive.** `store/custom_agents.py:56-112` accepts historical tool shapes and drops malformed entries despite canonicalizing migrations, contrary to the strict-schema policy.
5. **Three live Postgres execution-lane tests are skipped in normal CI.** Durable claims around the highest-risk execution path need a real database release gate.

Strong foundations include durable execution lanes, tenant/role checks, mutation quotas, approval outboxes, pre-egress governance, LLM call accounting, audit trails, retry lineage, and fail-closed custom-tool constraints.

### Codegen

1. **`TaskSpec.context` is accepted, stored, and then discarded.** It is defined in `services/codegen/app/models/changeset.py:124-145` and sent by Agents at `services/agents/app/tools/code.py:87-119`, but omitted when `jobs/runner.py:325-341` creates `EditRequest`. Propagate it or remove it from the canonical contract.
2. **Confidential, unbounded task content travels through process arguments/environment.** `editor/container_editor.py:307-360` passes full specs, constraints, policy, ledger, and runtime plan to `docker run -e`. Values are process-list visible and can exceed OS limits; most fields in `models/changeset.py:124-180` lack bounds. Use bounded stdin or a read-only mounted envelope.
3. **Readiness exposes raw operational failures.** `main.py:588-624` can return DB exception detail, and `routers/connections.py:118-124` forwards raw GitHub failures. Return stable public codes and keep detail in structured logs.
4. **No release gate exercises the real sandbox/publication flow.** CI runs source tests but not a worker container against a controlled repository, GitHub publication recovery, model broker, or Agent-to-Codegen request.
5. **The safety architecture is promising but not yet a hardened isolation boundary.** Positive controls include repository grants, separated token roles, publication intent/recovery, exact SHA/CI identity checks, deterministic gates, a non-root sandbox, restricted capabilities, and runtime evidence.

### ClickHouse writer and storage pipeline

1. **Replica consumer names collide.** `pipeline/redis/clickhouse_writer.py:308` uses `worker-{pid}`; container replicas commonly run as PID 1, so all appear as `worker-1`. Use hostname/pod UID plus a random instance ID.
2. **Several interval settings accept zero/negative values.** Constructor validation at `:245-303` omits `flush_interval`, `pending_claim_idle_ms`, and `pending_claim_interval`; environment parsing occurs at `:1875-1886`. Invalid values can create tight loops or Redis errors.
3. **The writer image runs as root and has no image-level health check.** Digest-pinned base and hash-locked dependencies are good; runtime hardening is incomplete.
4. **History reconciliation assumes the declared sole consumer group.** `:892-949` can delete entries unsafe for another group if operators extend `streams.yaml:8-10`. Enforce the sole-group invariant or make trimming group-aware.

The writer's durability logic is otherwise strong: stable message IDs, ClickHouse deduplication, ACK/delete only after ClickHouse or DLQ durability, pending recovery, row isolation, retry backpressure, bounded shutdown, and safe DLQ metadata.

### JavaScript SDK

1. **Advertised browser zero-config environment discovery is generally ineffective.** `sdk/javascript/src/core/env.ts:2-37` uses computed `process.env[name]`; common bundlers inline only static public references and Vite uses `import.meta.env`. The built browser artifact retained the computed lookup. Missing configuration silently returns a no-op client (`core/init.ts:55-59`).
2. **Flag delivery can hang indefinitely.** Initial fetch has no timeout and SSE starts only in its `finally` (`core/client.ts:440-470`); SSE opening also has no connection timeout (`sse/connection.ts:89-114`).
3. **`identify()` and `reset()` do not refresh flag subscribers.** They change evaluator inputs at `core/client.ts:319-349` but do not invoke the handler used at `:686-708`, leaving `onVariantChange` and active-health snapshots stale.
4. **Cookieless identity is internally inconsistent.** Capture starts with a random ID before asynchronous generation (`core/client.ts:417-437,501-516`), splitting early events. `privacy/cookieless.ts:14-36` calculates only once, so a tab does not rotate at midnight; `reset()` can replace/persist a random ID.
5. **Unload batches can exceed browser keepalive limits.** `core/event-queue.ts:421-435,578-629` permits 512 KiB and `core/transport.ts:107-127` sends it as one keepalive request; browsers commonly permit about 64 KiB aggregate. Use an unload-specific budget.
6. **Malformed authoritative snapshots can preserve deleted flags.** `core/client.ts:482-488`, `sse/handlers.ts:65-74`, and `flags/cache.ts:158-169` mark invalid rows while retaining unrelated cached flags. Python has the same behavior. A deleted flag can remain active during a malformed snapshot.
7. **Persistent flag cache has no maximum age.** `flags/cache.ts:12-17,297-329` restores snapshots indefinitely during outages, including stale experiments or kill switches.
8. **SPA capture misses normal programmatic navigation.** `capture/auto-capture.ts:61-74` handles initial load and `popstate`, not `history.pushState`/`replaceState`; route page views and scroll-state resets are missed.
9. **Remote UI schemas explicitly pass unknown properties.** `ui/registry.ts:82-87` conflicts with the strict canonical-schema policy and forwards undeclared data to custom renderers.
10. **Singleton identity ignores material configuration.** `core/init.ts:66-74` keys only by client key; different endpoint, consent, persistence, capture, or queue options silently return the original instance.
11. **Browser package size has no core-specific budget.** The packed package is about 476 KB (about 2.1 MB unpacked); Admin has bundle gates, but the analytics SDK itself does not.

Strengths include strict event canonicalization, finite/cycle/accessor rejection, default-deny consent, revocation fencing, header-authenticated SSE, safe built-in DOM rendering, and extensive cross-format package validation.

### Python SDK

1. **Flag parsing silently coerces invalid canonical values.** `sdk/python/apdl/flags/models.py:49-53,87-93,136-149` accepts string percentages/booleans and a missing rule name; `flags/parse.py:53-58` does not request strict validation. JavaScript rejects these shapes. Cross-runtime assignment can differ.
2. **Malformed authoritative snapshots can retain removed flags.** `apdl/client.py:280-287` and `flags/cache.py:42-51` preserve unrelated cached entries rather than replacing the authoritative set.
3. **The SDK is synchronous/thread-based only and has no crash-durable event store.** This narrows relevance for async Python services and risks in-memory loss on process termination. It also requires Python 3.12+, excluding still-common runtimes.

The queue itself has good bounded count/byte batching, retry classification, and explicit delivery reports.

## Infrastructure, supply chain, and OSS readiness

### Release/distribution gaps

- `release-manifest.json:6-20` contains no Docker images. `.github/workflows/release.yml` publishes SDK packages and a source release, not runnable application images.
- There is no application SBOM, image signing, container provenance, image vulnerability scan, or automated dependency-license compatibility report.
- GitHub Actions use mutable major/version tags rather than commit-SHA pinning.
- The release smokes explicitly omit Agents and Codegen, so the product's defining autonomous path is not validated from a clean install.
- The Codegen `agent` dependency extra is excluded from dependency auditing.
- Core Compose images are generally digest-pinned and Python locks are hash-locked, but `infra/docker/docker-compose.deps.yml`, Codegen bases/tool downloads, and the egress image retain mutable inputs.

### Runtime consistency and container hardening

- CI/local Python targets 3.12 while several service images use Python 3.14. Admin CI uses Node 20 while its build image uses Node 26. This tests a different runtime from the artifact users execute.
- Admin API, Ingestion, Config, Query, Agents, writer, and Admin/nginx lack consistent non-root execution. Compose does not consistently set `read_only`, `cap_drop`, `no-new-privileges`, resource limits, or bounded tmpfs mounts.
- Most service images have no Dockerfile-level `HEALTHCHECK`; Compose probes provide some coverage only when Compose is the runtime.
- Development defaults and production guidance are not mechanically separated strongly enough. Known local credentials and insecure cookie settings are appropriate only if misuse outside localhost fails loudly.

### OSS readiness matrix

| Dimension | Status | Assessment |
|---|---|---|
| License file | Ready | MIT license file exists and release verification checks license copies. |
| Documentation content | Not assessed | Excluded by audit instruction; only conventional filenames were confirmed. |
| Core source build | Ready | Builds, lints, package checks, migrations, and core/experiment smokes pass. |
| Core correctness | Not ready | Critical experiment enrollment/analysis corruption. |
| Security defaults | Not ready | Registration/auth exhaustion, LAN database exposure, Codegen secret/isolation issues, root containers. |
| Autonomous product completeness | Not ready | Default governance unusable and three core workflows disabled. |
| SDK distribution | Mostly ready | npm/PyPI release mechanics and provenance are strong; runtime contract defects remain. |
| Application distribution | Not ready | No released service images or exact runnable artifact set. |
| Supply-chain assurance | Partial | Strong locks/digests in core; Codegen runtime, Actions pinning, SBOM/signing/license scans missing. |
| Test depth | Strong core / partial integration | Broad unit coverage and good smokes; optional/high-risk paths, full ClickHouse SQL set, browser E2E, load/failure tests missing. |
| Observability | Partial | Health/readiness and audits exist; metrics/tracing/lag/watermarks and complete health dashboard are insufficient. |
| Scalability/HA | Not ready | Config single-replica, writer O(projects), pool/SSE pressure, outbox retention. |
| Tenant/auth model | Strong | Project scoping, role checks, hashed credentials, origin/CSRF controls, audit and execution authority are good. |
| Operator experience | Partial | Core fresh install works; autonomous bootstrap and role management do not. |

## Positive findings worth preserving

1. **Strict data-plane boundaries:** Ingestion rejects duplicate JSON keys, non-finite values, excessive depth/nodes/container sizes, and unknown Pydantic fields.
2. **Strong tenant isolation:** Credentials are hashed, project scoped, role checked, expiry aware, and separated between browser and confidential uses.
3. **Atomic configuration changes:** Flag/experiment mutation, audit, project version, and outbox writes are transactional; Redis invalidation is version aware.
4. **Bounded analytics execution:** Query uses parameterized SQL and explicit time, memory, row, byte, and concurrency budgets.
5. **Durable event movement:** Redis publishing and writer processing preserve at-least-once behavior with deterministic deduplication and safe DLQ handling.
6. **Governed mutation foundations:** Agents has quotas, approval commands/effects, durable lanes, audit trails, and LLM governance tables even though default usability is incomplete.
7. **Thoughtful Codegen controls:** Repository grants, distinct GitHub token roles, publication recovery, exact commit/CI identity, deterministic gates, and runtime evidence are a strong base.
8. **Migration discipline:** PostgreSQL migrations are ordered, checksummed, locked, and maintenance-fenced; clean-volume core initialization passes.
9. **Release engineering:** SDK provenance, strict release-manifest verification, package-contract tests, dependency audits, and fresh-install smokes are materially better than average.

## Required release plan

### P0 — Before any public preview that enables experiments or Codegen

1. Replace experiment flag fallthrough semantics with an explicit no-enrollment result; prevent exposure emission for nonmatching and traffic-excluded actors.
2. Filter Query assignments to canonical enrollment events/reasons and scope identity conflicts to assigned actors.
3. Add clean-stack tests for targeting rejection, 10–50% traffic exclusion, cross-SDK parity, and contaminated historical records.
4. Update the Codegen secret gate for current formats and isolate repository-controlled execution from all provider/GitHub credentials.
5. Rate-limit and gate registration before Argon2/DB work; move hashing off the event loop. Add pre-auth Ingestion protection.
6. Bind dependency-only databases to loopback.

### P1 — Before calling the OSS distribution complete

1. Decide and enforce product scope: implement disabled autonomous workflows or remove them from runtime schemas/UI/claims.
2. Ship a first-run LLM governance/bootstrap workflow with a Docker-valid local endpoint and usable budget/policy templates.
3. Ship auditable role/authority management accessible to container operators.
4. Add Config outbox retention, guardrail predicate pushdown, writer discovery caching, pool validation, and capacity metrics.
5. Align Python/Node CI and image runtimes; lock and hash the full Codegen worker graph.
6. Publish exact service images and add image scan, SBOM, signing/provenance, and license checks.
7. Fix Admin cross-record editor state and make `make check` deterministic.
8. Make Python SDK validation identical to Ingestion and bound exposure deduplication.

### P2 — Before a production-ready/GA claim

1. Add data-completeness watermarks and lag-aware experiment finality.
2. Add real ClickHouse coverage for every query family and large-history guardrail tests.
3. Add real Postgres Agents tests, controlled Codegen sandbox tests, provider broker tests, and Agent-to-Codegen end-to-end smoke.
4. Add Playwright-style browser E2E, accessibility, CSP/external-URL, cookieless, SPA navigation, offline/cache-expiry, and unload tests.
5. Define a horizontally scalable Config architecture and load-test Ingestion auth, SSE fanout, writer discovery, outbox retention, and Admin proxy limits.
6. Run services as non-root with read-only filesystems, dropped capabilities, no-new-privileges, resource limits, and stable public error contracts.
7. Add service-level metrics, tracing/correlation IDs, queue/outbox/write lag, decision-data watermarks, and a complete operator health view.

## Release recommendation

Do not tag this snapshot as a production-ready OSS release while experiments and autonomous Codegen are enabled as supported features.

A defensible developer-preview release requires, at minimum, C-01, C-02, H-07, H-08, H-09, H-10, and H-13 to be fixed; the LLM bootstrap and actual supported workflow set must be made truthful and usable; and partial/high-risk capabilities must default off. The preview should explicitly scope support to the tested core path until Agents and Codegen receive clean-install, real-runtime release gates.

## Audit limitations

- Documentation contents were not reviewed, by explicit instruction.
- No external LLM provider, real GitHub publication, hostile repository, penetration test, browser automation suite, or production load test was run.
- Fresh smokes intentionally excluded Agents and Codegen.
- Only one authoritative experiment SQL path ran against a live ClickHouse instance; two broader opt-in Query execution tests were skipped.
- Findings are based on the named snapshot and should be revalidated after any contract or migration change.
