.PHONY: help setup lint fmt test check run run-fast

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

setup:  ## create the dev virtualenv (the runtime itself needs none)
	python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]'

lint:   ## ruff
	.venv/bin/ruff check .

fmt:    ## ruff, applying safe fixes
	.venv/bin/ruff check . --fix

test:   ## pytest
	.venv/bin/pytest

check: lint test  ## everything CI runs

run-fast:  ## fetch + signals only, no LLM, no notifications
	./run_daily.sh --no-llm

run:    ## the full daily run
	./run_daily.sh
