#!/usr/bin/env bash
# Set the GitHub repo's About box, homepage and topics — the discovery surface
# the landing page cannot reach on its own. Idempotent; needs `gh auth login`.
set -euo pipefail

REPO="${1:-svedbg/pharma}"
SITE="${SITE:-https://desk.sved.net/}"

command -v gh >/dev/null || { echo "needs the GitHub CLI: https://cli.github.com"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "run: gh auth login"; exit 1; }

gh repo edit "$REPO" \
  --description "Nightly research desk for small-cap biotech: reads SEC filings, insider trades and short interest, vetoes false dips, alerts only on tier changes. Python stdlib, no API keys, MIT." \
  --homepage "$SITE" \
  --enable-issues \
  --add-topic biotech \
  --add-topic pharma \
  --add-topic trading \
  --add-topic stock-screener \
  --add-topic sec-edgar \
  --add-topic form-4 \
  --add-topic insider-trading \
  --add-topic short-interest \
  --add-topic clinicaltrials-gov \
  --add-topic python \
  --add-topic stdlib \
  --add-topic systemd \
  --add-topic launchd \
  --add-topic claude-code

echo "updated $REPO — About, homepage, 14 topics"
echo "still manual: Settings -> Pages -> Source: GitHub Actions (one click, once)"
