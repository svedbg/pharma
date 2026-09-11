.PHONY: help setup lint fmt test check check-units run run-fast premarket premarket-fast briefing brief screen site www og site-stats backup

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

check-units:  ## are the installed timers/jobs still what this checkout says?
	@./systemd/check-units.sh; s=$$?; \
	./launchd/install-launchd.sh --check; l=$$?; \
	exit $$((s | l))

run-fast:  ## fetch + signals only, no LLM, no notifications
	./run_daily.sh --no-llm

run:    ## the full daily run
	./run_daily.sh

premarket:  ## the pre-market news pass (07:30 ET; emails, pushes only if urgent)
	./run_premarket.sh

premarket-fast:  ## pre-market fetch + signals + delta only, no LLM, no email
	./run_premarket.sh --no-llm

brief:  ## rebuild the marketing/capability PDF from its HTML source
	@command -v google-chrome >/dev/null || { echo "needs google-chrome"; exit 1; }
	@# Printed from the built page, not the raw source: the build fills the
	@# {{tests}} slot, and everything it adds -- nav, column, meta -- is
	@# screen-only, so the printed result is the source document either way.
	python3 tools/build_site.py --out .brief-build >/dev/null
	google-chrome --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
		--print-to-pdf="$(PWD)/docs/the-91-percent-question.pdf" \
		"file://$(PWD)/.brief-build/brief.html"
	@rm -rf .brief-build
	@echo "wrote docs/the-91-percent-question.pdf"

briefing:  ## rebuild docs/biotech-desk-briefing.pdf from the HTML source
	@command -v google-chrome >/dev/null || { echo "needs google-chrome for headless PDF"; exit 1; }
	google-chrome --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
		--print-to-pdf="$(PWD)/docs/biotech-desk-briefing.pdf" \
		"file://$(PWD)/docs/briefing.html"
	@echo "wrote docs/biotech-desk-briefing.pdf"

screen:  ## monthly: look for candidates OUTSIDE the watchlist (slow, ~500 requests)
	python3 scripts/screen.py --out data/screen_candidates.toml

www:    ## build the public site to _site/ and serve it exactly as Pages will
	python3 tools/build_site.py --serve

site-stats:  ## recompute the operating record shown on the public site (www/stats.toml)
	python3 tools/site_stats.py

og:     ## rebuild www/og.png (dev-only: needs pillow + fonts in tools/fonts/)
	python3 tools/make_og.py

site:   ## build the local report archive and open it
	python3 scripts/publish.py --open

backup: ## snapshot the gitignored layer + history to ~/pharma-backups
	python3 scripts/backup.py
