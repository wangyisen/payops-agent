.DEFAULT_GOAL := help
UV := uv run

.PHONY: help setup lint fmt typecheck test check mock-pay seed

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup: ## Install the Python workspace (all services, dev tools)
	uv sync --all-packages

lint: ## Ruff lint and format check
	$(UV) ruff check .
	$(UV) ruff format --check .

fmt: ## Apply ruff fixes and formatting
	$(UV) ruff check --fix .
	$(UV) ruff format .

typecheck: ## mypy --strict over all services
	$(UV) mypy services/mock-pay/src services/mock-pay/tests

test: ## Run the offline test suite
	$(UV) pytest

check: lint typecheck test ## Everything CI runs

mock-pay: ## Run the mock payments API on http://127.0.0.1:8001 (docs at /docs)
	$(UV) uvicorn mock_pay.app:create_app --factory --port 8001 --reload

seed: ## Regenerate mock-pay seed data and scenario labels
	$(UV) python -m mock_pay.seed --data-dir services/mock-pay/src/mock_pay/data --labels evals/scenarios.json
