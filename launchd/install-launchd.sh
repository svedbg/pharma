#!/usr/bin/env bash
# Install (or uninstall) the macOS launchd jobs. Idempotent.
#
#   launchd/install-launchd.sh              install / reinstall
#   launchd/install-launchd.sh --uninstall  remove both jobs
#
# The launchd equivalent of systemd/. Same two jobs, same schedule:
#   com.pharma.desk       Mon-Fri 23:18  the daily run
#   com.pharma.heartbeat  Mon-Fri 10:23  alerts if no report for two weekdays
#
# The plists are generated here with plistlib rather than kept as sed
# templates: a project path containing &, |, <, > or a quote survives XML
# generation and shell quoting, where a sed substitution produced a broken
# plist that -- because bootout ran before bootstrap -- would then uninstall
# the existing job and stop.
#
# Deliberately NOT `set -e`: each label installs independently, so a failure
# on one cannot leave the other silently untouched.
set -uo pipefail

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
# to the job. Resolve the two binaries that matter now, and bake the python in
# as an ABSOLUTE PATH: prepending only its directory is not enough, because a
# stock /usr/local/bin holds Apple's python3 (3.9) which would shadow the
# vetted interpreter at run time. run_daily.sh honours PHARMA_PYTHON, and the
# jobs receive it via EnvironmentVariables, which launchd passes where a login
# shell variable never arrives.

CLAUDE_BIN="$(command -v claude || true)"
if [[ -z "$CLAUDE_BIN" ]]; then
    echo "claude not found on PATH -- install Claude Code, or drop the analysis" >&2
    echo "pass and schedule './run_daily.sh --no-llm' instead" >&2
    exit 1
fi
CLAUDE_DIR="$(cd "$(dirname "$CLAUDE_BIN")" && pwd)"

# tomllib proves 3.11+; pyexpat proves the Form 4 insider XML will actually
# parse. A python missing pyexpat does not fail -- it silently drops the
# insider layer and marks the snapshot `degraded`, so it must be caught here.
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

echo "claude:  $CLAUDE_BIN"
echo "python:  $PYTHON_BIN ($("$PYTHON_BIN" -V))"

if [[ ! -f "$HOME/.config/pharma/pharma.env" ]]; then
    echo "warning: ~/.config/pharma/pharma.env is missing -- see pharma.env.example" >&2
fi

# Generate both plists. Values reach python through the environment, never by
# interpolating into code.
PHARMA_ROOT="$ROOT" CLAUDE_DIR="$CLAUDE_DIR" PYTHON_BIN="$PYTHON_BIN" \
"$PYTHON_BIN" - <<'PYEOF' || exit 1
import os
import plistlib
import shlex
from pathlib import Path

root = os.environ["PHARMA_ROOT"]
claude_dir = os.environ["CLAUDE_DIR"]
python_bin = os.environ["PYTHON_BIN"]
home = str(Path.home())
agents = Path(home) / "Library" / "LaunchAgents"

q = shlex.quote

# Environment for both jobs. PHARMA_PYTHON is the vetted interpreter as an
# absolute path; run_daily.sh tries it first. PATH carries claude's directory
# plus a sane base -- the python is reached via PHARMA_PYTHON, not via PATH.
env = {
    "PHARMA_PYTHON": python_bin,
    "PATH": f"{claude_dir}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
}

# Start/exit markers bracket every run, so an unattended failure is
# distinguishable from "nothing happened". Each command re-checks its own
# prerequisites at run time: install-time state does not survive a moved
# binary, and the one job that detects silence must not fail silently.
marker = 'echo "=== $(date \'+%Y-%m-%d %H:%M:%S\') {name} start ==="'

desk_cmd = "; ".join([
    marker.format(name="pharma-desk"),
    f'cd {q(root)} || {{ echo "FATAL: project directory missing"; exit 66; }}',
    'command -v claude >/dev/null || { echo "FATAL: claude not on PATH (re-run launchd/install-launchd.sh)"; exit 127; }',
    '"$PHARMA_PYTHON" -c "import tomllib, pyexpat" || { echo "FATAL: $PHARMA_PYTHON lacks tomllib or pyexpat; re-run launchd/install-launchd.sh"; exit 78; }',
    # Jitter stands in for systemd's RandomizedDelaySec, which launchd lacks.
    "sleep $((RANDOM % 240))",
    "./run_daily.sh",
    'rc=$?; echo "=== exit $rc ==="; exit $rc',
])

heartbeat_cmd = "; ".join([
    marker.format(name="pharma-heartbeat"),
    f'cd {q(root)} || {{ echo "FATAL: project directory missing"; exit 66; }}',
    'command -v "$PHARMA_PYTHON" >/dev/null || { echo "FATAL: PHARMA_PYTHON missing (re-run launchd/install-launchd.sh)"; exit 78; }',
    "sleep $((RANDOM % 600))",
    '"$PHARMA_PYTHON" scripts/heartbeat.py',
    'rc=$?; echo "=== exit $rc ==="; exit $rc',
])

def weekdays(hour: int, minute: int) -> list[dict]:
    return [{"Weekday": w, "Hour": hour, "Minute": minute} for w in range(1, 6)]

def job(label: str, cmd: str, hour: int, minute: int, log: str) -> dict:
    return {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", "-lc", cmd],
        "EnvironmentVariables": env,
        # 23:18 Europe/Sofia is after the US close in every DST alignment.
        # launchd coalesces calendar events missed during SLEEP into one run at
        # wake; a machine powered OFF over the trigger skips that day.
        "StartCalendarInterval": weekdays(hour, minute),
        # stdout and stderr share one file on purpose: the run markers and the
        # pre-log diagnostics (FATALs, lock messages) belong in the same
        # stream, the way journald interleaved them.
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "RunAtLoad": False,
    }

jobs = {
    "com.pharma.desk": job(
        "com.pharma.desk", desk_cmd, 23, 18, f"{home}/Library/Logs/pharma-desk.log"),
    "com.pharma.heartbeat": job(
        "com.pharma.heartbeat", heartbeat_cmd, 10, 23, f"{home}/Library/Logs/pharma-heartbeat.log"),
}

for label, data in jobs.items():
    target = agents / f"{label}.plist"
    with open(target, "wb") as f:
        plistlib.dump(data, f)
    print(f"wrote {target}")
PYEOF

# (Re)load. Order matters: enable first, so a previously disabled service does
# not make bootstrap fail; then bootout the old instance; then bootstrap the
# new one. Each label stands alone -- a failure on one is reported and the
# other still installs, because "reinstall" must never end with fewer jobs
# than it started with.
FAILED=0
for label in "${LABELS[@]}"; do
    target="$HOME/Library/LaunchAgents/$label.plist"
    launchctl enable "$DOMAIN/$label" 2>/dev/null || true
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    if launchctl bootstrap "$DOMAIN" "$target"; then
        echo "installed $label"
    else
        echo "ERROR: bootstrap failed for $label -- the previous instance was" >&2
        echo "       removed and nothing replaced it. Re-run this installer" >&2
        echo "       from a GUI session (the $DOMAIN domain must be reachable)." >&2
        FAILED=1
    fi
done

[[ $FAILED -eq 0 ]] || exit 1

cat <<EOF

  desk:       Mon-Fri 23:18 (after the US close in every DST alignment)
  heartbeat:  Mon-Fri 10:23

  run now:    launchctl kickstart -k $DOMAIN/com.pharma.desk
  status:     launchctl print $DOMAIN/com.pharma.desk | head -20
  logs:       ~/Library/Logs/pharma-desk.log
              $ROOT/logs/\$(date +%F).log
EOF
