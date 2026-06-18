# Structured PR Workflow

Use this workflow when the user asks to create a PR, open a pull request, raise
a PR, make commits for a PR branch, or ship the current branch or changes. It is
the canonical APDL PR workflow for all coding agents.

Turn the current working changes into a clean, reviewable pull request for the
APDL monorepo. Follow these phases in order. Confirm with the user before the
push/PR-create step if anything is ambiguous, including which changes belong,
the target branch, or whether a binary/generated artifact should be included.

## Commit-only and multi-commit mode

If the user asks to make one or more commits before opening the PR, repeat
Phases 0 through 3 for each logical commit. Do not run Phase 4 or Phase 5 until
the user asks to roll out, push, or open the PR.

Use this command shape for each commit:

```bash
# Phase 0 - scope this commit
git status --short
git branch --show-current
git diff -- <path> [<path> ...]

# Phase 1 - run lint/tests for the touched area before committing
cd services/<service> && .venv/bin/ruff check app/
# or
cd sdk/javascript && npx tsc --noEmit

# Phase 2 - create a branch first if still on main
git checkout -b <kebab-case-topic>

# Phase 3 - stage only the files for this commit and review the staged diff
git add <path> [<path> ...]
git diff --cached --stat
git diff --cached
git commit -m "<type>(<scope>): <imperative subject>" \
  -m "- What changed in this commit." \
  -m "- Why this commit belongs on the branch."
```

For a multi-commit branch, each commit should be independently reviewable and
cohesive. After the final commit, check the whole branch before opening the PR:

```bash
git status --short
git log --oneline main..HEAD
git diff main...HEAD --stat
```

## Phase 0 - Scope the change

1. Run `git status --short` and `git branch --show-current` to see what changed
   and where you are.
2. Determine the diff under review:
   - Committed work: `git diff main...HEAD --stat`
   - Uncommitted/untracked work: `git status --short`
3. Decide what belongs in the PR. Exclude binary/design artifacts that are not
   source, such as `*.docx`, `*.xlsx`, exported diagrams, and local scratch
   files, unless the user explicitly wants them.
4. Stage files explicitly by path. Do not use blanket staging commands such as
   `git add .`.
5. If the changes span unrelated concerns, ask the user whether to split them
   into multiple PRs.

## Phase 1 - Lint and test before committing

CI runs `ruff` on the four Python services and the Python SDK, `pytest` on the
Python SDK, and `tsc` on the JS SDK. Run the relevant linter for each touched
area:

| Touched path | Lint command |
|---|---|
| `services/ingestion/` | `cd services/ingestion && .venv/bin/ruff check app/` |
| `services/config/` | `cd services/config && .venv/bin/ruff check app/` |
| `services/query/` | `cd services/query && .venv/bin/ruff check app/` |
| `services/agents/` | `cd services/agents && .venv/bin/ruff check app/` |
| `services/codegen/` | `cd services/codegen && .venv/bin/ruff check app/` |
| `sdk/javascript/` | `cd sdk/javascript && npx tsc --noEmit` |
| `sdk/python/` | `cd sdk/python && .venv/bin/ruff check apdl/ tests/` |

Run the relevant tests too if the change is non-trivial, using
`make test-<service>` or the single-test commands in the repo agent guidance.
Fix lint/test failures before proceeding. Do not open a PR with a red diff unless
the user explicitly instructs you to and the failure is documented in the PR.

## Phase 2 - Branch

If on `main`, create a descriptive feature branch before committing:

```bash
git checkout -b <kebab-case-topic>
```

Pick a branch name from the change's purpose, such as
`canonical-envelope-schema` or `fix-funnel-retention-window`. If already on a
feature branch, stay on it unless the user asks otherwise.

## Phase 3 - Commit

Stage the chosen files explicitly, then commit with a conventional message:

- Use a concise imperative subject line of 72 characters or fewer.
- Add a blank line after the subject.
- Use a terse body that explains what changed and why.
- Add agent/tool attribution only when the current environment or user requires
  it. Do not hard-code an attribution footer for a different agent.

A pre-commit hook may re-run `ruff`. If it fails, fix the issue and commit again.

## Phase 4 - Push and open the PR

Do not push or open the PR if the user only asked to prepare, stage, or commit.
Stop at the requested step.

When the user has asked to open the PR, push the branch and create the PR:

```bash
git push -u origin <branch>
gh pr create --base main --head <branch> --title "<subject>" --body-file <body-file>
```

Use this PR body template. Keep the `Notes` section only when it adds useful
review context.

```markdown
## Summary

- 1-3 bullets describing the change and the motivation.

## Test plan

- [x] Lint clean for touched services
- [x] What you verified and how
- [ ] Follow-ups or checks intentionally left for later

## Notes

- Migrations, rollback steps, screenshots, trade-offs, or reviewer context.
```

## Phase 5 - Report

Return the PR URL printed by `gh`. Summarize what was committed, what was
deliberately excluded, and any follow-ups the test plan flagged.

## Principles

- Keep the test plan faithful. Check boxes only for what you actually verified.
- Stage explicit paths so unrelated or binary files do not slip in.
- Prefer one coherent concern per PR.
- Ask before bundling unrelated work.
- Do not hide failing lint or tests. Fix them or disclose them.
