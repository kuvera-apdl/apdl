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

## 3. Reuse the repo's primitives — do not reinvent
- Before adding cross-cutting behavior (analytics, identity, auth, data
  fetching, styling), FIND how the repo already does it and use that path.
  Grep for an existing SDK/util/provider first; match it.
- Do not hand-roll what an installed dependency already provides.

### Analytics / instrumentation (APDL SDK)
- If the repo depends on `@apdl-oss/sdk` (or any analytics SDK), emit events
  through THAT SDK, not a bespoke `fetch`/`sendBeacon` emitter and not an
  assumed `window.*` global.
- Locate how the SDK is initialized (commonly an init component such as
  `APDLInit`) and reuse its instance/identity. Events must carry the same
  resolved identity / distinctId as the rest of the app so they join the
  identity graph — instrumentation that lands on a different id is unjoinable
  and worthless.
- If the SDK instance is not reachable from your call site, expose it through
  the existing init path (e.g. a shared module/provider) rather than minting a
  parallel emitter. Use the SDK's own `track`/`capture` API; don't guess names.
- Do not fabricate data a real endpoint already serves (e.g. use the repo's FX
  / pricing / data route instead of hardcoding values) when demonstrating a
  capability.

## 4. Prove it works — tests, not just a green build
- Add at least one test that exercises the NEW behavior (renders the component,
  drives the key interaction, asserts the event/side-effect fires). "Existing
  tests pass" is not coverage of what you built.
- If the feature's point is to MEASURE something (an event), a test must assert
  that event is emitted to its real sink.

## 5. Respect the proposal's stated gates and scope
- If the task description lists unmet dependencies or says research/decision must
  precede code ("before any code is written", "definition must be agreed
  first"), do NOT ship a full implementation that presumes the answer. Produce
  the unblocked slice (e.g. the API route + a stubbed/flagged surface) and leave
  clear TODOs for the gated parts, rather than hardcoding a guess.
- Keep the change minimal and coherent. Prefer the smallest reachable,
  tested vertical slice over a broad monolith.
"""
