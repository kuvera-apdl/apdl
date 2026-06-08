---
name: structured-pr
description: Create a well-structured pull request for the APDL monorepo. Use when the user asks to "create a PR", "open a pull request", "raise a PR", make commits for a PR branch, or ship the current branch/changes. Follows the shared repo workflow in docs/agent-workflows/structured-pr.md.
---

# Structured PR

This Claude skill is a thin wrapper around the repository-wide workflow.

When triggered, read and follow `docs/agent-workflows/structured-pr.md` from the
repository root. That file is canonical for every agent; do not add divergent PR
workflow instructions to this Claude-specific skill.
