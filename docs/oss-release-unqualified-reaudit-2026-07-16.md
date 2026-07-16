# APDL OSS unqualified post-remediation audit — 2026-07-16

## Executive decision

**Verdict: NO-GO for an unqualified OSS release.**

The remediation branch materially improves the release: the full local check is green, both fresh-stack smoke paths pass, the selected Config timestamp defect is fixed, package publication is resumable, default browser collection is denied, SDK flag snapshots are tenant-bound, the Codegen controller contains a verified Docker CLI, and Agents now refuses code-backed approvals when Codegen declares the capability disabled.

Those improvements do not make the current application safe to release without qualification. The fresh audit found release-blocking behavior that the green suites do not exercise:

1. An explicit browser consent denial can be overridden by an older persisted grant, causing collection before the host can revoke it.
2. The migration guard is a same-Compose point-in-time snapshot, not an authoritative maintenance fence; supported host-run writers and start-after-check races remain.
3. A valid Query `contains` selector fails on the exact pinned ClickHouse 24.1 image.
4. Query's typed JSON selectors silently match values of the wrong JSON type.
5. The shipped JavaScript UI renderer executes HTML event handlers supplied in modal content and accepts unsafe link schemes.
6. Experiment enrollment can change after launch, launched experiments can be physically deleted, exposure retries can duplicate, and final analysis can ignore unknown variants.
7. Codegen's global readiness capability can claim changeset creation is available when project-specific creation must fail.
8. Agents can run a newly triggered workflow concurrently with an older workflow resuming after approval for the same project.

The current tree is credible as a **single-node developer preview for controlled evaluation**, provided the remaining privacy, analytics-correctness, migration, and autonomous-execution limitations are stated prominently. It is not ready to be presented as a generally safe, complete, production-capable OSS platform.

## Audit identity and constraints

- Audit date: 2026-07-16, America/Toronto.
- Branch: `fix/oss-release-highest-priority-blockers`.
- Audited commit: `ddd79d06f5a0f6e292efde01108a6fea4057a6a8`.
- Branch base: `3ff383e6ee5dfadce34e67cde5d784bff9fb1e2f` (`fix/database-audit-db-01-02`).
- Relative position at audit time: 76 commits ahead of `origin/main` and 44 commits behind it; 245 commits ahead of the stale local `main` and 0 behind it.
- Existing project Markdown files were not opened or used. This new report was produced from source code, configuration, migrations, executable tests, built artifacts, container behavior, Git state, and registry/API checks.
- Documentation quality, accuracy, contributor onboarding, support policy, security-reporting prose, and other Markdown-governed OSS material are intentionally outside the evidence base because of the no-Markdown-read constraint.
- The Python packed-artifact path was not rerun during the independent re-audit because the package builder consumes the SDK Markdown readme. Source tests, metadata-independent package checks, and the previously green root release verifier were used instead.

## Method

The audit treated “unqualified release” as the full product claim, not merely “the test suite passes.” It covered:

- every public SDK and service;
- the event path from browser/server client through Redis and ClickHouse;
- flag and experiment lifecycle consistency;
- Query SQL generation and execution on the pinned database image;
- Agents approval, lease, resume, and durable-effect behavior;
- Codegen repository authority, capability truthfulness, sandbox/runtime packaging, and publication prerequisites;
- Admin API/UI route, role, schema, and readiness alignment;
- PostgreSQL and ClickHouse migration safety;
- fresh installation and experiment lifecycle integration;
- CI, dependency auditing, container/release packaging, licensing, public-repository state, and registry publication state.

Static tracing was paired with focused adversarial probes, exact-engine SQL execution, a built-SDK privacy reproduction, a built-SDK DOM injection reproduction, full component suites, fresh Docker volume tests, and a real Codegen controller image build.

## Remediation branch review

| Commit | Intended blocker | Re-audit result |
|---|---|---|
| `5202967` | Preserve typed Config experiment timestamps | **Fixed.** Database `datetime` values stay typed through mutation bindings and are serialized only at the HTTP boundary. |
| `cf5dbaa` | Private, tenant-bound SDK initialization | **Partially fixed.** Default consent and capture are denied, import is side-effect-free, and foreign-project flag snapshots are rejected. Persisted consent can still override an explicit denial, and state is not deployment-scoped. |
| `7889e82` | Align the core smoke/readiness contract | **Fixed for the tested contract.** Admin owns aggregate readiness, exact schema assertions pass, and the fresh smoke exercises the supported core path. |
| `96ee190` | Make immutable registry publication resumable | **Fixed in code and unit tests.** Absent artifacts publish; identical artifacts skip; mismatches fail. The workflow has not yet been proven by a real successful registry publication/rerun. |
| `046a86f` | Enforce migration quiescence | **Partial mitigation only.** Known services in one Compose project are detected, and dangerous direct runners require an explicit assertion. Host processes, other projects/orchestrators, and start-after-check races remain. |
| `fded1d2` | Ensure Codegen has a Docker CLI | **Fixed.** Docker CLI 27.5.1 is architecture-pinned, checksum-verified, installed, and executable by the runtime user in the built controller. |
| `ddd79d0` | Gate code-backed approvals on Codegen capability | **Partially fixed.** Disabled/unreachable Codegen now blocks persistence with 424. The advertised capability is global and omits tenant/runtime prerequisites, so it can still be a false positive. |

The commits are separated by real review boundaries and contain their corresponding regression tests. No unrelated existing audit file was staged or modified.

## Release blockers

### RA-01 — Critical — persisted consent overrides an explicit current denial

`ConsentManager` constructs the project-only key and restores it before considering the supplied state:

- `sdk/javascript/src/privacy/consent.ts:17-29`
- `sdk/javascript/src/privacy/consent.ts:23`

The restored state immediately controls enabled subsystems and synchronous startup:

- `sdk/javascript/src/core/client.ts:121-132`
- `sdk/javascript/src/core/client.ts:404-410`

Built-package reproduction:

- Existing storage: `apdl_consent_apdl` with all three categories `true`.
- New explicit configuration: all categories `false`, `autoCapture: true`.
- Observed effective consent: all categories `true`.
- Observed queued event before the caller could revoke: `page`.

This violates the expected authority of a current host/CMP decision and turns an old grant into collection despite an explicit denial.

The same issue crosses deployments. Consent, session, flag cache, anonymous identity, and offline storage keys use the project ID without binding the APDL endpoint:

- `sdk/javascript/src/privacy/consent.ts:23`
- `sdk/javascript/src/capture/session.ts:23-29`
- `sdk/javascript/src/core/client.ts:744-749`
- `sdk/javascript/src/core/storage.ts:49-67`

Two endpoints using the same project ID can therefore inherit each other's browser state.

Required closure:

- Preserve whether consent was explicitly supplied.
- Make explicit current consent authoritative before any subsystem starts.
- Bind all persistent state to a canonical deployment origin plus project ID.
- Reject or explicitly migrate legacy project-only keys.
- Add built-artifact tests for stale-grant/explicit-deny and same-project/different-endpoint cases.

### RA-02 — Critical — migration quiescence is not an authoritative fence

The new guard discovers Compose ownership from an anchor container and scans only running services carrying that exact project label:

- `scripts/migration_quiescence.py:42-69`
- `scripts/migration_quiescence.py:102-105`

The repository simultaneously advertises host-run services and a host-run pipeline:

- `Makefile:192-237`
- `Makefile:376-377`
- `Makefile:393-398`

Those processes, another Compose project, Kubernetes workloads, or remote writers are invisible. There is also a time-of-check/time-of-use window:

- ClickHouse check: `scripts/init-clickhouse.sh:66-68`; apply begins at `scripts/init-clickhouse.sh:70-82`.
- PostgreSQL check: `scripts/init-postgres.sh:60-61`; image build and apply begin at `scripts/init-postgres.sh:63-73`.

The relevant migrations are explicitly unsafe with concurrent work:

- event snapshot/exchange/drop: `pipeline/clickhouse/migrations/005_events_canonical_upgrade.sql:62-108`;
- exposure projection drop/backfill/recreation: `pipeline/clickhouse/migrations/006_feature_flag_exposures.sql:4-55`;
- health projection drop/backfill/recreation: `pipeline/clickhouse/migrations/007_frontend_health_events.sql:4-59`;
- in-flight autonomous execution reconciliation: `pipeline/postgres/migrations/028_admin_execution_authority.sql:280-304`.

The direct runners finally trust environment assertions:

- `pipeline/clickhouse/migrate.py:168-183`
- `pipeline/postgres/migrate.py:112-127`

`make dev-core` is safer because it stops the known Compose services first, but that is a local orchestration convention rather than a global invariant.

Required closure:

- Hold a shared/exclusive maintenance inhibitor for the entire drain-and-migrate interval.
- Make every supported application and worker entrypoint participate.
- Move build/pull work before the final check and recheck immediately before apply.
- For general self-hosting, use database-backed coordination or online shadow/catch-up migrations with atomic cutover. A Docker process snapshot cannot establish global quiescence.

### RA-03 — High — shipped SDK UI rendering permits DOM script execution

The public renderer accepts `UIConfig` and the built-in modal assigns caller-provided content to `innerHTML`:

- `sdk/javascript/src/core/client.ts:237-242`
- `sdk/javascript/src/ui/components/modal.ts:13-16`
- `sdk/javascript/src/ui/components/modal.ts:111-120`

Other built-in components accept arbitrary `href` values without a safe-scheme policy:

- `sdk/javascript/src/ui/components/banner.ts:62-65`
- `sdk/javascript/src/ui/components/card.ts:101-104`
- `sdk/javascript/src/ui/components/cta-button.ts:42-47`
- `sdk/javascript/src/ui/components/modal.ts:153-156`

Exact built-artifact probe rendered a modal body containing an image `onerror` handler, dispatched the error event, and observed `xss_executed true` in the host window.

Personalization consent is not an HTML trust boundary. Any remotely authored, agent-authored, CMS-authored, or otherwise insufficiently trusted configuration can execute in the customer application's origin.

Required closure:

- Default to text-only content.
- If rich content is essential, use a small audited allowlist sanitizer and Trusted Types-compatible sinks.
- Permit only explicit URL schemes and reject `javascript:`, unsafe `data:`, control characters, and ambiguous relative forms according to one canonical policy.
- Add built-browser adversarial tests for event handlers, scriptable SVG/data URLs, unsafe links, and malformed schemes.

### RA-04 — High — valid `contains` selectors fail on the shipped ClickHouse

Query generates `positionCaseSensitive(...)` for a supported selector:

- `services/query/app/clickhouse/selectors.py:77-81`

The exact runtime is pinned to ClickHouse 24.1.8.22:

- `infra/docker/docker-compose.yml:16-17`

That engine returns error code 46, `UNKNOWN_FUNCTION`, for `positionCaseSensitive`. The failure was reproduced both as a scalar query and by executing Query's compiled selector SQL against a fresh schema on the exact digest.

The current test only inspects SQL text:

- `services/query/tests/test_queries.py:63-73`

Required closure:

- Generate a function supported by the pinned engine, such as the canonical case-sensitive `position(...)` form.
- Add exact-image execution tests for every accepted selector operator, not string-only SQL assertions.

### RA-05 — High — typed Query selectors silently coerce wrong JSON types

Selectors extract directly with `JSONExtractString`, `JSONExtractFloat`, and `JSONExtractBool`:

- `services/query/app/clickhouse/selectors.py:67-119`

On the pinned engine, the audit observed all of the following matching:

- JSON string `"5"` as numeric `5`;
- JSON number `1` as string `"1"`;
- JSON number `1` as boolean `true`.

This affects counts, timeseries, funnels, retention, cohorts, experiments, and guardrails wherever a selector is accepted. Typed breakdown SQL already demonstrates a safer `JSONType`-guarded approach:

- `services/query/app/clickhouse/queries.py:136-202`

Required closure: every typed selector must assert the canonical JSON type before extracting and comparing it, with exact-engine cross-type rejection tests.

### RA-06 — High — experiment enrollment remains mutable after launch

`traffic_percentage` and `targeting_rules` define who enters an experiment, but both remain accepted after draft:

- request models: `services/config/app/models/schemas.py:768-796`;
- router freeze set: `services/config/app/routers/admin.py:452-461`;
- post-launch update path: `services/config/app/routers/admin.py:715-745`;
- mutation freeze set: `services/config/app/store/mutations.py:72-79`;
- backing-flag mutation: `services/config/app/store/mutations.py:628-650`.

Changing enrollment after exposure begins invalidates the causal population and can produce a decision snapshot from a mixed assignment regime.

Required closure: freeze both fields after draft in the router and the database-authoritative mutation layer. Any intentional adaptive allocation needs a separate canonical experiment design and analysis contract.

### RA-07 — High — launched experiment authority can be deleted

The delete route has no lifecycle guard:

- `services/config/app/routers/admin.py:816-844`

The mutation archives the flag but physically deletes the experiment row:

- `services/config/app/store/mutations.py:973-1028`

Running and completed experiment definitions, statistical plans, and version authority can disappear while historical exposures remain. Query analysis then loses its canonical contract.

Required closure: permit hard deletion only for drafts. Launched experiments need an immutable archived/tombstoned row and durable lifecycle/audit history.

### RA-08 — High — server-evaluation retries duplicate exposure events by default

`log_exposure` defaults to true while `message_id` defaults to an empty string:

- `services/config/app/models/schemas.py:396-406`

The route invents a new UUID when the caller omits the ID:

- `services/config/app/routers/evaluate.py:143-184`

Durable deduplication only works when the same message ID is retried:

- `services/config/app/store/mutations.py:1096-1137`

A lost response followed by a normal retry therefore records a second exposure and can bias experiment counts.

Required closure: require a stable `message_id` when exposure logging is enabled, or define one strict idempotency-key contract.

### RA-09 — High — unknown experiment variants do not prevent final analysis

Query counts and skips unknown variant actors:

- `services/query/app/routers/experiments.py:81-163`
- skip path: `services/query/app/routers/experiments.py:142-145`

Finality checks do not reject `unknown_variant_actors`:

- `services/query/app/routers/experiments.py:392-423`

A decision snapshot is still emitted:

- `services/query/app/routers/experiments.py:447-450`

The existing regression explicitly accepts finality with unknown actors at `services/query/tests/test_endpoints.py:1085-1128`.

Required closure: unknown variant exposure must make analysis non-final and produce an explicit, machine-readable reason.

### RA-10 — High — Codegen capability is globally optimistic, not tenant-executable

Agents probes a global unauthenticated readiness response without a project:

- `services/agents/app/routers/approvals.py:158-169`
- `services/agents/app/readiness.py:235-245`

Codegen marks changeset creation available from rollout stage plus `SELECT 1`:

- `services/codegen/app/main.py:462-479`

It does not prove the prerequisites later enforced by creation or execution:

- GitHub App credentials: `services/codegen/app/github/app_auth.py:104-116`, `218-240`;
- global/project kill switches: `services/codegen/app/safety/killswitch.py:16-22`;
- project repository grant: `services/codegen/app/routers/changesets.py:86-93`, `136-140`;
- provider, worker, sandbox, and project-specific runtime availability.

The audit reproduced `changeset_creation=available` with no GitHub App credentials and with `CODEGEN_KILL_SWITCH=true`.

Agents can then count a Codegen request as a successful effect before the asynchronous worker immediately abandons or errors it:

- effect result: `services/agents/app/store/approval_effects.py:920-966`;
- later failure: `services/codegen/app/jobs/runner.py:238-261`, `573-586`.

Most 4xx outcomes become permanent/manual-intervention failures at `services/agents/app/store/approval_effects.py:1004-1023`.

Required closure: use an authenticated tenant-scoped capability check covering stage, kill switches, grant, GitHub configuration, provider, worker, and runtime prerequisites, and revalidate synchronously inside Codegen before row creation.

### RA-11 — High — same-project Agents serialization breaks across approval resume

Trigger serialization excludes runs in `waiting_approval` and `approval_queued`:

- `services/agents/app/routers/triggers.py:99-125`

Approval enqueue sets `approval_queued` and clears the run lease:

- `services/agents/app/store/approval_effects.py:742-750`

A new run can start for the project. When approval effects finish, the old run becomes resumable:

- `services/agents/app/store/approval_effects.py:1026-1058`

The dispatcher schedules eligible runs independently, and leases are keyed by run ID rather than project:

- `services/agents/app/store/run_dispatcher.py:169-207`, `231-255`;
- `services/agents/app/store/run_leases.py:65-99`.

The old resumed run and new run can concurrently spend budget and generate stale or duplicate mutations for one project.

Required closure: create a database-authoritative project execution lane that covers fresh, waiting, approval-effect, and resumed states, with transactional race tests.

### RA-12 — High — a poison Config outbox row blocks a tenant lane forever

Outbox ordering prevents later rows in the same tenant/kind lane from overtaking the oldest pending row:

- `services/config/app/outbox.py:64-120`

Failures retry forever with no terminal classification or dead-letter state:

- `services/config/app/outbox.py:134-147`
- `services/config/app/outbox.py:263-299`

Readiness checks dependency connectivity, not oldest pending age, attempts, lag, or poisoned state:

- `services/config/app/main.py:263-290`

One permanently malformed payload or non-retryable downstream error can freeze flag distribution or exposure delivery for the project while readiness remains green.

Required closure: classify permanent failures, quarantine/DLQ them with evidence, cap attempts, expose lag/age metrics, and define readiness degradation thresholds.

## Additional high-priority privacy, durability, and correctness gaps

### RA-13 — full client IP is retained without a runtime consumer

Ingestion adds the full source IP to every accepted event at `services/ingestion/app/routers/events.py:129-140`. The canonical event table stores it for 12 months at `pipeline/clickhouse/migrations/001_events.sql:2-26`.

No application feature was found consuming `events.ip`; rate limiting already hashes IP identity at `services/ingestion/app/middleware/rate_limit.py:120-123`, `151-175`.

Default to no stored IP, or require an explicit opt-in with truncation/anonymization and a separate retention contract.

### RA-14 — derived personal-data tables have no retention boundary

The source `events` table has a 12-month TTL:

- `pipeline/clickhouse/migrations/001_events.sql:22-26`
- `pipeline/clickhouse/migrations/005_events_canonical_upgrade.sql:54-60`

Derived tables retain user ID, anonymous ID, session ID, page, and diagnostic data without TTLs:

- `pipeline/clickhouse/migrations/006_feature_flag_exposures.sql:7-29`
- `pipeline/clickhouse/migrations/007_frontend_health_events.sql:7-31`

No purge path was found. The projections can outlive source consent/retention expectations indefinitely and grow without bound.

### RA-15 — `cookieless` is deterministic fingerprinting and can split identity at startup

The identifier hashes user agent, language, hardware concurrency, screen properties, timezone, date, and the public browser key:

- `sdk/javascript/src/privacy/cookieless.ts:19-25`
- `sdk/javascript/src/privacy/cookieless.ts:44-64`
- salt use: `sdk/javascript/src/core/client.ts:488-492`

Capture starts before the asynchronous hash is ready at `sdk/javascript/src/core/client.ts:404-424`; a temporary random identity is used at `sdk/javascript/src/core/client.ts:500-504`. The audit observed an immediate event and a later event using different anonymous IDs. `reset()` also writes an ordinary anonymous UUID through the persistent path at `sdk/javascript/src/core/client.ts:330-336`, `523-529`.

Use a random non-persistent session identifier or a server-issued rotating ID. Do not label deterministic browser fingerprinting as cookieless privacy.

### RA-16 — valid-format invalid credentials can exhaust authentication pools before quotas

Any syntactically valid random key reaches PostgreSQL before authenticated rate limiting:

- Ingestion lookup: `services/ingestion/app/auth.py:89-104`;
- Config lookup: `services/config/app/auth.py:98-113`;
- Ingestion quota begins after authentication: `services/ingestion/app/routers/events.py:47-55`;
- Ingestion pool size: `services/ingestion/app/main.py:76-84`;
- Config pool default: `services/config/app/main.py:66-72`.

The shipped gateway has no pre-auth rate controls at `infra/docker/gateway/nginx.conf:5-45`. An attacker can generate unlimited valid-format, invalid hashes and occupy the small pools without entering Redis quotas.

Add bounded global/IP admission before database authentication and a carefully bounded negative-credential cache.

### RA-17 — Python exposure dedupe is unbounded and semantically process-scoped

The Python SDK creates a client-lifetime set and one client-lifetime session ID:

- `sdk/python/apdl/client.py:71-79`

Each identity/flag/version/variant key remains forever:

- `sdk/python/apdl/client.py:403-453`

The key omits page, component, and end-user session. Long-lived servers can grow memory without bound, suppress legitimate exposures across contexts, and assign all end users one synthetic session.

Use caller-owned exposure/session IDs or a bounded TTL/LRU whose semantics are explicit.

### RA-18 — singleton SDK initialization silently ignores conflicting configurations

The JavaScript global registry is keyed only by browser client key and returns the first instance without comparing endpoint, consent, persistence, capture, or privacy settings:

- `sdk/javascript/src/core/init.ts:55-74`

The audit reproduced the same instance being returned for the same key with different endpoints and opposite consent/capture settings. This can route data to the wrong deployment and ignore a stricter current configuration.

Bind the singleton key to the full canonical configuration or throw on any conflicting reinitialization.

## Service-by-service assessment

### JavaScript SDK

**Relevance:** essential browser edge for telemetry, client-side flag evaluation, consent, SSE distribution, and optional UI personalization.

**Strengths:** strict event and flag parsing, stable queued message IDs, evaluator parity, explicit delivery outcomes, privacy scrubbing, now-denied capture defaults, tenant-bound flag envelopes, and import-side-effect-free packaging. The test/build/package surface is broad.

**Incomplete or defective:** RA-01, RA-03, RA-15, and RA-18 are direct release blockers or high privacy/security defects. SSE reconnects on 401/403 and buffers until a frame boundary without a hard frame limit at `sdk/javascript/src/sse/connection.ts:67-145`; Config emits terminal `stream_error` states that the handler ignores at `sdk/javascript/src/sse/handlers.ts:35-62` and `services/config/app/sse/broadcaster.py:338-346`. Dynamic environment lookup leaves `process.env[name]` in browser bundles and does not implement Vite semantics (`sdk/javascript/src/core/env.ts:13-37`), despite React adapter claims at `sdk/javascript/src/react/index.ts:27-30`. Unknown UI properties are silently accepted at `sdk/javascript/src/ui/registry.ts:60-76`, contrary to the strict canonical schema rule.

**Verdict:** no-go.

### Python SDK

**Relevance:** server-side event delivery and local flag evaluation.

**Strengths:** strict typed models, canonical evaluator behavior, tenant-bound flag refresh, stable event IDs, retry reporting, and strong coverage.

**Incomplete or defective:** RA-17. Construction performs a synchronous initial flag fetch at `sdk/python/apdl/client.py:90-93` with a default ten-second timeout (`sdk/python/apdl/config.py:29`, `63-65`), which can block application startup. Retryable events are memory-only and shutdown returns undelivered snapshots rather than restart-durable state (`sdk/python/apdl/queue.py:53-71`, `261-268`; `sdk/python/apdl/client.py:299-332`).

**Verdict:** usable alpha SDK, not an unqualified durable analytics client.

### Ingestion

**Relevance:** essential public write edge and tenant authority for all event data.

**Strengths:** database-owned tenant/role authority, exact browser-role restriction, bounded canonical JSON, batch validation, auto-capture privacy stripping, hierarchical atomic quotas after authentication, atomic bounded Redis admission, and fail-closed dependency behavior.

**Incomplete or defective:** RA-13 and RA-16. Stream capacity is bounded per project but not globally (`services/ingestion/app/streaming/redis_producer.py:12-35`, `54-78`); enough valid tenant streams can exhaust shared Redis memory. `/health` combines liveness and dependency readiness (`services/ingestion/app/main.py:113-127`). The real Redis quota contract remains environment-dependent and was skipped in the independent service run.

**Verdict:** strong core path; no-go as an unqualified internet-facing service until pre-auth and privacy/operability gaps are closed.

### Config

**Relevance:** canonical flag, experiment, evaluation, cache, and distribution authority.

**Strengths:** strict request models, startup schema checks, transactionally coupled flag/experiment/audit/outbox writes, project versions, version-aware cache invalidation, bounded SSE admission, credential revalidation, and the fixed timestamp boundary.

**Incomplete or defective:** RA-06, RA-07, RA-08, and RA-12. Flag creation accepts path-hostile keys such as `a/b` because `FlagCreate.key` has length-only validation at `services/config/app/models/schemas.py:426-428`, while operations address flags through `/flags/{key}`. Admin mutations lack a common raw-body bound. Server evaluation performs database/outbox work per default-logged request without a request quota. The service is deliberately single-replica through a lifetime advisory lock at `services/config/app/main.py:30-47`, `72-85`.

**Verdict:** functionally central and substantially engineered, but experiment integrity and delivery durability remain release-blocking.

### Query

**Relevance:** essential analytical read plane for events, funnels, retention, cohorts, experiments, and guardrails.

**Strengths:** tenant authorization, bounded date ranges, query concurrency and execution budgets, parameterized SQL, production experiment analysis, and broad endpoint coverage. Exact-engine audit execution passed count, timeseries, breakdown, catalog, funnel, daily/weekly retention, cohort, experiment, and guardrail families apart from `contains`.

**Incomplete or defective:** RA-04, RA-05, and RA-09. Cohorts are event-property segmentation rather than stable actor cohorts, so changing properties can place one actor in multiple cohorts (`services/query/app/clickhouse/queries.py:405-453`; `services/query/app/routers/cohorts.py:25-75`). A separate statistics implementation in `services/query/app/models/statistics.py:1-291` is test-only while production uses `services/query/app/routers/experiments.py:166-265`, `425-450`, creating competing semantics.

**Verdict:** no-go due hard runtime failure and silent correctness defects.

### Redis-to-ClickHouse writer

**Relevance:** essential durability bridge from accepted events to analytics storage.

**Strengths:** acknowledgements/deletion occur only after ClickHouse or DLQ durability, inserts retry with backoff, terminal rows are isolated, pending work is reclaimed, and stream pressure is observed.

**Incomplete or defective:** migration coordination in RA-02. Consumer identity is `worker-{pid}` at `pipeline/redis/clickhouse_writer.py:182`; replicas commonly all run as PID 1 and can collide. Shared Redis capacity is observed but not prevented globally (`pipeline/redis/clickhouse_writer.py:682-712`). The ClickHouse `config_version` projection casts to `UInt32` while Ingestion accepts any non-negative integer (`services/ingestion/app/validation/schema.py:440-446`, `604-605`; `pipeline/clickhouse/migrations/006_feature_flag_exposures.sql:21`, `46`, `71`).

**Verdict:** strong normal-path durability, incomplete multi-replica and upgrade safety.

### ClickHouse schema and migrations

**Relevance:** canonical analytical storage and projection layer.

**Strengths:** contiguous checksummed ledger, migration authority checks, fresh and legacy-static upgrade coverage, rerun idempotence, canonical table retirement, and source event TTL.

**Incomplete or defective:** RA-02 and RA-14. The `sessions` table is created and upgraded (`pipeline/clickhouse/migrations/002_sessions.sql:1-19`; `005_events_canonical_upgrade.sql:110-166`) but has no production writer or reader and no TTL. Migration tooling is tied to `docker exec` and a Compose container (`pipeline/clickhouse/migrate.py:187-234`, `337-356`; `scripts/init-clickhouse.sh:40-82`), leaving no supported remote/Kubernetes upgrade path.

**Verdict:** suitable for controlled fresh installs; not general self-hosted upgrade-ready.

### PostgreSQL schema and migrations

**Relevance:** authority for credentials, Admin identity, flags, experiments, Agents governance, and Codegen state.

**Strengths:** contiguous immutable checksummed ledger, transactional migrations, migrator advisory serialization, schema ownership outside service startup, execution authority, strict provenance, and fresh-database rejection for unsupported unversioned schemas.

**Incomplete or defective:** RA-02. Some statistical-plan invariants remain application-only: the database checks basic shape/ranges at `pipeline/postgres/migrations/018_experiment_statistical_plan.sql:59-120`, while direction/effect/sample feasibility lives in Config (`services/config/app/models/schemas.py:579-674`; `services/config/app/experiments/analysis.py:37-77`). Direct database writes can therefore persist plans the application considers invalid.

**Verdict:** mechanically strong, but the offline execution cutover is not globally enforced.

### Agents

**Relevance:** core orchestration and governance plane for the advertised autonomous loop.

**Strengths:** tenant authority, durable runs and approvals, effect outbox, leases, audit evidence, safety validation, LLM budgets/quotas, provider governance, project execution authorization, and strong failure-path coverage.

**Incomplete or defective:** RA-10 and RA-11. Several advertised graph families are explicitly unavailable: personalization lacks storage/delivery/SDK contracts (`services/agents/app/graphs/personalization.py:1-6`, `29-47`), experiment evaluation is read-only and disabled (`services/agents/app/graphs/experiment_evaluation.py:1-5`, `17-27`), automatic health rollback is unavailable (`services/agents/app/safety/rollback.py:1-36`), and feature proposal is disabled (`services/agents/app/graphs/feature_proposal.py:132-158`). Nested custom-agent selectors accept unknown fields at `services/agents/app/framework/tool_catalog.py:33-51`.

**Verdict:** substantial governance foundation, but the advertised autonomous lifecycle and project serialization are incomplete.

### Codegen

**Relevance:** required “hands” for code-backed autonomous effects.

**Strengths:** repository grants, scoped/JIT GitHub tokens, fail-closed rollout stages, immutable evaluated-image identity in reviewed flows, network isolation policy, safety-policy digests, durable publication recovery, and now a verified Docker CLI.

**Incomplete or defective:** RA-10. The controller still starts from mutable `python:3.12-slim`, uses live apt/NodeSource inputs, and performs unlocked `pip install .` (`services/codegen/Dockerfile:1`, `16-36`). The worker uses mutable bases/live sources/unversioned `uv` (`services/codegen/Dockerfile.worker:30`, `40`, `49-60`). Dependency auditing excludes the privileged `agent` extra containing Aider (`scripts/audit_dependencies.sh:36-48`), and CI describes Codegen as source-only (`.github/workflows/ci.yml:194-208`).

**Verdict:** sophisticated safety design, but capability truthfulness and privileged runtime reproducibility/auditing prevent an unqualified release.

### Admin API

**Relevance:** necessary human/session boundary and BFF that keeps project credentials out of the SPA.

**Strengths:** CSRF/origin enforcement, secure session model, login-risk controls, tenant roles, ephemeral upstream credentials, mutation audit, bounded upstream timeouts, and no public OpenAPI surface.

**Incomplete or defective:** readiness checks only `body.status` and therefore calls disabled Codegen ready (`services/admin-api/app/main.py:23-26`, `46-68`, `119-160`). The proxy allowlist blocks implemented cancel, tenant-policy, and capability routes and retains a nonexistent merge rule (`services/admin-api/app/proxy.py:347-387`). Startup creates a pool but performs no migration/schema assertion (`services/admin-api/app/main.py:71-85`). Registration performs synchronous Argon2 hashing inside an async database transaction (`services/admin-api/app/auth.py:373-400`), unlike login's offloaded verification (`services/admin-api/app/auth.py:290-299`).

**Verdict:** strong security boundary, incomplete contract coverage and fail-closed schema/readiness behavior.

### Admin UI

**Relevance:** primary operator surface for setup, flags, experiments, analytics, Agents, Codegen, and credentials.

**Strengths:** broad tested workflows, strict response schemas, explicit role-aware workspace state in many areas, bundle budget, production build, and good error surfaces.

**Incomplete or defective:** health omits Codegen capability state (`services/admin/src/api/health.ts:5-30`, `82-105`). Codegen revert/retry/abandon controls render without workspace-role gating (`services/admin/src/features/codegen/ChangesetsPage.tsx:34-82`, `195-214`; `ChangesetDetailPage.tsx:197-266`, `308-327`). Trigger fallback includes the server-disabled `feature_proposal` agent and initially selects all fallback agents (`services/admin/src/features/agents/TriggerPage.tsx:30-51`, `112-147`).

**Verdict:** broad alpha console; operator capability and permission affordances drift from backend authority.

### Gateway and deployment stack

**Relevance:** supported local front door and self-hosting composition.

**Strengths:** default host bindings are loopback, database/cache images are digest-pinned, Codegen is not host-published, Admin separates the SPA edge from its BFF, and fresh-stack startup is executable.

**Incomplete or defective:** most Python application containers run as root, including Ingestion, Config, Query, Agents, Admin API, and writer. Compose does not set read-only filesystems, capability drops, or resource limits. Most service directories lack `.dockerignore`, allowing local virtual environments, caches, tests, and accidental service-local files into build context. Local development credentials are embedded throughout the default Compose contract and must not be mistaken for production configuration. No backup/restore command or platform metrics endpoint was found in executable source.

**Verdict:** good local developer stack, not hardened production deployment packaging.

## OSS release engineering assessment

### Positive evidence

- The GitHub repository is public, uses the MIT license, has Issues enabled, and declares `main` as the default branch.
- Root and SDK license bytes are checked by the release verifier.
- CI covers every source package, dependency audits, package contracts, fresh core and experiment smokes, and a static ClickHouse upgrade path.
- JavaScript and Python SDK metadata share release version `0.3.0`; the Python classifier explicitly says Alpha.
- npm and PyPI artifacts are built once and passed between publish jobs.
- The new registry verifier accepts only absent or byte/digest-identical immutable artifacts and rejects mismatches.
- Dependency audits passed for supported runtime lock sets.
- Dependabot covers GitHub Actions, npm, pip, Docker, and Compose ecosystems.

### Release-engineering gaps

1. **No application distribution.** `release-manifest.json` requires `docker_images: []`; the release publishes only source plus two SDK packages. Self-hosters must build every service locally.
2. **No release exists yet.** At audit time there was no local/remote `v0.3.0` tag, npm returned 404 for `@apdl-oss/sdk@0.3.0`, and PyPI returned 404 for `apdl-sdk==0.3.0`. The resumable workflow is therefore unit-tested but not registry-proven.
3. **Mutable GitHub Action references.** Workflows use major/release tags such as `actions/checkout@v4`, `astral-sh/setup-uv@v6`, `pypa/gh-action-pypi-publish@release/v1`, and `softprops/action-gh-release@v2`, rather than immutable commit SHAs.
4. **No image/SBOM attestation path.** No application images, SBOM generation, image vulnerability scan, or container signature/provenance gate is present. npm provenance is enabled; comparable full-stack provenance is absent.
5. **Privileged Codegen runtime excluded.** The dependency gate audits only the Codegen offline API set and excludes the agent/Aider extra used by the privileged worker path.
6. **Exact-engine coverage is incomplete.** Fresh smokes did not exercise every accepted Query selector, allowing RA-04 and RA-05 through green release gates.
7. **Branch integration risk.** The audited stacked branch is far ahead of both local and remote `main`; a release must first preserve the exact tested stack while reconciling the remote divergence, then rerun all gates on the resulting commit.
8. **Documentation cannot be certified.** Because existing Markdown was intentionally not read, this audit cannot certify installation guidance, security reporting, support boundaries, upgrade instructions, architecture claims, contributor workflow, or release notes.

## Verification record

### Full gates

- `make check`: passed all 21 parallel jobs.
- `make build`: passed JavaScript SDK and Admin production builds.
- `make verify-release`: passed the strict manifest/version/license contract.
- `make audit-dependencies`: passed supported npm and Python runtime audits; Codegen's `agent` extra remains explicitly excluded.
- `make test-clickhouse-upgrade`: passed all 12 migrations, static legacy seed upgrade, rerun, ledger validation, and checksum-drift rejection.
- `make smoke-fresh`: passed from empty volumes, applying 12 ClickHouse and 30 PostgreSQL migrations, starting the core stack, checking Admin aggregate readiness, verifying browser roles, ingesting exactly one event, observing it through Query, and exercising a flag lifecycle.
- `make smoke-experiment-fresh`: passed from empty volumes with 71 events, running-state withholding, completion, production ClickHouse analysis, and cleanup.

### Component suites

| Component | Result |
|---|---|
| JavaScript SDK | 21 files / 374 tests passed; lint/typecheck/build passed |
| Python SDK | 257 source tests passed in the constraint-safe re-audit; Ruff passed |
| Ingestion | 177 passed, 1 real-Redis contract skipped because `APDL_TEST_REDIS_URL` was not set; Ruff passed |
| Config | 360 passed; Ruff passed |
| Query | 187 passed; Ruff passed |
| Agents | 433 passed; Ruff passed |
| Codegen | 771 passed; Ruff passed |
| Redis writer | 57 passed; Ruff passed |
| Admin API | 108 passed; Ruff passed; one upstream Starlette deprecation warning |
| Admin UI | 42 files / 354 tests passed; lint/typecheck/build and bundle budget passed |
| Script contracts | 34 passed |

### Dynamic probes

- Built Codegen controller: runtime user UID 1000; `/usr/local/bin/docker`; Docker 27.5.1 executed successfully.
- Exact pinned ClickHouse: all compiled Query families executed on an empty canonical schema except `contains`; cross-type extraction coercion was reproduced.
- Built JavaScript SDK: stale grant overrode explicit denial and queued a page event.
- Built JavaScript SDK: same project ID across different endpoints inherited consent state.
- Built JavaScript SDK: conflicting singleton configuration returned the same instance.
- Built JavaScript SDK: modal HTML event handler executed in the host DOM.
- Codegen readiness: reported changeset creation available without GitHub App credentials and with the kill switch enabled.
- Live registries: npm and PyPI both reported version 0.3.0 absent.

## Required release sequence

The minimum order for an unqualified release is:

1. Fix consent authority/deployment isolation and the SDK UI injection surface; add built-browser adversarial tests.
2. Replace snapshot-based migration quiescence with an authoritative maintenance protocol or online-safe migrations.
3. Fix Query's exact-engine selector function and JSON type guards; add exact-image execution coverage for the complete selector matrix.
4. Make experiment enrollment immutable after launch, preserve launched experiment authority, require exposure idempotency, and reject finality with unknown variants.
5. Make Codegen capability tenant-scoped and executable, and add a project-level Agents execution lane across approval resume.
6. Add Config outbox quarantine/lag health and align ClickHouse-derived retention with the source contract.
7. Close pre-auth admission, raw-IP/cookieless privacy, Python exposure-dedupe, and writer replica/global-capacity gaps.
8. Align Admin capability/readiness/routes/role affordances and add schema readiness.
9. Reproduce and audit the privileged Codegen runtime, pin workflow actions, add SBOM/image scanning/signing, and decide whether application images are part of the release.
10. Reconcile the stacked branch with current remote `main` without losing the tested commit chain, then rerun every gate and adversarial probe on the exact release commit.
11. Independently review all existing Markdown release, support, security, upgrade, and contributor material once the no-read constraint is lifted.
12. Publish to a staging or disposable version first, verify artifact identity and rerun recovery, then create the final immutable tag.

## Final assessment

The codebase is considerably stronger than a typical alpha monorepo: contracts are often strict, migrations are checksummed, failure paths are deliberately tested, core data flow is durable, the control plane has meaningful safety design, and fresh integration paths are executable.

The remaining defects sit exactly at the boundaries that an unqualified release must get right: current consent authority, browser-origin code execution, exact production database semantics, experiment validity, upgrade concurrency, tenant-specific autonomous capability, and same-project execution serialization. Green unit and smoke suites do not compensate for those failures.

**Release decision: NO-GO.** Continue to label the current tree as a controlled single-node developer preview until the release-blocking items above are closed and the exact post-fix commit passes another unqualified audit.
