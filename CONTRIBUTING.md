# Contributing to APDL

Thanks for your interest in improving APDL! This guide covers everything you
need to get a change from idea to merged PR.

## Getting set up

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker & Docker Compose,
Node.js 20.19+, Python 3.12+.

```bash
git clone https://github.com/kuvera-apdl/apdl.git
cd apdl
make setup
```

`make setup` creates per-package Python virtualenvs with `uv`, installs npm
dependencies, starts the infrastructure containers (Redis, ClickHouse,
PostgreSQL), runs ClickHouse migrations, and copies `.env.example` → `.env`.

Verify your environment works before making changes:

```bash
make check    # lint + test every package in parallel
```

`scripts/dev.sh` is the master entry point for everything local —
`scripts/dev.sh help` lists setup, stack lifecycle (`up`, `up-core`, `up-full`,
`down`, `reset`), `status`, and an end-to-end `smoke` test. The 0.3.0
developer-preview support boundary is defined in [SUPPORT.md](SUPPORT.md):
core changes must work in the fresh, single-node source-built Compose stack;
Agents is opt-in, Codegen cannot publish, and future pipeline/deployment
scaffolds are not supported runtime surfaces.

## Repository layout

| Path | What lives there |
|---|---|
| `sdk/javascript/` | `@apdl-oss/sdk` — browser TypeScript SDK (Rollup, Vitest) |
| `sdk/python/` | `apdl-sdk` — server-side Python SDK (httpx, Pydantic) |
| `services/ingestion/` | Event ingestion API (FastAPI → Redis Streams) |
| `services/config/` | Feature flags & experiments API (FastAPI, PostgreSQL, SSE) |
| `services/query/` | Analytics query engine (FastAPI, ClickHouse) |
| `services/agents/` | Opt-in operator-preview LLM agents (FastAPI, pgvector) |
| `pipeline/` | Redis Streams → ClickHouse writer; ClickHouse schemas |
| `infra/docker/` | Docker Compose for local dev |

## Development workflow

1. **Branch from `main`** with a descriptive name.
2. **Make your change**, keeping it focused — one logical change per PR.
3. **Add or update tests.** Every package has a test suite:
   - JS SDK: `__tests__/**/*.test.ts` (Vitest)
   - Python packages: `tests/` (pytest)
4. **Run the checks for the package you touched** (fast inner loop):

   ```bash
   make test-sdk          # or test-sdk-python, test-ingestion, test-config,
   make lint-sdk          #    test-query, test-agents — see the Makefile
   ```

   Run a single test while iterating:

   ```bash
   cd sdk/javascript && npm test -- core/client.test.ts
   cd services/query && .venv/bin/python -m pytest tests/test_funnels.py -v
   ```

5. **Run the full suite before pushing:** `make test && make lint`.

Dependency changes must also follow
[docs/dependency-policy.md](docs/dependency-policy.md). Do not auto-merge a
dependency update: review its changelog and license, refresh the package's
canonical lock or build metadata, and run the gates for every affected
artifact.

### Hot-reload service development

```bash
make dev            # start infra deps only (Redis, ClickHouse, PostgreSQL)
make run-ingestion  # :8080 — each run-* target reloads on save
make run-config     # :8081
make run-query      # :8082
make run-agents     # :8083
make run-pipeline   # ClickHouse writer
```

## Conventions

- **Python:** managed with `uv` (not pip directly); each package has its own
  `.venv/`. Lint with `ruff check` using the default config — don't add
  per-package rule overrides.
- **TypeScript:** strict mode, no unused locals/params. `tsc --noEmit` is the
  lint gate.
- **Cross-SDK parity:** the JS SDK, Python SDK, and Config Service share a
  byte-for-byte identical FNV-1a bucketing implementation. If you touch
  hashing or gate evaluation in one place, update all three and keep the
  golden-value parity tests passing.
- **Commit messages:** follow the conventional style used in the history —
  `feat(query): ...`, `fix(sdk): ...`, `docs: ...`, `ci: ...`.
- **Coverage:** the Python SDK enforces `--cov-fail-under=88` in CI.

## Pull requests

- Keep PRs small and self-contained; describe *what* changed and *why*.
- CI must pass: JS SDK lint + tests, Python lint (ruff) + tests for every
  package.
- Update documentation (READMEs, `CHANGELOG.md`) when behavior or public
  APIs change.

## Reporting bugs & proposing features

Open a [GitHub issue](https://github.com/kuvera-apdl/apdl/issues) with:

- What you expected vs. what happened
- Steps to reproduce (ideally against the supported `make dev-core` stack)
- Relevant logs, versions, and platform details

For security vulnerabilities, **do not open a public issue** — see
[SECURITY.md](SECURITY.md).

By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Project roles and decision-making are
described in [GOVERNANCE.md](GOVERNANCE.md).

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
