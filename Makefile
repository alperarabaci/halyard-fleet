# The handful of things you actually type. Everything here is a shortcut for a
# command in the README, not a new way of doing anything.

.PHONY: help run doctor sessions test lint fmt check shell

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

run:  ## Start the control plane
	uv run halyard

doctor:  ## Check the configuration and say what is wrong with it
	@uv run halyard doctor

sessions:  ## List the session names this machine can see
	@uv run halyard sessions

test:  ## Run the test suite
	uv run pytest -q

lint:  ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt:  ## Format
	uv run ruff format .

check: lint test  ## What CI runs
