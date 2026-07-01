"""House rules handed to the editing agent as a read-only conventions file.

These are *standing* instructions — stable across every changeset — so they are
loaded via Aider's ``--read`` rather than appended to the per-task ``--message``.
``--read`` files join the cacheable static prefix (system prompt + repo map +
read-only files), so with ``--cache-prompts`` the rules are re-read at ~0.1x on
each ``--auto-test`` retry instead of full input price. Per-task specifics belong
in the spec/constraints; only repo-agnostic rules belong here.

The rules encode the failure modes observed in early codegen PRs: code that
*compiles but is unreachable* (component never mounted, event endpoint missing),
and code that *reinvents primitives the repo already provides* (hand-rolled
analytics instead of the installed SDK). The agent's bar is "wired in and
exercised", not "builds green".
"""

from __future__ import annotations

# Markdown so the agent reads it as structured guidance, not prose.
CONVENTIONS_MD = """\
# Codegen house rules (read-only — always apply)

You are implementing an approved feature in a customer's repository. A change
that compiles but does nothing is a FAILED change. Hold yourself to "reachable
and exercised", never merely "builds green".

## 1. Wire everything you add into a reachable path
- A new component/page/handler MUST be imported and rendered/mounted somewhere
  on a real route. Never leave a new export unreferenced — dead code that
  compiles is a defect, not a deliverable.
- Before finishing, trace each new symbol to an entry point (layout, page,
  route table, DI registration). If you cannot reach it, wire it or say why.

## 2. Every call must hit something that exists
- If new code calls an API route / endpoint / beacon URL, that target MUST
  exist. Create the missing route (or use an existing one) in the SAME change —
  do not emit to a URL that 404s.
- Verify request/response shapes against the actual handler, not an assumed one.

## 3. Reuse app code; call external systems through their own API
Two different rules — do not conflate them.

- INSIDE the app (UI, components, business logic, styling, local utils): reuse
  and match what already exists. Grep for the existing component/helper/pattern
  and use it; do not reinvent a primitive the app already provides.
- ACROSS a boundary (an installed SDK/package, another service, an external
  module): call that system through ITS OWN documented API — e.g. the analytics
  SDK's `track`, the module's client. Do NOT assume an app-local wrapper is the
  right path just because it exists.
  An app wrapper around a boundary is trustworthy ONLY if you verify it reaches
  the external system. Trace it end to end: does it terminate in the SDK/module's
  real entry point? If it instead pushes to a `window.*` global, a `dataLayer`,
  or a queue nothing flushes, it is BROKEN — bypass it, call the module directly,
  and note the broken wrapper. Reusing a boundary wrapper you did not verify is
  how instrumentation lands nowhere.
- NEVER import a package that is not already in the repo's manifest
  (package.json / pyproject / go.mod / Cargo.toml). An import of an
  uninstalled package is not a TODO — it breaks the build/type-check for the
  whole project (e.g. a Next.js build type-checks every file). If a new
  dependency is genuinely required, add it to the manifest AND the lockfile in
  THIS change and confirm the install succeeds; otherwise use what is present.

### Analytics / instrumentation (APDL SDK)
- If this repo depends on an APDL SDK, a read-only `APDL_SDK_*.md` reference with
  the exact call path is provided alongside these rules — follow it.
- Emit events through the SDK's own `track`/`capture` API so they carry the app's
  resolved identity and reach the backend. Never emit through a bespoke
  `fetch`/`sendBeacon` or an assumed `window.*` / `dataLayer` global — events that
  land on a different sink or identity are unjoinable and worthless.
- A test that asserts an event fires should spy on the SDK (its `track`, or the
  client its init returns), never on a `window` global.
- Do not fabricate data a real endpoint already serves (e.g. use the repo's FX
  / pricing / data route instead of hardcoding values) when demonstrating a
  capability.

## 4. Prove it works — tests when the repo can run them
- The per-task message states whether this repo has a test framework. Follow it.
- IF the repo has a test runner: add at least one test that exercises the NEW
  behavior (renders the component, drives the key interaction, asserts the
  event/side-effect fires), using the framework the repo ALREADY depends on —
  never a different one. "Existing tests pass" is not coverage of what you built.
  If the feature's point is to MEASURE something (an event), a test must assert
  that event is emitted to its real sink.
- IF the repo has NO test runner: do NOT add test files and do NOT import a test
  library — the dependency is absent, so the import breaks the build (see #3).
  Verify your change against the repo's build/type-check gate instead. Only add
  a runner if it is essential to the feature, and then wire it fully per #3.

## 5. Respect the proposal's stated gates and scope
- If the task description lists unmet dependencies or says research/decision must
  precede code ("before any code is written", "definition must be agreed
  first"), do NOT ship a full implementation that presumes the answer. Produce
  the unblocked slice (e.g. the API route + a stubbed/flagged surface) and leave
  clear TODOs for the gated parts, rather than hardcoding a guess.
- Keep the change minimal and coherent. Prefer the smallest reachable,
  tested vertical slice over a broad monolith.
"""
