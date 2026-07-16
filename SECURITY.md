# Security Policy

## Supported Versions

APDL is pre-1.0; only the latest release receives security fixes.

| Version | Supported |
|---|---|
| 0.1.x (latest) | ✅ |
| older | ❌ |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub
issues.**

Report them privately via
[GitHub Security Advisories](https://github.com/JahaanRawat/apdl/security/advisories/new)
or by email to <jahaan.rawat@gmail.com>.

Include as much of the following as you can:

- The component affected (SDK, ingestion, config, query, agents, pipeline)
- Steps to reproduce or a proof of concept
- Impact assessment (what an attacker could do)

You should receive an acknowledgment within a few days. Please allow a
reasonable window for a fix before any public disclosure.

## Scope notes

- Confidential service credentials use `proj_{project_id}_{secret}`. Treat the
  secret like a password, never embed it in browser code, and never commit it.
- Browser clients use only `client_{project_id}_{token}` credentials. These are
  public, rotatable project identifiers restricted in PostgreSQL to
  `events:write` and `config:read`. They must still be sent in `X-API-Key`,
  never in a URL or query string; they cannot administer flags, query
  analytics, run agents, or invoke Codegen.
- The agents service can hold LLM provider API keys (`OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) via environment variables; these
  must never be exposed to clients.
- Agent-initiated actions on operator-provisioned projects pass through safety
  validation with audit logging. Self-created projects cannot execute Agents,
  and autonomous experiment rollback is disabled in this release; bypasses of
  either boundary are considered high severity.
