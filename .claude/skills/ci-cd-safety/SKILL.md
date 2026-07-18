---
name: ci-cd-safety
description: APDL's CI/CD safety practices — merging stacked PRs bottom-up, GitHub Actions trigger gotchas (base retargets, conflicted PRs getting no runs), verifying green evidence per layer, resolving merge conflicts against main without losing verified pins, observing main's push runs, and release publication gates. Use when merging or babysitting PRs, retargeting bases, resolving conflicts with main, editing workflows, reading CI results, or cutting a release.
---

# CI/CD safety

This Claude skill is a thin wrapper around the repository-wide workflow.

When triggered, read and follow `docs/agent-workflows/ci-cd-safety.md` from the
repository root. That file is canonical for every agent; do not add divergent
CI/CD guidance to this Claude-specific skill.

The core invariants: merge stacked PRs bottom-up with a positive green gate per
layer; never treat "no checks reported" as a pass; fetch fresh refs and verify
resolutions with `git merge-tree` before pushing; prefer the stack tip's verified
pins except where `main` made a deliberate policy change; and watch `main`'s push
run after every merge, verifying failure signatures against known issues before
continuing.
