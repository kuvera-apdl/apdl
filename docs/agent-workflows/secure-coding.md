# Secure coding & review — APDL

Canonical security conventions for the APDL monorepo. Follow this when **writing** or
**reviewing** any code in a service, SDK, or pipeline. It is grounded in the patterns
this codebase already gets right — the goal is to keep new code consistent with them,
and to catch the specific mistakes that would break them.

How to use it:

- **Writing code** — before you finish a change that touches any area below, walk its
  checklist and match the existing canonical pattern named there.
- **Reviewing code** — use the per-domain "red flags" as a grep/read checklist against
  the diff. A red flag is not automatically a bug, but it must be justified.
- **Scope** — apply the domains the diff actually touches. Don't invent findings; tie
  every concern to a concrete tainted-input → sink data flow.

Severity language matches the audit reports: Critical / High / Medium / Low / Info.

The register of currently-open findings lives in `docs/audits/` (currently
`docs/audits/reaudit-2026-07-16-findings-register.md`, tracking the full report at
`docs/oss-release-unqualified-reaudit-2026-07-16.md`). Before writing or reviewing code on
a surface it names, read its findings for that surface: do not re-introduce a pattern an
open finding condemns, and do not assume a green suite covers it — the open findings are
precisely the defects the green suites did not catch.

---

## 1. Authentication & API keys

The canonical authenticator is `services/config/app/auth.py` (mirrored in ingestion,
query, agents, codegen). New auth code must match its shape.

**Do:**
- Store only a **SHA-256 hash** of the key; never the raw key.
- Compare every secret with `secrets.compare_digest` (constant-time). This includes the
  key hash *and* the embedded `project_id` / `kind` / `prefix` fields.
- Verify the key's embedded `project_id`/`kind`/`prefix` against the stored row, so a key
  minted for project A cannot be replayed against project B.
- Run a **dummy-hash comparison on the not-found path** to equalize timing and defeat user/key
  enumeration.
- Enforce `active` and `expires_at` on every lookup.
- **Reject credentials passed in query params** (`?api_key=...`) — accept them only in the
  header. Query strings land in logs and history.
- Keep the **public/secret key split** intact: browser keys are `client_<project>_...` with
  the minimal role set (`{events:write, config:read}`); confidential keys are `proj_...`.
  A browser-facing endpoint must never require or accept a `proj_` key, and the JS SDK must
  reject one.

**Red flags:** `==` on a key/token/hash; hashing skipped; a not-found branch that returns
early without a dummy compare; `request.query_params.get("api_key")`; a browser role set
that includes write/admin scopes.

---

## 2. Authorization & multi-tenancy

Every request is scoped to exactly one `project_id`. Tenant isolation is enforced
server-side, never trusted from the client.

**Do:**
- Derive `project_id` from the **verified principal**, not from the request body/query.
  Where the body carries a `project_id`, only *equality-check* it against the principal
  (`require_project(...)`), then use the principal's value.
- Filter **every** DB/warehouse/cache query by `project_id`.
- Gate each endpoint with `require_project` + `require_role`; deny self-registered projects
  the privileged roles (see agents `agents:run/manage/approve`).
- Build Redis/stream keys only from principal-derived, charset-constrained values
  (`events:raw:{project_id}`, `project_id` matches `^[A-Za-z0-9]{1,64}$`).
- When a resource is looked up by id, re-check `row["project_id"] == principal.project_id`
  before returning or mutating it (prevents IDOR).

**Do not** treat "who inserted the row first" as authoritative tenant ownership. Console-side
project creation must not be able to claim/squat a `project_id` that maps to a real tenant's
data (see admin `create_project` — namespace or gate claiming).

**Red flags:** a query without a `project_id` filter; using `body.project_id` directly in a
query; a path/query id fetched without an ownership re-check; a stream/cache key built from
unvalidated input.

---

## 3. SQL / warehouse query injection

**Do:**
- Parameterize **always** — asyncpg `$1/$2`, ClickHouse `%(name)s`. User data goes in the
  params dict, never into the query string.
- Never `f-string`/`.format()`/`%`/concatenate user input into SQL. The *only* acceptable
  interpolation is a **static module constant** (a fixed column list) or an **enum-constrained
  literal** (e.g. `INTERVAL {interval}` where `interval` can only be `1 HOUR|DAY|WEEK|MONTH`).
- Allowlist identifiers you cannot parameterize (table/column/group-by names) against a fixed
  set or a strict regex (`_PROPERTY_NAME_RE`).
- Add an **input allowlist even to free-text value fields** (`event_name`, `metric_event`) so
  injection safety doesn't rest solely on the driver's client-side `str.format`/`escape_param`.
- Constrain param **types** to `{str, int, float, date}`; an unrecognized type can be
  interpolated unquoted by the driver's fallback branch.
- Never build ClickHouse table functions (`url`/`file`/`remote`/`s3`) from user input — that is
  SSRF / local-file read.

**Red flags:** any `f"... {user_value} ..."` / `.format(` / `%` / `+` inside a SQL string;
a dynamic column/table name; a value field with no regex; a param whose type isn't one of the
four above.

---

## 4. Input validation & request models

**Do:**
- Every request body is a strict Pydantic model with `extra="forbid"` (`StrictModel`). This
  blocks mass-assignment (an injected `project_id`, `role`, etc. is rejected, not silently
  bound).
- Enforce explicit bounds: batch size, string length, JSON depth/node/container counts,
  date-range width. Reject **before** doing expensive work.
- Validate every externally-derived identifier that reaches a sensitive sink with a tight
  regex: git SHAs (`^[0-9a-fA-F]{7,64}$`), branch/ref names, head refs
  (`^[A-Za-z0-9._-]{1,128}$`), config ids, file paths.
- Set roles/salts/ids **server-side** (`secrets.token_urlsafe`), never from the request.

**Red flags:** a model without `extra="forbid"`; a `dict[str, Any]` context that flows into a
sink unvalidated; a raw `str` id/ref/path reaching git, a filesystem, or an HTTP path; a size
check that runs after the payload is already fully buffered/parsed.

---

## 5. Subprocess, git & shell

**Do:**
- Use **argv arrays** via `create_subprocess_exec` / `Popen(list(argv))`. Never `shell=True`,
  `os.system`, `eval`, `exec`, backticks, or string-built commands.
- For git (and any tool that parses `-`-prefixed options), place a `--` end-of-options
  separator before untrusted positionals: `["revert", "--no-edit", "--", sha]`. Combine with
  charset validation of the value (Section 4) — belt and suspenders against argument injection.
- Pin the subprocess environment: `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_TERMINAL_PROMPT=0`, and a clean `HOME`, so no host `~/.gitconfig`
  `credential.helper` / `url.insteadOf` is picked up.
- Keep secrets **out of** the subprocess/sandbox environment (App private key, Postgres DSN,
  internal token must not be reachable from the editor/agent container).

**Red flags:** `shell=True`; any f-string command; a tenant-controlled value used as a git
positional without `--` and without charset validation; a subprocess env that inherits the
service's full environment.

---

## 6. Frontend / UI XSS (SDK + admin console)

The JS SDK renders **server/agent-controlled** UI configs, and those configs are attacker-
influenceable (multi-tenant config service; LLM-generated personalization). Treat every UI
config field as untrusted.

**Do:**
- Never assign server-controlled strings to `innerHTML` / `dangerouslySetInnerHTML`. Use
  `textContent`, or sanitize with a strict allowlist before insertion. Remove any
  "HTML allowed" affordance.
- If a rich-content affordance must exist, sanitize with a strict **element and
  attribute** allowlist: strip every inline event-handler attribute (`on*`), `style`,
  and `srcdoc`, and apply the URL scheme allowlist to every URL-bearing attribute.
  Filtering tag names alone is not sanitization — event handlers ride in on allowed tags.
- Validate every URL prop (`href`, `src`, `imageUrl`, redirect targets) against a scheme
  allowlist (`https:`, `http:`, `mailto:`, relative). **Reject `javascript:`, `data:`,
  `vbscript:`.** Do this even when a CSP is present — defense in depth.
- Add `rel="noopener noreferrer"` to every generated anchor; allowlist `target`.
- Keep the strong CSP (`script-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'`,
  `base-uri 'self'`, no `'unsafe-inline'`); rely on React auto-escaping and never bypass it.
- Login/return redirects: accept only paths starting with a single `/` (reject `//` and
  absolute URLs) — open-redirect guard.

**Red flags:** `innerHTML`/`dangerouslySetInnerHTML`/`el.href = props.x` without a scheme
check; a sanitizer that allowlists element names but not attributes; a URL schema typed as
bare `z.string()` instead of `.url().refine(startsWith('https://'))`;
`target="_blank"` without `rel`.

---

## 7. SSRF & outbound HTTP

**Do:**
- Enforce `https` on every hop, including redirects. Cap redirect count.
- On each redirect hop, **resolve and reject private/link-local targets**: RFC1918, loopback,
  `169.254.0.0/16` (cloud metadata), ULA.
- Strip `Authorization`/token headers when a redirect leaves the API origin
  (`is_api_origin` check) so credentials don't leak to a third-party host.
- Never fetch a URL derived from tenant input. Upstream service base URLs are
  operator-configured env vars, not request-controlled.
- Bound response size when downloading (logs, artifacts) and inspect archives for path
  traversal / symlinks / zip-bombs before extracting.

**Red flags:** an HTTP client that follows redirects with only a scheme check; a fetch target
built from repo/webhook/user data; an outbound call that keeps the auth header across origins.

---

## 8. Secrets management

**Do:**
- **Never commit real secrets.** `.env` must be git-ignored; only `.env.example` (with
  placeholders) is tracked. Confirm with `git ls-files | grep -i env` and check git history,
  not just the working tree.
- **Fail closed** on missing production config. Don't fall back to a hardcoded default DSN /
  credential (`postgresql://apdl:apdl_dev@localhost...` is a *dev-only* default) — require the
  env var and fail startup if absent in prod.
- Never log secrets, keys, tokens, or DSN-bearing exceptions. Readiness/health handlers must
  keep connection strings out of their responses.
- Keep provider/App keys out of prompts, agent memory, audit logs, and sandbox/subprocess
  environments.
- GitHub App auth: sign JWTs `RS256` with a short bounded `exp`; re-validate installation
  tokens for exact repo id + exact permission set before use; revoke on lease exit.

**Red flags:** a tracked `.env`; a literal password/token/`BEGIN PRIVATE KEY`/`ghp_`/
`github_pat_` in source; `logger.info(f"...{api_key}...")`; a health endpoint echoing DB error
text; a prod code path with an embedded credential default.

---

## 9. Deserialization

**Do:** deserialize untrusted data only as JSON, into plain dicts/lists, then shape-enforce
with Pydantic.

**Do not:** use `pickle`, `marshal`, `yaml.load` (use `yaml.safe_load`), `eval`, `exec`, or
`__import__`/`compile` on any value that originates outside the process — DB JSONB, LLM output,
request bodies, and webhook payloads all count as untrusted.

**Red flags:** `pickle.loads`, `yaml.load(` without `SafeLoader`, `eval(`/`exec(` on parsed data.

---

## 10. Denial of service & resource limits

**Do:**
- Enforce the request-body byte cap **while streaming**, before buffering the whole body.
  A `Content-Length` pre-check is bypassable with `Transfer-Encoding: chunked` — also read via
  `request.stream()` with a running byte count, or set an ASGI/uvicorn/proxy body limit.
- Rate-limit **before** expensive parse/validation. Charge a cheap first-pass cost (by byte
  size / batch length) right after auth, then reconcile the exact cost — don't run full Pydantic
  validation on traffic you're about to `429`.
- Cap long-lived connections (SSE) **per project / per credential / per IP**, plus a global
  ceiling; return `429` past the cap. Add nginx `limit_conn`/`limit_req` on `/v1/stream` and
  `/v1/flags`. Remember public browser keys make these endpoints effectively unauthenticated
  for DoS purposes, and single-replica services take down *all* tenants when exhausted.
- Apply warehouse query budgets (`max_execution_time`, `max_bytes_to_read`, `max_rows_to_read`,
  `max_memory_usage`) and per-project concurrency; prefer them over unbounded scans.
- Bound Redis stream / DLQ growth with `maxlen`.
- Bound the cost of **failed** authentication too: cap per-source rate/concurrency on
  credential checks so valid-format invalid keys can't monopolize DB pool checkouts or
  hashing CPU before quotas apply (pre-auth exhaustion).
- Catch `RecursionError`/`ValueError` in JSON parsing and return a clean 4xx, not a 500.

**Red flags:** `await request.body()` before any size check; a rate-limit call at the end of the
handler; `add_connection` with no cap; a query builder with no `LIMIT`/budget; an unbounded
stream.

---

## 11. CORS, transport & proxy headers

**Do:**
- Scope `allow_origins` to known first-party origins on privileged/state-changing endpoints;
  reserve wildcard for the genuinely public SDK read paths (`/v1/flags`, `/v1/stream`,
  `/v1/events`), and even there keep `allow_credentials=False`. Replace `allow_methods=["*"]` /
  `allow_headers=["*"]` with explicit lists.
- Enforce/default to `https` in SDK endpoint resolution; allow `http` only for an explicit
  `localhost` dev opt-in. Validate the endpoint scheme in the JS SDK, mirroring the Python SDK.
- Do **not** trust `X-Forwarded-For` / `X-Real-IP` for stored IPs or rate-limit keys unless it
  comes through a configured trusted-proxy hop count; validate it parses as a real IP.

**Red flags:** `allow_origins=["*"]` on an admin/write/evaluate route; string-concatenating an
unvalidated `endpoint` into request URLs; raw `X-Forwarded-For` written into data or used as a
limiter key.

---

## 12. LLM & autonomous-agent safety

Untrusted end-user event data (event names, property keys/values) flows into agent prompts and
can steer autonomous actions (prompt injection). Design so injection can't escape the
deterministic guardrails.

**Do:**
- Treat all tool-result / warehouse-derived content re-entering a prompt as **data, never
  instructions**. Wrap it in explicit untrusted-data delimiters with a system-prompt contract;
  strip control phrases.
- Keep the **deterministic `SafetyValidator` as the real gate** (exposure bounds, guardrails,
  autonomy-level gating). An LLM "safety review" that fails open is defense-in-depth only —
  never the security boundary.
- Dispatch tools by **allow-listed name lookup** with per-call param validation and
  context-injected `project_id`/scope; the model must not be able to name an arbitrary callable
  or widen scope.
- Make safety state **durable and shared** — rate-limit / conflict-detection state in
  Postgres/Redis keyed by `(project_id, action_type)`, not a per-process in-memory dict (it
  multiplies by replica count and resets on restart).
- `quote(value, safe="")` any LLM-authored id placed in a request path (consistency with the
  other tools).
- Consider requiring approval (not auto-deploy) for any action whose lineage includes free-text
  warehouse data, even at the highest autonomy level; tag agent memory by provenance and exclude
  untrusted-derived memory from safety-relevant prompts.
- Redact event property values (potential PII) before they enter third-party LLM prompts.

**Red flags:** tool output concatenated straight into the prompt; a fail-open LLM check treated
as a gate; tool dispatch by arbitrary model-supplied name; in-process rate-limit state in a
multi-replica service; an LLM-authored id interpolated into a path without `quote`.

---

## 13. Webhooks

**Do:**
- Verify the HMAC signature with `hmac.compare_digest` (constant-time) before doing any work.
- Add **replay/idempotency protection**: persist processed `X-GitHub-Delivery` (or equivalent)
  IDs with a short TTL and reject repeats before scheduling background work.

**Red flags:** signature compared with `==`; a delivery id read but never checked for reuse;
work scheduled before signature verification.

---

## 14. Logging

**Do:**
- Sanitize/`repr()` untrusted substrings before logging (JSON keys, event/field names can carry
  newlines → log forging). Prefer fixed reason codes plus a bounded, escaped sample.
- Return **generic** error messages to callers; don't reflect raw DB/upstream error text (map to
  fixed messages). Never log secrets or keys.

**Red flags:** `logger.warning(f"...{untrusted}...")` without escaping; an exception's raw
message returned in an HTTP `detail`; a key/token in a log line.

---

## 15. CI/CD & supply chain

**Do:**
- Pin third-party GitHub Actions to a **commit SHA**, not `@main`/a moving tag.
- Never interpolate `${{ github.event.* }}` into a `run:` step (script injection); pass via
  `env:` and quote. Avoid `pull_request_target` with untrusted checkouts; don't expose secrets
  to fork PRs; keep `GITHUB_TOKEN` permissions least-privilege.
- Install dependencies with lifecycle scripts disabled (`--ignore-scripts`) and hashes/frozen
  locks (`--require-hashes --only-binary`); refuse to run installs outside the sandbox.
- Pin/bound dependency ranges (avoid unbounded `>=`), keep lockfiles authoritative in CI, and
  ship a lockfile with published packages.

**Red flags:** an action referenced by tag/branch; `${{ github.event.pull_request.title }}` in a
`run:` line; `pull_request_target` + `actions/checkout` of the PR head; floating `>=` deps with
no upper bound; a publish workflow that leaks the npm/PyPI token.

---

## 16. Containers & local infra

Docker publishes ports through its own iptables rules that **bypass host ufw/firewalld** on
Linux, so a published port with no host-IP bind is reachable from the whole LAN/VM.

**Do:**
- Bind every published port to the loopback interface by default:
  `"${APDL_BIND_ADDRESS:-127.0.0.1}:6379:6379"`. The main `docker-compose.yml` already does this
  — keep the `deps`/dev compose files consistent with it. Never leave a `"6379:6379"` bare bind
  on a service that has no auth (Redis) or default credentials (Postgres/ClickHouse).
- Give datastores real credentials/`--requirepass` even in dev compose, or keep them
  loopback-only.
- Run app containers as a **non-root `USER`** (follow the `appuser`/`agent` pattern in the
  codegen Dockerfiles), so an app-level RCE isn't root-in-container.
- **Pin base images by digest** (`FROM python:3.12-slim@sha256:...`) and avoid `:latest`.
  Before moving a pinned runtime version, check dependency ceilings — e.g. `aider-chat`
  requires Python `<3.13`, so a bump to `python:3.14-slim` breaks the codegen worker (see
  `ci-cd-safety.md` on semantic merge conflicts).
- Don't `curl … | bash` a remote install script at build time (e.g. NodeSource). Install from the
  distro package, or a checksum-verified tarball — especially in images that hold credentials
  (codegen API image carries the GitHub App key).
- Generate a **random per-install** bootstrap key in `make setup`/`dev.sh`
  (`proj_demo_$(openssl rand -hex 16)`) rather than shipping one fixed value everyone shares.
- Set uvicorn `--forwarded-allow-ips` to the actual proxy address/subnet, not `*`.
- Never fall back to group `0` for the Docker-socket GID; fail instead. The Docker socket is
  host-root-equivalent — mount it only in explicitly-gated opt-in overlays with loud warnings.

**Red flags:** a `"port:port"` bind with no host IP on an unauthenticated/default-cred service; a
Dockerfile with no `USER`; a `FROM` without `@sha256:`; `curl ... | bash`/`| sh` in a build; a
fixed demo credential copied verbatim into every install; `--forwarded-allow-ips=*` on an
internet-reachable service; a `|| echo 0` GID fallback.

---

## 17. Consent, privacy & data retention

Browser collection is consent-gated and personal data is retention-bounded. The privacy
state machine is part of the security surface, not product polish.

**Do:**
- Treat an **explicit current consent denial as authoritative**: it must override any
  older persisted grant, immediately and before any collection starts. Persisted consent
  is a cache of the last decision, never an escalation over the present one.
- Scope persisted consent/identity state to the **deployment** (project + endpoint), so
  state written by one deployment cannot authorize collection in another.
- Default-deny: a fresh install collects nothing until the host opts in, and imports are
  side-effect-free.
- Store personal data (raw client IP, stable identifiers) only with a named runtime
  consumer and an enforced retention boundary. Derived tables (profiles, aliases,
  materialized views) inherit the **source table's** retention contract — never an
  implicit "forever".
- No deterministic device fingerprinting as an identity fallback; `cookieless` modes must
  not derive stable IDs from device characteristics or silently split identity.
- Redact event property values (potential PII) before they leave the deployment (LLM
  prompts, third-party sinks).

**Red flags:** a consent check that reads persisted storage ahead of the current explicit
setting; collection that starts before the consent gate resolves; a personal-data column
with no TTL/retention migration; a derived table whose retention outlives its source; an
identity path hashing device characteristics.

---

## 18. Capability truthfulness & lifecycle integrity

Readiness, capability, and lifecycle state drive autonomous actions. A false "ready" or a
mutable "launched" is a safety failure, not a cosmetic bug.

**Do:**
- Capability/readiness endpoints report what is **executable for this tenant right now**
  (credentials present, kill switches honored, runtime prerequisites met) — never a
  global optimistic flag. Consumers fail closed (e.g. `424`) when a required capability
  is disabled or unreachable.
- Freeze enrollment authority at launch: `traffic_percentage`, targeting rules, and
  variant sets are immutable while an experiment runs. Launched experiments are stopped
  or archived — never hard-deleted while they anchor recorded analysis.
- Make durable effects **idempotent**: exposure/event writes deduplicate server-side
  across retries (bounded, shared state — not per-process memory); registry publication
  is check-before-publish / verify-after, so identical reruns skip and mismatches fail.
- Reject finality when data disagrees with authority: unknown variants in a final
  analysis are an error, not a silent exclusion.
- Give every queue/outbox lane **poison-row quarantine plus lag/health visibility**, so
  one bad row cannot block a tenant forever and the blockage is observable.
- Serialize autonomous execution **per project across all entry paths** — a workflow
  resuming after approval shares the same execution lane as newly triggered ones.

**Red flags:** a readiness payload that ignores per-tenant prerequisites; an
UPDATE/DELETE path on launched-experiment fields without a state guard; an at-least-once
effect with no dedupe key; an outbox consumer that retries the head row forever; two
runners for the same project reachable via trigger + resume.

---

## Quick pre-merge checklist

- [ ] Every new query is parameterized and `project_id`-scoped.
- [ ] Request models are `extra="forbid"`; externally-derived ids/refs/paths are regex-validated.
- [ ] No `shell=True` / `eval` / `pickle` / `yaml.load`; git positionals use `--`.
- [ ] Secrets: constant-time compares, no secret in logs, `.env` not tracked, prod fails closed.
- [ ] UI/URLs: no `innerHTML` on server data, scheme allowlist on hrefs, `rel="noopener"`.
- [ ] Outbound HTTP blocks private-range redirects and strips cross-origin auth headers.
- [ ] Size caps enforced while streaming; rate-limit before expensive work; connections capped.
- [ ] Webhook signatures constant-time + replay-protected.
- [ ] CORS scoped on privileged endpoints; `https` enforced.
- [ ] Agent tool output treated as untrusted; deterministic validator is the gate.
- [ ] Containers: loopback-bound ports, non-root `USER`, digest-pinned base, no `curl|bash`.
- [ ] Consent: an explicit current denial wins over persisted state; personal data has a
      named consumer and a retention boundary.
- [ ] Capabilities report tenant-executable truth; launched lifecycle state is immutable;
      durable effects are idempotent.
