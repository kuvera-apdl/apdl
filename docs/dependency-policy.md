# Dependency Policy

APDL treats third-party dependencies and release actions as part of the
security boundary. This policy applies to the published JavaScript and Python
SDKs, the supported core, the Agents operator preview, and the offline Codegen
API dependency set installed by the 0.3.0 developer-preview stack. It excludes
unsupported source-only dependency sets that the stack never installs,
including Codegen's `.[agent]` Aider editor/worker extra and the experimental
ETL surface.

## Sources and Updates

- Use the ecosystem-native manifest and package manager already owned by each
  package: npm lockfiles for JavaScript and `uv`/Python project metadata (or the
  writer's requirements file) for Python.
- Add a direct dependency only when a maintained standard-library or existing
  dependency cannot meet the requirement safely.
- Prefer active upstream projects with clear licensing, release history, and a
  vulnerability-reporting process. MIT compatibility does not remove the need
  to review transitive licenses and notices.
- Dependabot checks GitHub Actions, npm, Python, and Docker base-image
  dependencies weekly. Automated pull requests are proposals, never authority
  to merge or publish.
- Security updates take priority over routine version churn. If an upstream fix
  cannot be adopted safely, document the temporary mitigation and tracking
  issue; do not silently suppress the finding.

## Required Review Gates

Every dependency update must:

1. identify the affected direct and transitive packages and review relevant
   upstream release/security notes;
2. update the canonical manifest and lock/build metadata with the repository's
   package manager, without hand-editing generated dependency graphs;
3. pass lint, unit tests, and builds for each affected package;
4. pass packed-consumer checks when an SDK's runtime or package shape changes;
5. pass `make smoke-fresh` when a core runtime, protocol, database driver,
   container base, or shared dependency changes; and
6. preserve the artifact manifest, supported Node.js 20.19+/Python 3.12 floors,
   license files, and canonical repository metadata.

Major updates require an explicit migration note in the pull request. Updates
that alter authentication, serialization, hashing, statistics, database
behavior, LLM calls, or release permissions require review from the owning
maintainer even when tests pass.

## Vulnerability Gate

CI audits the package ecosystems used by published and supported artifacts. A
known exploitable vulnerability in a shipped dependency blocks release until
it is upgraded, removed, or covered by a maintainer-reviewed, time-bounded
exception that documents why the vulnerable path is unreachable and how the
exception will be retired.

Run `make audit-dependencies` locally to execute the same blocking npm audits,
hash-verified core Python lock audits, and resolved Python SDK/Agents/offline
Codegen API audits used by CI. This command intentionally does not certify the
unsupported Codegen editor/worker `.[agent]` extra.

Do not disclose a newly discovered vulnerability in a dependency through a
public update pull request before the project has assessed exposure. Follow
[SECURITY.md](../SECURITY.md).
