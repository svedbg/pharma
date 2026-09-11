#!/usr/bin/env bash
# Record a real, unedited `./run_daily.sh --no-llm` sweep and write www/demo.svg,
# an animated SVG that plays in any browser with no JS and no third-party embed.
# Then uncomment the demo <section> in www/index.html.
#
#   tools/record-demo.sh            # records, converts, done
#   COLS=100 ROWS=28 tools/record-demo.sh
#
# Needs: asciinema (pip install asciinema) and svg-term (npm i -g svg-term-cli).
set -euo pipefail
cd "$(dirname "$0")/.."

command -v asciinema >/dev/null || { echo "pip install asciinema"; exit 1; }
command -v svg-term  >/dev/null || { echo "npm i -g svg-term-cli"; exit 1; }
[ -f watchlist.toml ] || { echo "no watchlist.toml — the demo should show your real sweep, not the example"; exit 1; }

COLS="${COLS:-100}" ROWS="${ROWS:-28}"
CAST="$(mktemp -t desk-demo-XXXX).cast"

echo "recording a --no-llm sweep to $CAST (this is the real run; it takes a few minutes)"
asciinema rec --overwrite --cols "$COLS" --rows "$ROWS" --idle-time-limit 1.5 \
  --command "./run_daily.sh --no-llm" "$CAST"

# idle gaps are already clipped to 1.5s by asciinema above, so a 4-minute sweep
# plays in well under a minute. No window chrome, to sit flat on the page.
svg-term --in "$CAST" --out www/demo.svg --no-cursor \
  --width "$COLS" --height "$ROWS" --padding 14 --term iterm2 --profile Seti

echo "wrote www/demo.svg ($(du -k www/demo.svg | cut -f1) KB)"
echo "review it — entry zones and invalidation levels print in the signal table if you have them set."
echo "then remove the comment wrapper around the demo <section> in www/index.html and run: make www"
