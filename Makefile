.DEFAULT_GOAL := help
UV := uv run

.PHONY: help setup lint fmt typecheck test check secrets mock-pay seed

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

check: lint typecheck test ## Lint, type-check and test (CI also runs `secrets`)

secrets: ## Scan the git history for secrets with the CI config (needs gitleaks installed)
	@command -v gitleaks >/dev/null || { echo "gitleaks not installed (brew install gitleaks)"; exit 1; }
	gitleaks git --config .gitleaks.toml --redact

mock-pay: ## Run the mock payments API on http://127.0.0.1:8001 (docs at /docs)
	$(UV) uvicorn mock_pay.app:create_app --factory --port 8001 --reload

seed: ## Regenerate mock-pay seed data and scenario labels
	$(UV) python -m mock_pay.seed --data-dir services/mock-pay/src/mock_pay/data --labels evals/scenarios.json
