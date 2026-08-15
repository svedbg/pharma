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
    exec 9>"$LOCK"
    flock --nonblock 9 || busy
else
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
        # A directory left behind by a killed run would block every later run
        # forever, so take the lock over once its owner is gone.
        owner="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
        if [[ -n "$owner" ]] && kill -0 "$owner" 2>/dev/null; then
            busy
        fi
        echo "removing stale lock from pid ${owner:-unknown}" >&2
        rm -rf "$LOCKDIR"
        mkdir "$LOCKDIR" 2>/dev/null || busy
    fi
    echo $$ > "$LOCKDIR/pid"
    trap 'rm -rf "$LOCKDIR"' EXIT
fi

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
    python3 "$ROOT/scripts/notify.py" --failure "Biotech desk run failed on $DATE: $1" || true
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
python3 "$ROOT/scripts/fetch.py" || fail "fetch.py returned $? (all price sources down?)"

# --- 2. arithmetic --------------------------------------------------------
echo "--- signals"
python3 "$ROOT/scripts/signals.py" || fail "signals.py returned $?"

# --- 2b. grade past alerts ------------------------------------------------
# Non-fatal: a scoring failure must not cost you the day's report.
echo "--- scorecard"
python3 "$ROOT/scripts/score_alerts.py" > "$ROOT/data/scorecard.txt" 2>&1 \
    || echo "WARNING: score_alerts.py failed"
tail -20 "$ROOT/data/scorecard.txt" || true

# --- 2c. open paper positions ---------------------------------------------
echo "--- paper positions"
python3 "$ROOT/scripts/paper.py" status > "$ROOT/data/paper_status.txt" 2>&1 || true
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

# --- 4. delivery ----------------------------------------------------------
echo "--- notify"
python3 "$ROOT/scripts/notify.py" --report "$REPORT" || echo "WARNING: notify failed"

echo "=== done $(date +%Y-%m-%dT%H:%M:%S%z) ==="
