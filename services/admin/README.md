# APDL Admin Console

Single-page admin console for the APDL platform. The browser talks only to the
same-origin [Admin API](../admin-api/README.md), which authenticates human
users, enforces project roles, and injects service credentials server-side.
Project API keys never enter frontend JavaScript.

Full specification: `local-files/docs/plans/admin-console-ui-implementation-plan.md`
(vault). This package implements **all plan phases (0–7)**:

- App shell: sidebar navigation, workspace switcher, SSE liveness indicator,
  dark mode.
- Login (`/login`): authenticates an administrator email/password through the
  Admin API. The opaque session is held in an `HttpOnly`, `SameSite=Strict`
  cookie; any later API 401 clears cached data and redirects back to login.
- Project access (`/settings/workspace`): lists only the projects and roles
  assigned to the authenticated user and switches the active project without
  storing credentials locally.
- Overview (`/`): service health strip (10s poll), flag state summary, realtime
  stream status.
- Feature flags, full lifecycle: list with search/state/archived filters
  (`/flags`), detail tabs Overview · Targeting · Guardrails · Audit · Tester
  (`/flags/:key`), stale-flag hygiene report (`/flags/hygiene`).
- Flag writes (`/flags/new`, `/flags/:key/edit`): react-hook-form + zod editor
  mirroring FlagCreate/FlagUpdate (changed-fields-only PUTs, `enabled` derived
  from state), pre-submit review sheet (payload + curl), 409 version-conflict
  rebase dialog, and lifecycle actions — activate / deactivate / disable (kill
  switch with reason + evidence) / archive (typed confirmation) / cleanup.
- Evaluation tester (Tester tab): local FNV-1a evaluator **parity-tested
  against `fixtures/gates/parity.json`** (the same golden values the SDKs and
  config service pin), rule-by-rule trace with bucket bars, optional
  server-side verification via `POST /v1/evaluate` (proxied server-side), 10k-user
  population simulator (also available pre-save in the editor), and a
  served-config panel showing the exact SSE payload SDKs receive.
- Analytics (`/analytics/*`): events explorer (counts / timeseries /
  breakdown), funnels with drop-off highlighting, retention heatmap, cohort
  comparison — with saved views (localStorage), CSV export, and raw-JSON
  drawers. The query-service filter vocabulary is a distinct type from flag
  rule conditions (AD-6).
- Experiments (`/experiments`): strict Config-owned experiment/backing-flag
  lifecycle with optimistic versions. The read-only Results tab resolves the
  flag, binary-conversion metric, variants, control, and time window from
  Config, then displays every Bonferroni-adjusted treatment comparison or a
  typed insufficient-data state. It does not recommend or trigger actions.
- Integration verification (`/settings/verify`): the five-step console-native
  `dev.sh smoke` — ingest → pipeline poll (re-send at attempt 5) → flag
  bootstrap with X-Cache observation → SSE freshness.
- Agents (`/agents`): trigger form with the gating matrix mirrored (and
  drift-tested) against `framework/gating.py`, server-side run history,
  run monitor with phase stepper, **rich approvals** showing the exact
  experiment designs / proposals being approved, per-run agent audit trail
  with safety-check verdicts, and persisted run outputs. (The backing
  endpoints — runs list, run results, run audit — were added to the agents
  service as plan gaps G1–G3.)
- Live updates: one credential-free, same-origin `EventSource` per project; the
  Admin API supplies the service key upstream. SSE events
  invalidate TanStack Query caches (admin views re-fetch rather than trusting
  the client payload). Toasts announce changes made outside this console.
- Every panel and write dialog reproduces its exact API call as **curl**.

Remaining backend-tracked work: G4 (event-name discovery for autocomplete),
G6–G8 (guardrail/pipeline observability), and G10 (pagination).

## Stack

Vite + React 18 + TypeScript (strict, `noUnusedLocals`/`noUnusedParameters`),
TanStack Query + Table, React Router, Tailwind CSS with shadcn/ui-style
primitives (copied in-repo under `src/components/ui/`), react-hook-form + zod,
native `EventSource`.

Per the repo's **Strict Schema Rule**, `src/api/schemas/` holds zod mirrors of
the config service's Pydantic models — exact canonical field names, `.strict()`
objects (mirroring `extra="forbid"`), and every API response is parsed against
them; drift fails loudly as a `schema_mismatch` error.

## Commands

```bash
npm install        # or: make deps (repo root)
npm run dev        # dev server on http://localhost:5173  (make run-admin)
npm test           # vitest                                (make test-admin)
npm run lint       # tsc --noEmit for src + tests          (make lint-admin)
npm run build      # typecheck + production bundle to dist/ (make build-admin)
```

For local development, migrate PostgreSQL, then run the backend and SPA:

```bash
make migrate-postgres
make run-admin-api   # :8085, Vite proxies /api here
make run-admin       # :5173
```

Open `/register` to create an email/password account. New accounts have zero
projects and zero roles; project access must be granted separately by an
operator or created from `/settings/workspace`. Creating a project associates
it with the current profile and grants core analytics roles plus
`agents:read`. The console preserves the returned role set: self-created
projects can inspect Agents history, while trigger, approval, and custom-agent
mutation controls remain unavailable.

`scripts/dev.sh up-core` runs the backend and SPA together in Docker. The Vite
bundle has no environment-specific service URLs; nginx proxies `/api` over the
private container network.

## Layout

```
src/
├── api/          # http wrapper, SSE lifecycle, zod schemas, typed clients
├── core/         # workspace context, query client + keys, theme, live (SSE→cache),
│                 # evaluator/ (FNV-1a port, parity-tested against repo fixtures)
├── components/
│   ├── ui/       # shadcn-style primitives (button, dialog, table, …)
│   ├── shared/   # reusable wrappers: DataTable, StatePill, JsonDiff, CurlButton, …
│   └── layout/   # AppShell (sidebar + topbar)
├── features/     # overview/ flags/ system/ settings/ — one folder per area
└── lib/          # formatting, curl builder, small hooks
__tests__/        # vitest + Testing Library + MSW (pattern mirrors sdk/javascript)
```

## Security posture

The browser stores only non-secret preferences and the active project ID.
Passwords are Argon2id hashes; opaque session and CSRF tokens are stored as
SHA-256 digests in PostgreSQL. Unsafe requests require both an allowed `Origin`
and a session-bound double-submit CSRF token. The Admin API selects the
project-scoped service key from server environment configuration, strips
caller-supplied credentials, checks the user's project role, and then proxies
the request. Production deployments must use HTTPS, secure cookies, and a
least-privilege `APDL_SERVICE_API_KEYS` map.
