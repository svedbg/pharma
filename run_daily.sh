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
LOCK="$ROOT/data/.run.lock"
mkdir -p "$ROOT/data"
exec 9>"$LOCK"
if ! flock --nonblock 9; then
    # Benign: exit 0 so systemd does not record a skipped overlap as a failure.
    echo "another run is already in progress; exiting" >&2
    exit 0
fi

DATE="$(date +%F)"
LOG="$ROOT/logs/$DATE.log"
REPORT="$ROOT/reports/$DATE.md"
mkdir -p "$ROOT/logs" "$ROOT/reports"

# Everything below is teed to the log so a failed unattended run is diagnosable.
exec > >(tee -a "$LOG") 2>&1
echo "=== run $(date -Is) ==="

NO_LLM=0
[[ "${1:-}" == "--no-llm" ]] && NO_LLM=1

fail() {
    echo "FAILED: $1"
    python3 "$ROOT/scripts/notify.py" --failure "Biotech desk run failed on $DATE: $1" || true
    exit 1
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

timeout 1800 claude -p "$PROMPT" \
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
python3 "$ROOT/scripts/notify.py" --report "$REPORT" || echo "WARNING: notify failed"

echo "=== done $(date -Is) ==="
