# Findings register — OSS unqualified post-remediation re-audit (2026-07-16)

Tracking register for the findings in
[`docs/audits/oss-release-unqualified-reaudit-2026-07-16.md`](oss-release-unqualified-reaudit-2026-07-16.md).
The full report is canonical for evidence, reproductions, and exact line references;
this register exists to track remediation status per finding. When a fix merges,
flip its Status to `Fixed` and link the PR/commit.

- **Audit verdict:** NO-GO for an unqualified OSS release; current tree is a
  controlled single-node developer preview.
- **Audited commit:** `ddd79d0` on `fix/oss-release-highest-priority-blockers`
  (the tip of the remediation stack).
- **Integration status:** the audited stack merged into `main` on 2026-07-17 via
  PRs #98–#109 with all CI gates green, so every finding below applies to current
  `main` unless its Status says otherwise.
- **Register status as of:** 2026-07-22 — RA-01 through RA-12 have
  remediation PRs on cumulative branches but remain **Open** pending merge into
  `main` and independent re-audit. RA-13 through RA-18 remain
  **Open** and are outside this implementation pass.

## Pending remediation PR stack

The branches below are cumulative, based on `a94c40f` (`origin/main`). Each
branch contains its finding commit plus all preceding rows. These commits record
remediation work, not finding closure; every finding remains **Open** until the
stack is merged and independently re-audited.

| Order | Finding | PR | Cumulative branch | Finding commit |
|---:|---|---|---|---|
| 1 | RA-01 | [#111](https://github.com/kuvera-apdl/apdl/pull/111) | `audit/ra-01-sdk-consent-isolation` | `53cae5b` |
| 2 | RA-02 | [#112](https://github.com/kuvera-apdl/apdl/pull/112) | `audit/ra-02-database-maintenance-fence` | `6d91b36` |
| 3 | RA-03 | [#113](https://github.com/kuvera-apdl/apdl/pull/113) | `audit/ra-03-sdk-safe-rendering` | `2fb64b7` |
| 4 | RA-04 | [#114](https://github.com/kuvera-apdl/apdl/pull/114) | `audit/ra-04-clickhouse-selector-compatibility` | `4dfdd50` |
| 5 | RA-05 | [#115](https://github.com/kuvera-apdl/apdl/pull/115) | `audit/ra-05-strict-selector-types` | `f753848` |
| 6 | RA-06 | [#116](https://github.com/kuvera-apdl/apdl/pull/116) | `audit/ra-06-immutable-enrollment` | `62c56d8` |
| 7 | RA-07 | [#117](https://github.com/kuvera-apdl/apdl/pull/117) | `audit/ra-07-archive-experiment-authority` | `ee5489a` |
| 8 | RA-08 | [#118](https://github.com/kuvera-apdl/apdl/pull/118) | `audit/ra-08-stable-exposure-message-id` | `884a083` |
| 9 | RA-09 | [#119](https://github.com/kuvera-apdl/apdl/pull/119) | `audit/ra-09-unknown-variant-finality` | `d84fd28` |
| 10 | RA-10 | [#120](https://github.com/kuvera-apdl/apdl/pull/120) | `audit/ra-10-tenant-codegen-capability` | `b038c14` |
| 11 | RA-12 | [#121](https://github.com/kuvera-apdl/apdl/pull/121) | `audit/ra-12-config-outbox-quarantine` | `6d13be6` |
| 12 | RA-11 | [#122](https://github.com/kuvera-apdl/apdl/pull/122) | `audit/ra-11-agent-run-concurrency-lane` | `2c60f24` |

RA-12 intentionally precedes RA-11 in the stack because their PostgreSQL
migrations are respectively `033_config_outbox_quarantine.sql` and
`034_agent_project_execution_lane.sql`.

This pass covers the formal Critical and High entries in the register's
**Release blockers** table (RA-01–RA-12). RA-13–RA-18 remain important and
Open, but the register classifies them as **High-priority gaps**, not formal
High-severity entries in that table.

## Release blockers

| ID | Severity | Finding | Area | Status |
|---|---|---|---|---|
| RA-01 | Critical | Persisted consent overrides an explicit current denial; browser state is not deployment-scoped | JS SDK privacy | Open |
| RA-02 | Critical | Migration quiescence is a Compose snapshot, not an authoritative maintenance fence | Migrations / scripts | Open |
| RA-03 | High | Shipped SDK UI rendering permits DOM script execution (`innerHTML` modal content, unguarded `href` schemes) | JS SDK UI | Open |
| RA-04 | High | Valid `contains` selectors fail on the pinned ClickHouse 24.1 (`positionCaseSensitive` is `UNKNOWN_FUNCTION`) | Query ↔ ClickHouse | Open |
| RA-05 | High | Typed Query selectors silently coerce wrong JSON types (`"5"` matches `5`, `1` matches `true`) | Query selectors | Open |
| RA-06 | High | Experiment enrollment (`traffic_percentage`, `targeting_rules`) remains mutable after launch | Config experiments | Open |
| RA-07 | High | Launched experiment authority can be hard-deleted while exposures remain | Config experiments | Open |
| RA-08 | High | Server-evaluation retries duplicate exposure events (route invents a UUID when `message_id` is omitted) | Config evaluate | Open |
| RA-09 | High | Unknown experiment variants do not prevent final analysis or decision snapshots | Query experiments | Open |
| RA-10 | High | Codegen capability is globally optimistic (`available` with no GitHub App credentials and kill switch on), not tenant-executable | Agents ↔ Codegen | Open |
| RA-11 | High | Same-project Agents serialization breaks across approval resume (resumed + new run share one project concurrently) | Agents execution | Open |
| RA-12 | High | One poison Config outbox row blocks a tenant lane forever with readiness still green | Config outbox | Open |

## Additional privacy, durability, and correctness gaps

| ID | Severity | Finding | Area | Status |
|---|---|---|---|---|
| RA-13 | High-priority gap | Full client IP stored on every event for 12 months with no runtime consumer | Ingestion / events schema | Open |
| RA-14 | High-priority gap | Derived personal-data tables (exposures, health events) have no TTL or purge path | ClickHouse migrations | Open |
| RA-15 | High-priority gap | `cookieless` is deterministic device fingerprinting and can split identity at startup | JS SDK privacy | Open |
| RA-16 | High-priority gap | Valid-format invalid credentials reach PostgreSQL before any quota and can exhaust auth pools | Ingestion / Config / gateway | Open |
| RA-17 | High-priority gap | Python SDK exposure dedupe is unbounded, process-scoped, and assigns one synthetic session to all users | Python SDK | Open |
| RA-18 | High-priority gap | Singleton SDK init returns the first instance and silently ignores conflicting endpoint/consent/capture config | JS SDK init | Open |

## Required closure per finding (condensed)

- **RA-01** — preserve whether consent was explicitly supplied; explicit current
  consent wins before any subsystem starts; bind all persisted state to
  deployment origin + project ID; migrate/reject legacy project-only keys;
  built-artifact tests for stale-grant/explicit-deny and cross-endpoint cases.
- **RA-02** — hold a shared/exclusive maintenance inhibitor across the whole
  drain-and-migrate interval with every supported entrypoint participating;
  recheck immediately before apply; for general self-hosting use DB-backed
  coordination or online shadow/catch-up migrations.
- **RA-03** — default to text-only content; if rich content is essential, use a
  small audited element-and-attribute allowlist sanitizer with Trusted
  Types-compatible sinks; one canonical URL-scheme policy; built-browser
  adversarial tests (event handlers, scriptable SVG/`data:`, malformed schemes).
- **RA-04** — emit the canonical `position(...)` form supported by the pinned
  engine; exact-image execution tests for every accepted selector operator.
- **RA-05** — assert canonical `JSONType` before extracting/comparing in every
  typed selector; exact-engine cross-type rejection tests.
- **RA-06** — freeze enrollment fields after draft in both the router and the
  database-authoritative mutation layer.
- **RA-07** — hard delete only for drafts; launched experiments get an immutable
  archived/tombstoned row with durable lifecycle/audit history.
- **RA-08** — require a stable `message_id` whenever exposure logging is
  enabled, or define one strict idempotency-key contract.
- **RA-09** — unknown variant exposure makes analysis non-final with an
  explicit machine-readable reason.
- **RA-10** — authenticated, tenant-scoped capability check covering stage,
  kill switches, repo grant, GitHub App config, provider, worker, and runtime;
  revalidate synchronously inside Codegen before row creation.
- **RA-11** — database-authoritative per-project execution lane spanning fresh,
  waiting, approval-effect, and resumed states, with transactional race tests.
- **RA-12** — classify permanent failures; quarantine/DLQ with evidence; cap
  attempts; expose lag/age metrics; readiness degrades past thresholds.
- **RA-13** — default to no stored IP, or explicit opt-in with
  truncation/anonymization and its own retention contract.
- **RA-14** — TTLs/purge paths for derived tables aligned with the source
  `events` retention contract.
- **RA-15** — random non-persistent session identifier or server-issued
  rotating ID; stop labeling deterministic fingerprinting as cookieless privacy.
- **RA-16** — bounded global/IP admission before database authentication plus a
  carefully bounded negative-credential cache.
- **RA-17** — caller-owned exposure/session IDs or a bounded TTL/LRU with
  explicit semantics.
- **RA-18** — bind the singleton key to the full canonical configuration or
  throw on conflicting reinitialization.

## Release-engineering gaps

| # | Gap | Status |
|---|---|---|
| 1 | No application distribution (`docker_images: []`; source + two SDK packages only) | Open (declared policy for the v0.3.0 line) |
| 2 | No `v0.3.0` release published yet; resumable publish workflow unproven against real registries | Open |
| 3 | GitHub Actions referenced by mutable major/release tags, not commit SHAs | Open |
| 4 | No SBOM, image scanning, or container signing/provenance path | Open |
| 5 | Dependency gate excludes the privileged Codegen agent/Aider extra | Open (explicitly excluded by SUPPORT.md) |
| 6 | Fresh smokes did not exercise every accepted Query selector (let RA-04/RA-05 through) | Open — remediation exists in the RA-04/RA-05 stack layers; pending merge and independent re-audit |
| 7 | Stacked branch far ahead of `main`; reconciliation required before release | **Closed 2026-07-17** — PRs #98–#109 merged with per-layer CI gates; `main` fully green |
| 8 | Markdown docs not certified (no-read constraint during audit) | Open |

Note: the report's positive-evidence list predates two deliberate `main` policy
changes — Dependabot version updates and the Dependency Review job were removed
(dependency updates are manual per `docs/dependency-policy.md`).

## Remediation sequence

The report's minimum order for an unqualified release, mapped to finding IDs:

1. Consent authority, deployment isolation, and SDK UI injection — RA-01, RA-03,
   RA-18 (+ built-browser adversarial tests).
2. Authoritative migration maintenance protocol or online-safe migrations — RA-02.
3. Exact-engine selector function and JSON type guards with full selector-matrix
   coverage — RA-04, RA-05 (also closes release-eng gap 6).
4. Experiment integrity: immutable enrollment, preserved authority, exposure
   idempotency, unknown-variant finality — RA-06, RA-07, RA-08, RA-09.
5. Tenant-scoped executable Codegen capability and a project-level Agents
   execution lane — RA-10, RA-11.
6. Config outbox quarantine/lag health; ClickHouse-derived retention — RA-12, RA-14.
7. Pre-auth admission, raw-IP/cookieless privacy, Python exposure dedupe, writer
   replica/global-capacity gaps — RA-16, RA-13, RA-15, RA-17.
8. Admin capability/readiness/route/role alignment and schema readiness.
9. Privileged Codegen runtime audit; pin workflow actions; SBOM/scanning/signing;
   image-distribution decision — release-eng gaps 3, 4, 5.
10. ~~Reconcile the stacked branch with `main` and rerun every gate~~ — done
    2026-07-17 (merge train #98–#109).
11. Independent review of all Markdown release/support/security material.
12. Staging/disposable publish first; verify artifact identity and rerun
    recovery; then the final immutable tag.
