# APDL Admin Console

Single-page admin console for the APDL platform — a pure API client of the four
services (ingestion :8080, config :8081, query :8082, agents :8083). It adds no
backend of its own and persists nothing server-side; all configuration
(connection URLs, API key, actor) lives in browser `localStorage` as
"workspaces".

Full specification: `local-files/docs/plans/admin-console-ui-implementation-plan.md`
(vault). This package currently implements **Phases 0–3**:

- App shell: sidebar navigation, workspace switcher, SSE liveness indicator,
  dark mode.
- Workspace settings (`/settings/workspace`): connection profiles, live
  `project_id` derivation from the API key, per-service health test.
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
  server-side verification via `POST /v1/evaluate` (internal token), 10k-user
  population simulator (also available pre-save in the editor), and a
  served-config panel showing the exact SSE payload SDKs receive.
- Live updates: one `EventSource` on `GET /v1/stream` per workspace; SSE events
  invalidate TanStack Query caches (admin views re-fetch rather than trusting
  the client payload). Toasts announce changes made outside this console.
- Every panel and write dialog reproduces its exact API call as **curl**.

Analytics, experiments, and agent screens are later phases (plan §11).

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

Point a workspace at a running stack (`scripts/dev.sh up-full` or
`make dev` + individual services). Defaults assume localhost ports; build-time
`VITE_INGESTION_URL` / `VITE_CONFIG_URL` / `VITE_QUERY_URL` / `VITE_AGENTS_URL`
seed different defaults.

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

The backend currently format-validates API keys without verifying secrets, and
the query/agents services are unauthenticated — the console is a
**trusted-network/localhost tool** until plan gap G9 lands. Keys are stored in
`localStorage`, sent as the `X-API-Key` header everywhere except the
`EventSource` URL (browser limitation). `x-apdl-actor` is client-asserted
attribution, not authentication.
