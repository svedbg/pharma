.PHONY: help setup lint fmt test check run run-fast briefing brief screen

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

brief:  ## rebuild the marketing/capability PDF from its HTML source
	@command -v google-chrome >/dev/null || { echo "needs google-chrome"; exit 1; }
	google-chrome --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
		--print-to-pdf="$(PWD)/docs/the-91-percent-question.pdf" \
		"file://$(PWD)/docs/capability-brief.html"
	@echo "wrote docs/the-91-percent-question.pdf"

briefing:  ## rebuild docs/biotech-desk-briefing.pdf from the HTML source
	@command -v google-chrome >/dev/null || { echo "needs google-chrome for headless PDF"; exit 1; }
	google-chrome --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
		--print-to-pdf="$(PWD)/docs/biotech-desk-briefing.pdf" \
		"file://$(PWD)/docs/briefing.html"
	@echo "wrote docs/biotech-desk-briefing.pdf"

screen:  ## monthly: look for candidates OUTSIDE the watchlist (slow, ~500 requests)
	python3 scripts/screen.py --out data/screen_candidates.toml
