---
name: secure-coding
description: APDL's security conventions for writing and reviewing code — auth, tenant isolation, SQL/injection, XSS, SSRF, secrets, DoS, LLM-agent safety, webhooks, CI/CD, container hardening, consent/privacy/data retention, and capability-readiness/lifecycle integrity. Use when writing or reviewing code that touches any of these, when the user asks to "check security", "review for vulnerabilities", "is this secure", or before merging changes to a service, SDK, pipeline, or infra file.
---

# Secure coding & review

This Claude skill is a thin wrapper around the repository-wide workflow.

When triggered, read and follow `docs/agent-workflows/secure-coding.md` from the
repository root. That file is canonical for every agent; do not add divergent
security guidance to this Claude-specific skill.

Apply only the domains the change actually touches, match the canonical pattern each
domain names, and tie every concern to a concrete tainted-input → sink data flow
rather than flagging patterns in the abstract. For a full pending-branch audit, the
built-in `/security-review` command still applies; this skill governs how the code is
written and reviewed in the first place.
