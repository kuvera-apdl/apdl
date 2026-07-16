# APDL Support Policy

APDL 0.3.0 is a community-supported developer preview. It has no uptime,
response-time, data-recovery, or compatibility SLA.

## Supported Version

Only the latest `0.3.x` release is supported. Fixes land on the current release
line; older pre-1.0 versions and arbitrary commits from `main` are not
maintained release channels.

## Published Artifacts

The complete 0.3.0 artifact set is:

- source archives from
  [GitHub Releases](https://github.com/kuvera-apdl/apdl/releases);
- [`@apdl-oss/sdk`](https://www.npmjs.com/package/@apdl-oss/sdk) from npm; and
- [`apdl-sdk`](https://pypi.org/project/apdl-sdk/) from PyPI.

No GHCR or other container images are published or supported. Docker Compose
builds runtime images from the source revision checked out by the operator.

## Supported Runtime

The runtime support target is a **fresh, single-node, source-built Docker
Compose installation** on a current Docker release, with Node.js 20.19+ and
Python 3.12 used for source development and package tooling.

The supported core is:

- Ingestion, Config, and Query;
- the Redis-to-ClickHouse writer;
- Admin API and Admin Console;
- the local-development Gateway; and
- Redis, ClickHouse, PostgreSQL, and the checked-in fresh-install migrations.

`make smoke-fresh` is the release's canonical installation proof. The Gateway
and default credentials/configuration are for isolated local development and
must not be treated as hardened public ingress.

## Preview and Unsupported Surfaces

- **Agents:** opt-in operator preview only. Self-registered projects have
  read-only Agents access and cannot execute or approve work. At least one LLM
  provider must be configured for operator-preview execution.
- **Codegen:** source-only offline API/control-plane preview. Branch and
  pull-request publication is disabled and not supported. The Aider editor,
  `.[agent]` dependency extra, `Dockerfile.worker`, sandbox execution, and all
  publication rollout overlays are experimental source only, are not installed
  by the supported stack, and are outside the 0.3.0 dependency/security gate.
- **Unsupported:** ETL v2, Kafka, Flink, Kubernetes, Terraform, multi-replica
  operation, in-place upgrades, backup, restore, disaster recovery, managed
  cloud deployment, production ingress, and production security/SLO claims.

Experimental and design files may remain in the repository, but their presence
does not put them in the release or support contract.

## Getting Help

Before opening a report, reproduce it against an unmodified 0.3.x release and,
where applicable, run `make smoke-fresh` or the affected package test target.
Search existing issues, then open a
[GitHub issue](https://github.com/kuvera-apdl/apdl/issues) containing:

- the exact APDL version or commit and host platform;
- the command or minimal code used;
- expected and actual behavior; and
- sanitized logs or a small reproduction.

Community maintainers prioritize security, data correctness, tenant isolation,
and supported installation failures. Feature requests and unsupported
deployment questions may be closed or redirected without an implementation
commitment.

Never include credentials, personal data, event payloads, or private repository
content in a public issue. Report vulnerabilities only through the private
process in [SECURITY.md](SECURITY.md).
