.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help install lint format typecheck test cov e2e demo clean

help: ## List available targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-10s %s\n", $$1, $$2}'

install: ## Create the locked virtualenv and install pre-commit hooks
	$(UV) sync --locked
	$(UV) run pre-commit install

lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Apply ruff fixes and formatting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## mypy --strict on src/
	$(UV) run mypy

test: ## Offline test suite (no network, no Docker)
	$(UV) run pytest -q

cov: ## Offline test suite with branch coverage (fails under 85%)
	$(UV) run pytest -q --cov --cov-report=term-missing --cov-report=xml

e2e: ## Network/Docker end-to-end tests
	$(UV) run pytest -q -m e2e

demo: ## End-to-end demo (full pipeline lands with the demo slice)
	$(UV) run reporewind --version

clean: ## Remove caches and coverage output
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
