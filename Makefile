# Common tasks. `make help` lists them. Requires uv (https://docs.astral.sh/uv/).

HAYBARN_VERSION ?= 2.0.1rc3
WORKER          := $(CURDIR)/.venv/bin/vgi-nba
PYTHON          := $(CURDIR)/.venv/bin/python
TOOLS           := $(CURDIR)/.tools
HAYBARN_UNITTEST := $(TOOLS)/bin/haybarn-unittest
TRANSPORTS      ?= stdio launch http

.DEFAULT_GOAL := help
.PHONY: help build tools lint test-unit test-sql test check

help: ## List the targets
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## /{printf "  %-12s %s\n",$$1,$$2}' $(MAKEFILE_LIST)

build: ## Install the worker and dev tools into .venv
	uv sync --locked

tools: ## Install the pinned SQL test runner into .tools
	@test -x $(HAYBARN_UNITTEST) || { uv venv -q $(TOOLS) && uv pip install -q -p $(TOOLS) haybarn-unittest==$(HAYBARN_VERSION); }

lint: build ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

test-unit: build ## Offline unit tests (pytest)
	uv run pytest

test-sql: build tools ## SQL tests through DuckDB over stdio, launch and http (TRANSPORTS="stdio" for one)
	@set -e; for t in $(TRANSPORTS); do \
	  WORKER=$(WORKER) HAYBARN_UNITTEST=$(HAYBARN_UNITTEST) TRANSPORT=$$t PYTHON="$(PYTHON)" ci/run-integration.sh; \
	done

test: test-unit test-sql ## Unit + SQL tests

check: lint test ## Everything CI's lint and test jobs run
