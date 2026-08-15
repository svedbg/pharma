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

DATE="$(date +%F)"
LOG="$ROOT/logs/$DATE.log"
REPORT="$ROOT/reports/$DATE.md"
mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/reports"

# Log before lock, so every refusal below -- a held lock, an unwritable lock
# path, a failed interpreter probe -- lands in the day's log instead of being
# visible only in the scheduler's journal. An overlapping starter briefly
# shares the append, but it writes two lines and exits, and appends land
# whole; the interleave is cosmetic.
exec > >(tee -a "$LOG") 2>&1
echo "=== run $(date +%Y-%m-%dT%H:%M:%S%z) ==="

NO_LLM=0
[[ "${1:-}" == "--no-llm" ]] && NO_LLM=1

busy() {
    # Benign: exit 0 so the scheduler does not record a skipped overlap as a failure.
    echo "another run is already in progress; exiting" >&2
    exit 0
}

# notify.py is deliberately runnable on any python3 -- stdlib-only, no 3.11
# features -- so the alarm can be raised even when the interpreter probe below
# is exactly what failed. Tries the vetted interpreter first, then anything.
notify_failure() {
    local c
    for c in "${PY:-}" "${PHARMA_PYTHON:-}" python3 "$HOME/.local/bin/python3"; do
        [[ -n "$c" ]] || continue
        command -v "$c" >/dev/null 2>&1 || continue
        "$c" "$ROOT/scripts/notify.py" --failure "$1" && return 0
    done
    echo "WARNING: could not send the failure notification" >&2
    return 1
}

fail() {
    echo "FAILED: $1"
    notify_failure "Biotech desk run failed on $DATE: $1" || true
    exit 1
}

# --- one run at a time ------------------------------------------------------
# A manual run colliding with the scheduled one shares the log file, races on
# state/alerts.json and interleaves sqlite writes -- all of which happened
# during development.
#
# flock(1) is util-linux and absent on macOS/BSD, where the old unconditional
# call exited 127 and was read as "already running": the job exited 0 having
# silently done nothing. Where flock is missing, a symlink whose *target* is
# the owner's pid carries the lock: creation is atomic and the pid is embedded
# in the same operation, so there is no window in which the lock exists but
# its owner is unknown.
LOCK="$ROOT/data/.run.lock"
LOCKLINK="$ROOT/data/.run.lock.owner"

if command -v flock >/dev/null 2>&1; then
    # Without `set -e` a failed `exec` redirection does NOT stop the script; it
    # returns 1 and carries on, and flock then fails on a bad descriptor. An
    # unwritable lock file is not an overlap.
    exec 9>"$LOCK" || {
        echo "FATAL: cannot open lock file $LOCK" >&2
        exit 1
    }
    # -n rather than --nonblock: the short spelling is the portable one.
    # Only status 1 means "held by another process"; anything else (unsupported
    # option, ENOLCK on a network filesystem) is a real error, and reporting it
    # as a benign overlap would be a silent nightly no-op.
    flock -n 9
    rc=$?
    case $rc in
        0) ;;
        1) busy ;;
        *) echo "FATAL: flock exited $rc -- an error, not contention" >&2
           exit 1 ;;
    esac
else
    if ! ln -s "$$" "$LOCKLINK" 2>/dev/null; then
        owner="$(readlink "$LOCKLINK" 2>/dev/null || true)"
        if [[ -z "$owner" ]]; then
            # The link vanished between our attempt and the read: its owner
            # just exited. One retry; failing again is genuine contention.
            ln -s "$$" "$LOCKLINK" 2>/dev/null || busy
        elif kill -0 "$owner" 2>/dev/null; then
            busy
        else
            # Stale lock from a killed run. Reap it by atomic rename, so
            # exactly one contender wins the takeover -- the loser's mv fails
            # and it yields. A third starter that claims the freed path first
            # wins instead: our own claim then fails and we yield to it.
            mv "$LOCKLINK" "$LOCKLINK.reap.$$" 2>/dev/null || busy
            rm -f "$LOCKLINK.reap.$$"
            echo "removed stale lock left by pid $owner" >&2
            ln -s "$$" "$LOCKLINK" 2>/dev/null || busy
        fi
    fi
    trap 'rm -f "$LOCKLINK"' EXIT
fi

# --- interpreter preflight ---------------------------------------------------
# Everything under scripts/ is stdlib-only, but two stdlib pieces are not
# guaranteed to be present: tomllib (3.11+) and pyexpat, which parses the Form 4
# ownership XML. An interpreter missing pyexpat runs the entire desk and merely
# records status=degraded -- the insider layer, which CLAUDE.md calls the
# strongest available evidence for refuting a veto, just quietly disappears from
# every report. Refuse to start instead of producing a plausible-looking one;
# the refusal is logged above and notified below, so it is a loud failure.
PY=""
for candidate in "${PHARMA_PYTHON:-}" python3 "$HOME/.local/bin/python3"; do
    [[ -n "$candidate" ]] || continue
    if ! command -v "$candidate" >/dev/null 2>&1; then
        [[ "$candidate" == "${PHARMA_PYTHON:-}" ]] &&
            echo "WARNING: PHARMA_PYTHON=$candidate not found; trying fallbacks" >&2
        continue
    fi
    if "$candidate" -c 'import tomllib, pyexpat' 2>/dev/null; then
        PY="$candidate"
        break
    fi
    # An explicitly requested interpreter that fails the probe must say so --
    # silently falling through to another python is how the wrong one ends up
    # in use without anything drawing attention to it.
    [[ "$candidate" == "${PHARMA_PYTHON:-}" ]] &&
        echo "WARNING: PHARMA_PYTHON=$candidate fails the tomllib/pyexpat probe; trying fallbacks" >&2
done
if [[ -z "$PY" ]]; then
    fail "no python3 with both tomllib (3.11+) and pyexpat found -- install one (e.g. 'uv python install 3.12 --default') or set PHARMA_PYTHON"
fi
# Say which one, so a fallback is never mistaken for the interpreter on PATH.
[[ "$PY" == "python3" ]] || echo "[run] python: $PY"

# --- bounded execution --------------------------------------------------------
# timeout(1) is GNU coreutils and absent from a stock macOS. Every stage must
# stay bounded regardless: systemd's TimeoutStartSec=3600 has no launchd
# equivalent, and a stage hung on a stalled socket keeps its pid alive, so every
# later start would exit 0 as "already in progress" -- silently, nightly,
# forever.
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
    # The watchdog's sleeps run as background children it waits on, so the TERM
    # trap can reach them: killing a subshell does not kill the sleep it is
    # blocked in, and an unreaped `sleep 1800` outlives every fast run, holding
    # the log descriptor for half an hour.
    (
        s=""
        trap '[[ -n "$s" ]] && kill "$s" 2>/dev/null; exit 0' TERM
        sleep "$secs" & s=$!
        wait "$s" 2>/dev/null || exit 0
        kill -TERM "$pid" 2>/dev/null
        sleep 10 & s=$!
        wait "$s" 2>/dev/null || exit 0
        kill -KILL "$pid" 2>/dev/null
    ) &
    local watchdog=$!
    wait "$pid"; local rc=$?
    kill -TERM "$watchdog" 2>/dev/null
    wait "$watchdog" 2>/dev/null
    return $rc
}

# --- 1. facts -------------------------------------------------------------
echo "--- fetch"
run_with_timeout 1800 "$PY" "$ROOT/scripts/fetch.py" || fail "fetch.py returned $? (all price sources down?)"

# --- 2. arithmetic --------------------------------------------------------
echo "--- signals"
run_with_timeout 900 "$PY" "$ROOT/scripts/signals.py" || fail "signals.py returned $?"

# --- 2b. grade past alerts ------------------------------------------------
# Non-fatal: a scoring failure must not cost you the day's report.
echo "--- scorecard"
run_with_timeout 600 "$PY" "$ROOT/scripts/score_alerts.py" > "$ROOT/data/scorecard.txt" 2>&1 \
    || echo "WARNING: score_alerts.py failed"
tail -20 "$ROOT/data/scorecard.txt" || true

# --- 2c. open paper positions ---------------------------------------------
echo "--- paper positions"
run_with_timeout 300 "$PY" "$ROOT/scripts/paper.py" status > "$ROOT/data/paper_status.txt" 2>&1 || true
cat "$ROOT/data/paper_status.txt" || true

if [[ $NO_LLM -eq 1 ]]; then
    echo "--- skipping analysis (--no-llm)"
    exit 0
fi

# --- 3. judgement ---------------------------------------------------------
# The report is written by Claude directly; we only check that it appeared.
echo "--- analysis"
PROMPT="$(cat "$ROOT/prompts/daily.md")

Today is $DATE. Write the report to reports/$DATE.md.
Run python scripts with: $PY (e.g. \`$PY scripts/detail.py TICKER\`)."

# The analysis pass drills down via scripts/detail.py, so it needs to run the
# same vetted interpreter as the pipeline -- not whatever python3 happens to be
# on PATH, which on the machine the preflight protects against is precisely the
# interpreter that failed the probe.
ALLOWED_TOOLS="Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Bash(python3:*)"
[[ "$PY" != "python3" ]] && ALLOWED_TOOLS+=",Bash($PY:*)"

run_with_timeout 1800 claude -p "$PROMPT" \
    --permission-mode acceptEdits \
    --allowedTools "$ALLOWED_TOOLS" \
    --add-dir "$ROOT" \
    || echo "WARNING: claude exited $? -- checking for a report anyway"

if [[ ! -f "$REPORT" ]]; then
    fail "no report produced at reports/$DATE.md"
fi
echo "--- report: $REPORT ($(wc -l < "$REPORT") lines)"

# --- 3b. local archive ----------------------------------------------------
# Non-fatal: a broken site build must not cost you the report or the email.
echo "--- archive"
run_with_timeout 600 "$PY" "$ROOT/scripts/publish.py" || echo "WARNING: publish.py failed"

# --- 4. delivery ----------------------------------------------------------
echo "--- notify"
run_with_timeout 300 "$PY" "$ROOT/scripts/notify.py" --report "$REPORT" || echo "WARNING: notify failed"

echo "=== done $(date +%Y-%m-%dT%H:%M:%S%z) ==="
