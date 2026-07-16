# Security Policy

## Supported Versions

APDL is a pre-1.0 developer preview. Only the current `0.3.x` release line
receives security fixes.

| Version | Supported |
|---|---|
| 0.3.x | Yes |
| 0.2.x and earlier | No |
| `main` and untagged builds | No; development only |

Support covers the release artifacts and source-built runtime explicitly
listed in [SUPPORT.md](SUPPORT.md). It does not extend to modified deployments,
unsupported infrastructure, multi-replica operation, upgrades, or backup and
restore procedures.

## Reporting a Vulnerability

Do not report a vulnerability in a public issue, pull request, discussion, or
chat. Use the canonical repository's private
[GitHub Security Advisory form](https://github.com/kuvera-apdl/apdl/security/advisories/new).

Include, where possible:

- the affected artifact, component, and version or commit;
- a minimal reproduction or proof of concept;
- the confidentiality, integrity, availability, cost, or tenant-isolation
  impact;
- whether the issue has been disclosed anywhere else; and
- a safe way for maintainers to validate a proposed fix.

Maintainers will acknowledge a complete report on a best-effort basis, keep the
reporter informed as it is triaged, and coordinate a release and disclosure
window appropriate to the impact. The developer preview has no contractual
response-time SLA.

## High-Risk Boundaries

Reports are especially important when they affect:

- project credentials, browser/client-role restrictions, Admin sessions, or
  cross-project tenant isolation;
- event consent, redaction, accepted-event durability, or analytics integrity;
- Agents provider keys, spend boundaries, project-provenance enforcement, or
  action/audit authorization;
- Codegen repository grants, secret handling, sandboxing, or any path that can
  publish a branch or pull request despite the 0.3.0 offline-only boundary;
- release workflow permissions, dependency provenance, or published npm/PyPI
  artifacts; or
- the local Gateway being mistaken for hardened production ingress.

## Credential and Deployment Notes

- Confidential service credentials use `proj_{project_id}_{secret}`. Treat
  them as passwords; never embed them in browser code or commit them.
- Browser clients use `client_{project_id}_{token}` credentials. They are
  public, rotatable identifiers restricted to `events:write` and
  `config:read`, sent only in `X-API-Key`. They cannot administer flags, query
  analytics, run Agents, or invoke Codegen.
- LLM provider credentials are server-side secrets and must never be exposed
  to clients or included in event, audit, or Codegen artifacts.
- Self-registered projects cannot execute Agents. Autonomous experiment
  decisions and Codegen publication are disabled in 0.3.0. A bypass of any of
  these boundaries should be treated as a high-severity report.
- The shipped Compose/Gateway configuration is for isolated local development.
  It is not a production security perimeter.
