#!/usr/bin/env bash
# Install (or uninstall) the macOS launchd jobs. Idempotent.
#
#   launchd/install-launchd.sh              install / reinstall
#   launchd/install-launchd.sh --uninstall  remove both jobs
#
# The launchd equivalent of systemd/. Same two jobs, same schedule:
#   com.pharma.desk       Mon-Fri 23:18  the daily run
#   com.pharma.heartbeat  Mon-Fri 10:23  alerts if no report for two weekdays
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABELS=(com.pharma.desk com.pharma.heartbeat)
DOMAIN="gui/$(id -u)"

uninstall() {
    for label in "${LABELS[@]}"; do
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
        rm -f "$HOME/Library/LaunchAgents/$label.plist"
        echo "uninstalled $label"
    done
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# launchd starts jobs from a clean environment and `zsh -lc` does not source
# .zshrc, so anything a version manager puts on PATH interactively is invisible
# to the job. Resolve the two binaries that matter now and bake their
# directories into the plist, rather than discovering at 23:18 that they are gone.

CLAUDE_BIN="$(command -v claude || true)"
if [[ -z "$CLAUDE_BIN" ]]; then
    echo "claude not found on PATH -- install Claude Code, or drop the analysis" >&2
    echo "pass and schedule './run_daily.sh --no-llm' instead" >&2
    exit 1
fi
CLAUDE_DIR="$(cd "$(dirname "$CLAUDE_BIN")" && pwd)"

# tomllib proves 3.11+; pyexpat proves the Form 4 insider XML will actually
# parse. A python missing pyexpat does not fail -- it silently drops the insider
# layer and marks the snapshot `degraded`, so it must be caught here.
find_python() {
    local candidate
    for candidate in "${PHARMA_PYTHON:-}" python3 "$HOME/.local/bin/python3"; do
        [[ -n "$candidate" ]] || continue
        candidate="$(command -v "$candidate" 2>/dev/null || true)"
        [[ -n "$candidate" ]] || continue
        if "$candidate" -c 'import tomllib, pyexpat' 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "no usable python3 found (needs 3.11+ with a working pyexpat)." >&2
    echo "install one, e.g.:  uv python install 3.12 --default" >&2
    exit 1
fi
PYTHON_DIR="$(cd "$(dirname "$PYTHON_BIN")" && pwd)"

echo "claude:  $CLAUDE_BIN"
echo "python:  $PYTHON_BIN ($("$PYTHON_BIN" -V))"

PATH_EXTRA="$PYTHON_DIR:$CLAUDE_DIR"

for label in "${LABELS[@]}"; do
    target="$HOME/Library/LaunchAgents/$label.plist"
    sed -e "s|__PHARMA_DIR__|$ROOT|g" \
        -e "s|__HOME__|$HOME|g" \
        -e "s|__PATH_EXTRA__|$PATH_EXTRA|g" \
        "$ROOT/launchd/$label.plist" > "$target"

    # Idempotent (re)load: bootout any existing instance, then bootstrap fresh.
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$target"
    launchctl enable "$DOMAIN/$label"
    echo "installed $label -> $target"
done

cat <<EOF

  desk:       Mon-Fri 23:18 (after the US close in every DST alignment)
  heartbeat:  Mon-Fri 10:23

  run now:    launchctl kickstart -k $DOMAIN/com.pharma.desk
  status:     launchctl print $DOMAIN/com.pharma.desk | head -20
  logs:       ~/Library/Logs/pharma-desk.log
              $ROOT/logs/\$(date +%F).log
EOF
