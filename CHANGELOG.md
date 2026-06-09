# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are cut by pushing a `v*` git tag, which publishes `@apdl/sdk` to npm,
`apdl-sdk` to PyPI, and service Docker images to GHCR.

## [Unreleased]

### Added
- `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`, and this changelog
- README for the JavaScript SDK and an `examples/` directory with runnable
  browser and Python examples
- JS SDK: top-level `init` export, so the IIFE (script-tag) bundle supports
  `APDL.init(...)` directly instead of `APDL.APDL.init(...)`

## [0.1.0]

Initial development release.

### Added
- **JavaScript SDK** (`@apdl/sdk`): auto-capture (clicks, page views, forms,
  scroll depth, rage clicks, frontend errors, web vitals), manual tracking,
  local feature gate evaluation with FNV-1a bucketing, SSE flag updates,
  server-driven UI components, consent management / cookieless mode
- **Python SDK** (`apdl-sdk`): server-side client with background batching,
  local gate evaluation (byte-for-byte hash parity with the JS SDK and config
  service), background flag refresh, exposure logging
- **Ingestion Service**: authenticated event batch ingestion → Redis Streams
- **Config Service**: feature flag & experiment CRUD, Redis caching, SSE
  distribution
- **Query Service**: event counts/timeseries/breakdowns with property-filtered
  selectors, funnels, cohorts, retention, experiment statistics
  (frequentist, Bayesian, sequential)
- **Agents Service**: LLM-powered behavior analysis, experiment design,
  personalization, and feature-proposal workflows with safety validation,
  audit logging, and rollback
- **Pipeline**: Redis Streams → ClickHouse writer with batched flushes
- Docker Compose dev stack, ClickHouse migrations, GitHub Actions CI/CD

[Unreleased]: https://github.com/JahaanRawat/apdl/compare/main...HEAD
[0.1.0]: https://github.com/JahaanRawat/apdl/releases
