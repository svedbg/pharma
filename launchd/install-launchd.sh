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

usage() {
    cat <<'EOF'
launchd/install-launchd.sh              install or reinstall both jobs
launchd/install-launchd.sh --uninstall  remove both jobs

macOS only. On Linux the equivalent units live in systemd/.
EOF
}

uninstall() {
    for label in "${LABELS[@]}"; do
        launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
        rm -f "$HOME/Library/LaunchAgents/$label.plist"
        echo "uninstalled $label"
    done
}

# Anything unrecognised used to fall through and install, so a typo'd
# `--uninstal` did the exact opposite of what was asked.
case "${1:-}" in
    -h|--help)      usage; exit 0 ;;
    ""|--uninstall) ;;
    *)              echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
esac

# launchctl exists nowhere else; on Linux this otherwise created a stray
# ~/Library/LaunchAgents and only then failed.
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "this installer is macOS-only -- on Linux use systemd/ (see systemd/README.md)" >&2
    exit 1
fi

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
#
# Warnings mirror run_daily.sh: an explicitly requested PHARMA_PYTHON that is
# missing or fails the probe must say so. Falling through in silence is how the
# wrong interpreter ends up baked into the plist with nothing drawing attention
# to it -- and here it is baked in for every future run, not just this one.
find_python() {
    local candidate resolved
    for candidate in "${PHARMA_PYTHON:-}" python3 "$HOME/.local/bin/python3"; do
        [[ -n "$candidate" ]] || continue
        resolved="$(command -v "$candidate" 2>/dev/null || true)"
        if [[ -z "$resolved" ]]; then
            [[ "$candidate" == "${PHARMA_PYTHON:-}" ]] &&
                echo "WARNING: PHARMA_PYTHON=$candidate not found; trying fallbacks" >&2
            continue
        fi
        if "$resolved" -c 'import tomllib, pyexpat' 2>/dev/null; then
            echo "$resolved"
            return 0
        fi
        [[ "$candidate" == "${PHARMA_PYTHON:-}" ]] &&
            echo "WARNING: PHARMA_PYTHON=$candidate fails the tomllib/pyexpat probe; trying fallbacks" >&2
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

# `zsh -lc` sources /etc/zprofile, which runs path_helper: it rebuilds PATH from
# /etc/paths and /etc/paths.d and APPENDS the inherited PATH after that, so the
# claude directory set above arrives at the job demoted to LAST. A stale
# npm-global /usr/local/bin/claude then shadows the binary this installer just
# vetted and printed -- the same shadowing that forced PHARMA_PYTHON to be an
# absolute path rather than a directory on PATH. Re-prepend after the login
# shell has had its say, and before the guard below, so the guard checks the
# binary that will actually run.
claude_path = f'export PATH={q(claude_dir)}:"$PATH"'

# systemd's After=network-online.target has no launchd equivalent for a calendar
# job, and launchd coalesces events missed during sleep -- so a run can start the
# instant the lid opens, before Wi-Fi has associated. Every data source is a
# network call and so is notify_failure(), so without this the lost day is
# silent until the heartbeat notices it two mornings later. Bounded, and it
# proceeds regardless: a genuinely offline machine should still fail loudly
# rather than hang here. captive.apple.com is the endpoint macOS itself probes,
# and it exercises DNS, TCP and HTTP rather than merely asserting a default route.
await_network = (
    'n=0; until curl -sf --max-time 5 -o /dev/null http://captive.apple.com; do '
    'n=$((n + 1)); '
    'if [ $n -ge 24 ]; then echo "WARNING: no network after 120s -- running anyway"; break; fi; '
    'sleep 5; done'
)

# launchd holds StandardOutPath open for the life of the job, so rotation has to
# happen at the END of a run: the trailing bytes follow the descriptor into the
# rotated file, which is where the rest of that run already is, and the next run
# opens a fresh log. journald bounded its own growth; nothing on macOS rotates
# ~/Library/Logs, and at ~100KB a run this file otherwise grows forever while
# run_daily.sh already keeps the full copy under logs/.
LOG_MAX_BYTES = 5 * 1024 * 1024

def finish(log: str) -> str:
    return (
        'rc=$?; echo "=== exit $rc ==="; '
        f'if [ "$(stat -f%z {q(log)} 2>/dev/null || echo 0)" -gt {LOG_MAX_BYTES} ]; '
        f'then mv -f {q(log)} {q(log + ".1")}; fi; '
        'exit $rc'
    )

desk_log = f"{home}/Library/Logs/pharma-desk.log"
heartbeat_log = f"{home}/Library/Logs/pharma-heartbeat.log"

# Start/exit markers bracket every run, so an unattended failure is
# distinguishable from "nothing happened". Each command re-checks its own
# prerequisites at run time: install-time state does not survive a moved
# binary, and the one job that detects silence must not fail silently.
marker = 'echo "=== $(date \'+%Y-%m-%d %H:%M:%S\') {name} start ==="'

desk_cmd = "; ".join([
    marker.format(name="pharma-desk"),
    claude_path,
    f'cd {q(root)} || {{ echo "FATAL: project directory missing"; exit 66; }}',
    'command -v claude >/dev/null || { echo "FATAL: claude not on PATH (re-run launchd/install-launchd.sh)"; exit 127; }',
    '"$PHARMA_PYTHON" -c "import tomllib, pyexpat" || { echo "FATAL: $PHARMA_PYTHON lacks tomllib or pyexpat; re-run launchd/install-launchd.sh"; exit 78; }',
    # Jitter stands in for systemd's RandomizedDelaySec, which launchd lacks.
    # Ahead of the network wait, so the wait is the last thing before the run.
    "sleep $((RANDOM % 240))",
    await_network,
    "./run_daily.sh",
    finish(desk_log),
])

heartbeat_cmd = "; ".join([
    marker.format(name="pharma-heartbeat"),
    f'cd {q(root)} || {{ echo "FATAL: project directory missing"; exit 66; }}',
    'command -v "$PHARMA_PYTHON" >/dev/null || { echo "FATAL: PHARMA_PYTHON missing (re-run launchd/install-launchd.sh)"; exit 78; }',
    "sleep $((RANDOM % 600))",
    # The alarm itself is ntfy and SMTP: off the network this job reports
    # "could not notify" and the silence it exists to break goes unbroken.
    await_network,
    '"$PHARMA_PYTHON" scripts/heartbeat.py',
    finish(heartbeat_log),
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
        "com.pharma.desk", desk_cmd, 23, 18, desk_log),
    "com.pharma.heartbeat": job(
        "com.pharma.heartbeat", heartbeat_cmd, 10, 23, heartbeat_log),
}

for label, data in jobs.items():
    target = agents / f"{label}.plist"
    with open(target, "wb") as f:
        plistlib.dump(data, f)
    print(f"wrote {target}")
PYEOF

# bootout is not fully synchronous: a bootstrap issued while the old instance is
# still tearing down fails transiently ("Bootstrap failed: 5: Input/output
# error", "37: Operation already in progress"). Since the window between the two
# is exactly the window in which the label has nothing installed, retry rather
# than hand back a machine with one job fewer than it started with. The last
# attempt keeps launchctl's own diagnostics on screen.
bootstrap_with_retry() {
    local target="$1" attempt
    for attempt in 1 2 3; do
        if [[ $attempt -lt 3 ]]; then
            launchctl bootstrap "$DOMAIN" "$target" 2>/dev/null && return 0
            sleep 1
        else
            launchctl bootstrap "$DOMAIN" "$target" && return 0
        fi
    done
    return 1
}

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
    if bootstrap_with_retry "$target"; then
        echo "installed $label"
    else
        echo "ERROR: bootstrap failed for $label after 3 attempts -- the previous" >&2
        echo "       instance was removed and nothing replaced it. Re-run this" >&2
        echo "       installer from a GUI session (the $DOMAIN domain must be" >&2
        echo "       reachable)." >&2
        FAILED=1
    fi
done

[[ $FAILED -eq 0 ]] || exit 1

cat <<EOF

  desk:       Mon-Fri 23:18 (after the US close in every DST alignment)
  heartbeat:  Mon-Fri 10:23

  run now:    launchctl kickstart -k $DOMAIN/com.pharma.desk
  status:     launchctl print $DOMAIN/com.pharma.desk | head -20
  logs:       ~/Library/Logs/pharma-desk.log   (rolls to .1 past 5MB)
              $ROOT/logs/\$(date +%F).log
EOF
