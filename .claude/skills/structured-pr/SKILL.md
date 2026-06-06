---
name: structured-pr
description: Create a well-structured pull request for the APDL monorepo. Use when the user asks to "create a PR", "open a pull request", "raise a PR", or ship the current branch/changes. Handles branching off main, per-service lint, a conventional commit, and a templated PR body via the gh CLI.
---

# Structured PR

Turn the current working changes into a clean, reviewable pull request for the
APDL monorepo. Follow these phases in order. Confirm with the user before the
push/PR-create step if anything is ambiguous (which changes belong, target
branch, whether a binary/generated artifact should be excluded).

## Phase 0 — Scope the change

1. `git status --short` and `git branch --show-current` to see what's changed and
   where you are.
2. Determine the diff under review:
   - Committed work: `git diff main...HEAD --stat`
   - Uncommitted/untracked: `git status` — untracked files won't show in `git diff`.
3. Decide what belongs in the PR. **Exclude** binary/design artifacts that aren't
   source (e.g. `*.docx`, `*.xlsx`, exported diagrams, local scratch files) unless
   the user explicitly wants them. Stage files explicitly by path — never blanket
   `git add .` — so excluded artifacts stay untracked.
4. If the set of changes spans unrelated concerns, ask the user whether to split
   into multiple PRs rather than bundling.

## Phase 1 — Lint before committing

This repo's CI runs `ruff` on all four Python services and `tsc` on the SDK.
Run the relevant linter for each touched area so the PR is green on arrival:

| Touched path | Lint command |
|---|---|
| `services/ingestion/` | `cd services/ingestion && .venv/bin/ruff check app/` |
| `services/config/` | `cd services/config && .venv/bin/ruff check app/` |
| `services/query/` | `cd services/query && .venv/bin/ruff check app/` |
| `services/agents/` | `cd services/agents && .venv/bin/ruff check app/` |
| `sdk/javascript/` | `cd sdk/javascript && npx tsc --noEmit` |

Run the relevant tests too if the change is non-trivial (`make test-<service>` or
the single-test commands in CLAUDE.md). Fix lint/test failures before proceeding —
do not open a PR with a red diff.

## Phase 2 — Branch

If on `main`, create a descriptive feature branch before committing:

```bash
git checkout -b <kebab-case-topic>
```

Pick a branch name from the change's purpose (e.g. `canonical-envelope-schema`,
`fix-funnel-retention-window`). If already on a feature branch, stay on it.

## Phase 3 — Commit

Stage the chosen files explicitly, then commit with a conventional message:
a concise imperative subject line (≤72 chars), a blank line, and a body that
explains *what* and *why* as terse bullets. End every commit with the footer:

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

A pre-commit hook may re-run `ruff` — if it fails, fix and re-commit.

## Phase 4 — Push & open the PR

```bash
git push -u origin <branch>
gh pr create --base main --head <branch> --title "<subject>" --body "<body>"
```

Use this body template (drop sections that don't apply — never pad with filler):

```markdown
## What
One-paragraph summary of the change and the user-facing or system-level effect.

## Why
The motivation / problem this solves. Link issues with `#123` if any.

## Changes
- Grouped by area/service. Name the key files and what each does.
- Migrations, schema, or config changes called out explicitly.

## Quality pass
- Any cleanup applied (reuse, simplification, lint). Omit if none.

## Notes
- Anything reviewers must know: deferred work, deliberate trade-offs,
  things intentionally NOT done (e.g. "models not yet wired into handlers").

## Test plan
- [x] Lint clean for touched services
- [x] What you verified and how (round-trip, import, manual run)
- [ ] Follow-ups / things left for a later PR

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

## Phase 5 — Report

Return the PR URL `gh` prints. Summarize in one or two lines what was committed,
what was deliberately excluded (e.g. an untracked `.docx`), and any follow-ups
the test plan flagged.

## Principles

- **Faithful test plan.** Check boxes only for what you actually verified; leave
  unverified/follow-up items unchecked. If lint or tests failed and you couldn't
  fix them, say so in the PR rather than hiding it.
- **No blanket adds.** Stage by path so unrelated or binary files don't slip in.
- **Right-sized PRs.** Prefer one coherent concern per PR; offer to split when a
  changeset mixes unrelated work.
- **Don't push or open the PR** if the user only asked to prepare/commit — stop at
  the requested step.
