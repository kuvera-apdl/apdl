.PHONY: all setup build test clean lint dev dev-all dev-down install-hooks lint-staged

# ─── Top-Level ───────────────────────────────────────────────

all: build

setup:
	@bash scripts/setup.sh

install-hooks:
	@git config core.hooksPath .githooks
	@echo "==> Git hooks path set to .githooks"
	@echo "    pre-commit: ruff + merge-marker guard on staged files"
	@echo "    pre-push:   pytest for services whose .py files changed in the push range"
	@echo "    Bypass once with: git commit --no-verify  /  git push --no-verify"

lint-staged:
	@.githooks/pre-commit

deps:
	@echo "==> Installing SDK dependencies"
	cd sdk/javascript && npm install
	@echo "==> Setting up Ingestion service"
	cd services/ingestion && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Config service"
	cd services/config && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Query service"
	cd services/query && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Agents service"
	cd services/agents && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Pipeline"
	cd pipeline/redis && uv venv --python 3.12 .venv && uv pip install -r requirements.txt --python .venv/bin/python

build: build-sdk

test: test-sdk test-ingestion test-config test-query test-agents

lint: lint-sdk lint-ingestion lint-config lint-query lint-agents

clean: clean-sdk

# ─── SDK ─────────────────────────────────────────────────────

build-sdk:
	cd sdk/javascript && npm run build

test-sdk:
	cd sdk/javascript && npm test

clean-sdk:
	rm -rf sdk/javascript/dist sdk/javascript/node_modules

lint-sdk:
	cd sdk/javascript && npm run lint

# ─── Ingestion Service (Python) ─────────────────────────────

test-ingestion:
	cd services/ingestion && .venv/bin/python -m pytest -v

lint-ingestion:
	cd services/ingestion && .venv/bin/ruff check app/

run-ingestion:
	cd services/ingestion && .venv/bin/uvicorn app.main:app --reload --port 8080

# ─── Config Service (Python) ────────────────────────────────

test-config:
	cd services/config && .venv/bin/python -m pytest -v

lint-config:
	cd services/config && .venv/bin/ruff check app/

run-config:
	cd services/config && .venv/bin/uvicorn app.main:app --reload --port 8081

# ─── Query Service (Python) ──────────────────────────────────

test-query:
	cd services/query && .venv/bin/python -m pytest -v

lint-query:
	cd services/query && .venv/bin/ruff check app/

run-query:
	cd services/query && .venv/bin/uvicorn app.main:app --reload --port 8082

# ─── Agents Service (Python) ─────────────────────────────────

test-agents:
	cd services/agents && .venv/bin/python -m pytest -v

lint-agents:
	cd services/agents && .venv/bin/ruff check app/

run-agents:
	cd services/agents && .venv/bin/uvicorn app.main:app --reload --port 8083

# ─── Pipeline ────────────────────────────────────────────────

run-pipeline:
	cd pipeline/redis && .venv/bin/python clickhouse_writer.py

migrate-clickhouse:
	@echo "==> Running ClickHouse migrations"
	@for f in pipeline/clickhouse/migrations/*.sql; do \
		echo "  Applying $$f"; \
		clickhouse-client --multiquery < "$$f"; \
	done

# ─── Docker ──────────────────────────────────────────────────

dev:
	docker compose -f infra/docker/docker-compose.deps.yml up -d
	@echo "==> Dependencies running (Redis, ClickHouse, PostgreSQL)"
	@echo "    Run services individually: make run-ingestion, make run-config, make run-query, make run-agents, make run-pipeline"

dev-all:
	docker compose -f infra/docker/docker-compose.yml up --build

dev-down:
	docker compose -f infra/docker/docker-compose.yml down
	docker compose -f infra/docker/docker-compose.deps.yml down

# ─── CI ──────────────────────────────────────────────────────

ci: lint-sdk test-sdk lint-ingestion lint-config lint-query lint-agents
