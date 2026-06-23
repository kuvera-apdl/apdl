.PHONY: all setup deps build test clean lint check fmt fmt-check dev dev-all dev-down install-hooks lint-staged migrate-clickhouse test-sdk-python lint-sdk-python setup-sdk release-sdk status smoke run-admin build-admin test-admin lint-admin clean-admin

# ─── Top-Level ───────────────────────────────────────────────

all: build

CLICKHOUSE_COMPOSE_FILE ?= infra/docker/docker-compose.deps.yml

# Full-stack Compose command. `-f infra/docker/docker-compose.yml` makes Compose
# use that folder as its project dir (and its default `.env` lookup), so the
# repo-root `.env` is otherwise ignored — load it explicitly when it exists.
COMPOSE_FILE ?= infra/docker/docker-compose.yml
COMPOSE := docker compose $(if $(wildcard .env),--env-file .env,) -f $(COMPOSE_FILE)

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
	@echo "==> Installing Admin Console dependencies"
	cd services/admin && npm install
	@echo "==> Setting up Ingestion service"
	cd services/ingestion && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Config service"
	cd services/config && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Query service"
	cd services/query && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Agents service"
	cd services/agents && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Codegen service"
	cd services/codegen && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Pipeline"
	cd pipeline/redis && uv venv --python 3.12 .venv && uv pip install -r requirements.txt --python .venv/bin/python
	@echo "==> Setting up ETL framework"
	cd pipeline/etl && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python
	@echo "==> Setting up Python SDK"
	cd sdk/python && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" --python .venv/bin/python

build: build-sdk build-admin

test: test-sdk test-sdk-python test-ingestion test-config test-query test-agents test-codegen test-etl test-admin

lint: lint-sdk lint-sdk-python lint-ingestion lint-config lint-query lint-agents lint-codegen lint-etl lint-admin

clean: clean-sdk clean-admin

# Parallel local CI mirror: lint + test every package at once.
check:
	@bash scripts/check.sh

# Auto-format all packages (ruff format + autofix; JS formatter if present).
fmt:
	@bash scripts/fmt.sh

fmt-check:
	@bash scripts/fmt.sh --check

# ─── SDK (JavaScript) ─────────────────────────────────────────────────────

build-sdk:
	cd sdk/javascript && npm run build

setup-sdk:
	cd sdk/javascript && npm run setup

test-sdk:
	cd sdk/javascript && npm test

clean-sdk:
	rm -rf sdk/javascript/dist sdk/javascript/node_modules

lint-sdk:
	cd sdk/javascript && npm run lint

release-sdk:
	cd sdk/javascript && npm run release:check

# ─── Admin Console (TypeScript) ──────────────────────────────

run-admin:
	cd services/admin && npm run dev

build-admin:
	cd services/admin && npm run build

test-admin:
	cd services/admin && npm test

lint-admin:
	cd services/admin && npm run lint

clean-admin:
	rm -rf services/admin/dist services/admin/node_modules

# ─── SDK (Python) ────────────────────────────────────────────

test-sdk-python:
	cd sdk/python && .venv/bin/python -m pytest -q --cov=apdl --cov-report=term-missing --cov-fail-under=88

lint-sdk-python:
	cd sdk/python && .venv/bin/ruff check apdl/ tests/

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

# ─── Codegen Service (Python) ────────────────────────────────

test-codegen:
	cd services/codegen && .venv/bin/python -m pytest -v

lint-codegen:
	cd services/codegen && .venv/bin/ruff check app/

run-codegen:
	cd services/codegen && .venv/bin/uvicorn app.main:app --reload --port 8084

build-codegen-sandbox:
	docker build -f services/codegen/Dockerfile.worker -t apdl-codegen-sandbox:latest services/codegen

# ─── Pipeline ────────────────────────────────────────────────

run-pipeline:
	cd pipeline/redis && .venv/bin/python clickhouse_writer.py

test-etl:
	cd pipeline/etl && .venv/bin/python -m pytest -v

lint-etl:
	cd pipeline/etl && .venv/bin/ruff check etl/ scripts/ tests/

new-transform:
	cd pipeline/etl && .venv/bin/python scripts/new_transform.py $(ARGS)

migrate-clickhouse:
	@CLICKHOUSE_COMPOSE_FILE="$(CLICKHOUSE_COMPOSE_FILE)" scripts/init-clickhouse.sh

# ─── Docker ──────────────────────────────────────────────────

dev:
	docker compose -f infra/docker/docker-compose.deps.yml up -d
	@$(MAKE) --no-print-directory migrate-clickhouse CLICKHOUSE_COMPOSE_FILE=infra/docker/docker-compose.deps.yml
	@echo "==> Dependencies running (Redis, ClickHouse, PostgreSQL)"
	@echo "    Run services individually: make run-ingestion, make run-config, make run-query, make run-agents, make run-codegen, make run-pipeline"

dev-all:
	$(COMPOSE) up -d --build redis clickhouse postgres
	@$(MAKE) --no-print-directory migrate-clickhouse CLICKHOUSE_COMPOSE_FILE=$(COMPOSE_FILE)
	$(COMPOSE) up --build ingestion config query agents codegen clickhouse-writer admin gateway

dev-down:
	$(COMPOSE) down
	docker compose -f infra/docker/docker-compose.deps.yml down

# Container status + service health endpoints.
status:
	@bash scripts/dev.sh status

# End-to-end smoke test against the running stack (event → flag → query).
smoke:
	@bash scripts/dev.sh smoke

# ─── CI ──────────────────────────────────────────────────────

ci: lint-sdk test-sdk lint-sdk-python test-sdk-python lint-ingestion lint-config lint-query lint-agents lint-codegen lint-etl test-etl lint-admin test-admin
