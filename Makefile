# The handful of things you actually type. Everything here is a shortcut for a
# command in the README, not a new way of doing anything.

.PHONY: help up down restart logs doctor test lint fmt check shell

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up:  ## Build and start the control plane
	# --wait holds until the container reports healthy. Without it the doctor
	# below runs against a port that is bound but not yet serving, and reports a
	# failure that fixes itself two seconds later.
	docker compose up -d --build --wait
	@echo
	@$(MAKE) --no-print-directory doctor

down:  ## Stop the control plane
	docker compose down

restart: down up  ## Stop, rebuild, start

logs:  ## Follow the control plane's logs
	docker compose logs -f

doctor:  ## Check the configuration and say what is wrong with it
	@uv run halyard doctor

test:  ## Run the test suite
	uv run pytest -q

lint:  ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt:  ## Format
	uv run ruff format .

check: lint test  ## What CI runs

# A process cannot set its parent shell's environment — that is an operating
# system rule, not a missing feature — so this prints the lines rather than
# installing them. Same shape as pyenv or direnv:
#
#     eval "$(make shell)"          for this terminal
#     make shell >> ~/.zshrc        for every terminal from now on
shell:  ## Print shell aliases for launching a navigator or a driver
	@echo "# Halyard: label a Claude Code session so its approvals reach the right chat."
	@echo "alias nav='HALYARD_ROLE=navigator claude'"
	@echo "alias drv='HALYARD_ROLE=driver claude'"
