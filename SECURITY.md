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

- API keys follow the `proj_{project_id}_{secret}` format and authenticate
  event ingestion. Treat the secret portion like a password — never commit
  real keys.
- The agents service can hold LLM provider API keys (`OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) via environment variables; these
  must never be exposed to clients.
- Agent-initiated actions pass through safety validation with audit logging
  and rollback — issues in that safety layer are considered high severity.
