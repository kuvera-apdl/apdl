# CI/CD safety — APDL

Canonical practices for operating this repository's CI/CD safely: merging stacked
PRs, retargeting bases, resolving conflicts against `main`, reading GitHub Actions
results, and gating merges and releases. It is grounded in failure modes actually
observed while running merge trains on this repo — the goal is that a green merge
train stays green, and that "green" always means what it claims to mean.

How to use it:

- **Running a merge train** — follow sections 1–7 in order for every layer.
- **Reviewing CI/CD changes** — use the per-section "red flags" as a checklist
  against workflow, Dockerfile, and dependency diffs.
- **Reading CI results** — sections 3 and 7 define what counts as evidence.

---

## 1. Stacked PRs merge bottom-up, one verified layer at a time

A stack of PRs (each based on the previous branch) must be merged from the bottom.
GitHub auto-retargets a child PR to the parent's base when the parent's branch is
deleted on merge.

**Do:**
- Merge the lowest PR first; wait for its base retarget and its CI before touching
  the next layer.
- Treat each layer as its own gate: retarget → refresh → CI green → merge. Never
  batch-merge the remainder because "the tip was green" — the tip was green on the
  stack's own base, not necessarily on the `main` that exists after earlier merges
  landed.
- After merging a layer, confirm the next PR's base actually flipped to `main`
  before proceeding (`gh pr view <n> --json baseRefName`).

**Red flags:** merging a PR whose base is still another PR branch; merging two
layers between CI runs; scripted loops that merge every open PR in one pass.

---

## 2. Make CI actually run — the triggers will silently skip you

This repo's CI runs on `pull_request: branches: [main]`, which filters on the
**base** branch. Three GitHub behaviors combine to leave stacked PRs with no CI at
all, silently:

1. A PR whose base is another feature branch never matches the trigger, so it has
   **zero** runs while stacked.
2. When the base auto-retargets to `main`, GitHub fires only the `edited` activity
   type — which is **not** in the default `opened/synchronize/reopened` set — so
   retargeting alone still starts nothing.
3. A PR in a conflicted (`CONFLICTING`/`DIRTY`) state gets **no workflow runs at
   all**, because GitHub cannot build the test-merge ref.

**Do:**
- After each retarget, force a `synchronize` event: `gh pr update-branch <n>`
  (preferred — it also merges current `main` into the head, testing the true
  post-merge state), or close/reopen the PR.
- Expect check-registration lag: the first `gh pr checks <n> --watch` within
  ~15–30s of pushing often reports "no checks reported". Retry before concluding
  anything.
- If a PR shows no runs, first check its mergeable state; resolve conflicts before
  expecting CI. GitHub's mergeability cache also goes stale — pushing to the head
  branch forces a recompute.

**Red flags:** a stacked PR with a green-looking "no checks" state; assuming a base
retarget re-ran CI; polling checks once immediately after a push and acting on the
empty result.

---

## 3. Absence of checks is never a pass

The merge gate for every layer is **positive green evidence on the current base**,
not the absence of red.

**Do:**
- Require a completed, successful check run for the exact head SHA being merged,
  produced after the PR's base became `main`.
- When a required job fails "expectedly" (a known pre-existing red), verify the
  **failure signature** — open the failing job's log and confirm it matches the
  known issue (same job, same error, same root cause) before treating it as
  expected. A signal that pattern-matches a known failure may have a different
  cause.
- Track the known-red's fix through the stack and confirm the run where it turns
  green (here: an experiment-smoke red introduced mid-stack stayed red on every
  layer until the tip's fix landed, then went green — each occurrence was
  signature-checked, and the final green confirmed the diagnosis).

**Red flags:** "no checks reported" treated as success; skipping the log read
because a failure "is the known one"; merging on checks that ran against a stale
base.

---

## 4. Sync with fresh refs and verify resolutions before pushing

**Do:**
- `git fetch origin main` immediately before any conflict resolution or merge of
  `main` into a branch. A stale `origin/main` produces a resolution that looks
  complete locally but leaves the PR conflicted on GitHub. (Observed cause: the
  fetch lived inside an earlier compound command that never actually ran — verify
  the fetch happened, don't assume it did.)
- Before pushing a resolution, prove it merges cleanly against the current remote:
  `git merge-tree --write-tree origin/main <branch>` must produce a tree without
  conflict markers.
- Do resolution work in a scratch worktree (`git worktree add`), never in the
  user's checkout; remove it when done.

**Red flags:** resolving conflicts against an `origin/main` fetched before the
last merge landed; pushing a merge commit without a `merge-tree` check; conflict
resolution mixed into the primary working tree.

---

## 5. Semantic conflicts: automated bumps vs. pinned, verified state

Automated dependency/image bumps on `main` (grouped Dependabot-style commits) can
land after a stack is cut. Merging `main` into stack branches then produces both
textual conflicts **and silent auto-merges that break builds** — git reports no
conflict, but the result is wrong.

Observed instance: `main` bumped base images to `python:3.14-slim` while the stack
carried digest-pinned `python:3.12-slim`; one Dockerfile auto-merged to 3.14 and
broke because `aider-chat` requires Python `<3.13`.

**Do:**
- After every merge of `main` into a stack branch — clean or not — diff the
  merged result against the stack tip for Dockerfile `FROM` lines, action
  versions, and dependency pins, and restore the tip's verified values.
- Keep digest-pinned images and pinned action versions; a bot bump does not
  outrank a pin that the stack's tests validated.
- Know the tooling constraints that make a bump breaking (runtime version ceilings
  of pinned dependencies) rather than trusting "newer is compatible".

**Red flags:** a clean merge accepted without inspecting base-image/pin lines; an
automated bump overriding a digest pin mid-train; upgrading a language runtime
without checking dependency version ceilings.

---

## 6. Conflict-resolution policy: tip-verified wins, except deliberate main policy

When stack branches conflict with `main`, there are exactly two legitimate sides:

- **Default: the stack tip's verified state wins** — image pins, action versions,
  dependency pins, workflow job definitions that the tip's CI validated.
- **Exception: deliberate post-cut policy decisions on `main` win** — e.g. this
  repo's removal of `.github/dependabot.yml` and the Dependency Review job
  (dependency updates are manual per `docs/dependency-policy.md`; the dependency
  graph is disabled). Re-adding them via a stack merge would silently reverse a
  policy decision.

**Do:**
- Classify each conflicted hunk as "verified state" vs. "policy decision" before
  picking a side; when in doubt, check `main`'s commit history for intent.
- Watch for vestigial jobs: a branch cut before a workflow change can still carry
  deleted jobs (a Dependency Review job failing in seconds on every stacked run).
  `gh pr update-branch` picks up `main`'s workflow file and removes them.

**Red flags:** blanket `--ours`/`--theirs` resolution across a workflow file;
resurrecting a file `main` deliberately deleted; a job failing instantly on every
PR that nobody investigates.

---

## 7. Observe `main` after every merge — the train isn't done at merge time

A PR's checks run on a test-merge ref; the push run on `main` after merging is the
first run of the **actual** merged tree.

**Do:**
- Watch `main`'s push run for every merge in the train
  (`gh run watch <id> --exit-status`), not just the last one.
- On failure, verify the signature against known issues (section 3) before
  continuing the train; on an unknown signature, stop the train and fix forward.
- End the train only when `main`'s push run is fully green — including the jobs
  that were expected-red mid-train.

**Red flags:** declaring a train complete when the last PR merged but `main`'s run
is still executing; continuing a train past an unexplained red on `main`.

---

## 8. Release and publication gates stay immutable-first

The release pipeline (`.github/workflows/release.yml`) encodes safety properties
that conflict resolutions and refactors must preserve:

**Do:**
- Keep the tag ↔ `release-manifest.json` verification (`verify_release.py`) as the
  first gate; version strings are derived from the manifest, never hand-typed.
- Preserve the check-before-publish / verify-after-publish pattern around
  immutable registries (`verify_published_artifacts.py` states `absent`/present):
  publication must be **resumable** — a rerun after partial failure skips
  already-published identical artifacts and fails on mismatches.
- Publish the exact built-and-checksummed artifact (`SHA256SUMS`, npm
  `--provenance`), not a rebuild in the publish job.
- Keep release jobs' permissions minimal (`contents: read`, `id-token: write`
  only where OIDC publishing needs it).

**Red flags:** a publish step without a preceding registry-state check; rebuilding
artifacts in a publish job; a workflow diff that widens `permissions:`; version
numbers derived from anything but the manifest.
