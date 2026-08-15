#!/usr/bin/env bash
#
# Daily biotech desk run. Invoked by the systemd timer on weekdays after the
# US close. Safe to run by hand at any time.
#
#   ./run_daily.sh            full run
#   ./run_daily.sh --no-llm   fetch + signals only (fast, free, no API usage)
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

# Only one run at a time. A manual run colliding with the scheduled one shares
# the log file, races on state/alerts.json and interleaves sqlite writes -- all
# of which happened during development. Re-exec under an exclusive lock.
#
# flock(1) is util-linux and does not exist on macOS/BSD, where the old
# unconditional `flock --nonblock 9` exited 127 and was read as "already
# running": the job then exited 0 having silently done nothing, which looks
# identical to a clean run in every log and every scheduler. mkdir is atomic
# everywhere, so it carries the lock where flock is absent.
LOCK="$ROOT/data/.run.lock"
LOCKDIR="$ROOT/data/.run.lock.d"
mkdir -p "$ROOT/data"

busy() {
    # Benign: exit 0 so the scheduler does not record a skipped overlap as a failure.
    echo "another run is already in progress; exiting" >&2
    exit 0
}

if command -v flock >/dev/null 2>&1; then
    # Without `set -e` a failed `exec` redirection does NOT stop the script: it
    # returns 1 and carries on, after which flock fails on a bad descriptor and
    # the run would exit 0 as a benign overlap. An unwritable lock file is not
    # an overlap, and reporting it as one is the very bug this replaced.
    exec 9>"$LOCK" || {
        echo "FATAL: cannot open lock file $LOCK" >&2
        exit 1
    }
    flock --nonblock 9 || busy
else
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
        # mkdir fails both when another run holds the lock and when the path is
        # unwritable; only the first is benign. Contention always leaves the
        # directory there, so its absence means a real error.
        if [[ ! -d "$LOCKDIR" ]]; then
            echo "FATAL: cannot create lock directory $LOCKDIR" >&2
            exit 1
        fi
        # A directory left behind by a killed run would block every later run
        # forever, so take the lock over once its owner is gone.
        owner="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
        if [[ -n "$owner" ]] && kill -0 "$owner" 2>/dev/null; then
            busy
        fi
        echo "removing stale lock from pid ${owner:-unknown}" >&2
        rm -rf "$LOCKDIR"
        # Losing this one is genuine contention: the directory was writable a
        # moment ago, so another run took it in between.
        mkdir "$LOCKDIR" 2>/dev/null || busy
    fi
    echo $$ > "$LOCKDIR/pid"
    trap 'rm -rf "$LOCKDIR"' EXIT
fi

# Everything under scripts/ is stdlib-only, but two stdlib pieces are not
# guaranteed to be present: tomllib (3.11+) and pyexpat, which parses the Form 4
# ownership XML. An interpreter missing pyexpat runs the entire desk and merely
# records status=degraded -- the insider layer, which CLAUDE.md calls the
# strongest available evidence for refuting a veto, just quietly disappears from
# every report. Refuse to start instead of producing a plausible-looking one.
PY=""
for candidate in "${PHARMA_PYTHON:-}" python3 "$HOME/.local/bin/python3"; do
    [[ -n "$candidate" ]] || continue
    if "$candidate" -c 'import tomllib, pyexpat' 2>/dev/null; then
        PY="$candidate"
        break
    fi
done
if [[ -z "$PY" ]]; then
    echo "FATAL: no python3 with both tomllib (3.11+) and pyexpat (XML) found." >&2
    echo "Install one, e.g.  uv python install 3.12 --default" >&2
    echo "or point PHARMA_PYTHON at an existing interpreter." >&2
    exit 1
fi
# Say which one, so a fallback is never mistaken for the interpreter on PATH.
[[ "$PY" == "python3" ]] || echo "[run] python: $PY"

DATE="$(date +%F)"
LOG="$ROOT/logs/$DATE.log"
REPORT="$ROOT/reports/$DATE.md"
mkdir -p "$ROOT/logs" "$ROOT/reports"

# Everything below is teed to the log so a failed unattended run is diagnosable.
exec > >(tee -a "$LOG") 2>&1
echo "=== run $(date +%Y-%m-%dT%H:%M:%S%z) ==="

NO_LLM=0
[[ "${1:-}" == "--no-llm" ]] && NO_LLM=1

fail() {
    echo "FAILED: $1"
    "$PY" "$ROOT/scripts/notify.py" --failure "Biotech desk run failed on $DATE: $1" || true
    exit 1
}

# timeout(1) is GNU coreutils and absent from a stock macOS. The analysis pass
# must stay bounded regardless -- an unattended `claude -p` that hangs would
# hold the run lock until the next one is skipped, silently, every night.
run_with_timeout() {
    local secs="$1"; shift
    local tool
    for tool in timeout gtimeout; do
        if command -v "$tool" >/dev/null 2>&1; then
            "$tool" "$secs" "$@"
            return $?
        fi
    done
    "$@" &
    local pid=$!
    ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null; sleep 10; kill -KILL "$pid" 2>/dev/null ) &
    local watchdog=$!
    wait "$pid"; local rc=$?
    kill "$watchdog" 2>/dev/null   # job finished first: cancel the watchdog
    wait "$watchdog" 2>/dev/null
    return $rc
}

# --- 1. facts -------------------------------------------------------------
echo "--- fetch"
"$PY" "$ROOT/scripts/fetch.py" || fail "fetch.py returned $? (all price sources down?)"

# --- 2. arithmetic --------------------------------------------------------
echo "--- signals"
"$PY" "$ROOT/scripts/signals.py" || fail "signals.py returned $?"

# --- 2b. grade past alerts ------------------------------------------------
# Non-fatal: a scoring failure must not cost you the day's report.
echo "--- scorecard"
"$PY" "$ROOT/scripts/score_alerts.py" > "$ROOT/data/scorecard.txt" 2>&1 \
    || echo "WARNING: score_alerts.py failed"
tail -20 "$ROOT/data/scorecard.txt" || true

# --- 2c. open paper positions ---------------------------------------------
echo "--- paper positions"
"$PY" "$ROOT/scripts/paper.py" status > "$ROOT/data/paper_status.txt" 2>&1 || true
cat "$ROOT/data/paper_status.txt" || true

if [[ $NO_LLM -eq 1 ]]; then
    echo "--- skipping analysis (--no-llm)"
    exit 0
fi

# --- 3. judgement ---------------------------------------------------------
# The report is written by Claude directly; we only check that it appeared.
echo "--- analysis"
PROMPT="$(cat "$ROOT/prompts/daily.md")

Today is $DATE. Write the report to reports/$DATE.md."

run_with_timeout 1800 claude -p "$PROMPT" \
    --permission-mode acceptEdits \
    --allowedTools "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Bash(python3:*)" \
    --add-dir "$ROOT" \
    || echo "WARNING: claude exited $? -- checking for a report anyway"

if [[ ! -f "$REPORT" ]]; then
    fail "no report produced at reports/$DATE.md"
fi
echo "--- report: $REPORT ($(wc -l < "$REPORT") lines)"

# --- 3b. local archive ----------------------------------------------------
# Non-fatal: a broken site build must not cost you the report or the email.
echo "--- archive"
python3 "$ROOT/scripts/publish.py" || echo "WARNING: publish.py failed"

# --- 4. delivery ----------------------------------------------------------
echo "--- notify"
"$PY" "$ROOT/scripts/notify.py" --report "$REPORT" || echo "WARNING: notify failed"

echo "=== done $(date +%Y-%m-%dT%H:%M:%S%z) ==="
