# The handful of things you actually type. Everything here is a shortcut for a
# command in the README, not a new way of doing anything.

.PHONY: help run doctor sessions init wire unwire verify test lint fmt check

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

run:  ## Start the control plane
	uv run halyard

doctor:  ## Check the configuration and say what is wrong with it
	@uv run halyard doctor

sessions:  ## List the session names this machine can see
	@uv run halyard sessions

init:  ## Build halyard.yaml, wire a project, and check it
	uv run halyard init

# `p` names a project from halyard.yaml, or a directory. With neither, the
# configuration decides — which is the point, and why this does not fall back
# to the working directory: that is almost always the Halyard checkout, and
# gating it means the control plane's own commands go through the hook it is
# serving.
#
#     make wire            every project in halyard.yaml
#     make wire p=alpha    one of them
wire:  ## Put the gate on a project  [p=<project|dir>]
	uv run halyard wire $(p)

unwire:  ## Take the gate off again  [p=<project|dir>]
	uv run halyard unwire $(p)

# Costs real turns on a real model: it drives each runtime into the gate and
# reads what happened. Kept out of `check` for that reason.
verify:  ## Prove the gate stops things, by running into it  [r=<runtime>]
	uv run halyard verify $(r)

test:  ## Run the test suite
	uv run pytest -q

lint:  ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt:  ## Format
	uv run ruff format .

check: lint test  ## What CI runs
