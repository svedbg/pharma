#!/usr/bin/env bash
#
# Daily biotech desk run. Invoked by the weekday scheduler (a systemd timer on
# Linux, a launchd job on macOS) after the US close. Safe to run by hand at any
# time.
#
#   ./run_daily.sh            full run
#   ./run_daily.sh --no-llm   fetch + signals only (fast, free, no API usage)
#
#   PHARMA_PYTHON=/path/to/python3   try this interpreter first; it is probed
#                                    for tomllib + pyexpat like the rest, and a
#                                    failure warns and falls back to python3,
#                                    then ~/.local/bin/python3
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

# $DATE is when this run happened; it names the log, which is a record of the
# run. The *session* -- which names the report, and which every window in
# signals.json is measured from -- is whatever the newest bar turns out to be,
# so it is not known until after the fetch. See SESSION below.
DATE="$(date +%F)"
LOG="$ROOT/logs/$DATE.log"
mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/reports"

# Log before lock, so every refusal below -- a held lock, an unwritable lock
# path, a failed interpreter probe -- lands in the day's log instead of being
# visible only in the scheduler's journal. An overlapping starter briefly
# shares the append, but it writes two lines and exits, and appends land
# whole; the interleave is cosmetic.
exec > >(tee -a "$LOG") 2>&1
echo "=== run $(date +%Y-%m-%dT%H:%M:%S%z) ==="

usage() {
    cat <<'EOF'
usage: run_daily.sh [--no-llm]

  --no-llm    fetch + signals only. Skips the analysis pass, the archive build
              and notification. Fast, free, no API usage.
  -h, --help  this message.
EOF
}

# Every argument is examined, and an unrecognised one refuses to run. Testing
# only "$1" against the exact string meant `./run_daily.sh --nollm`, or the flag
# in any position but the first, silently performed a *full* run -- the
# expensive direction to be wrong in, and one that leaves nothing behind saying
# the argument was misread.
#
# Not routed through fail(): whoever passed the argument is either watching this
# terminal, or is a scheduler, which records the non-zero exit and whose missing
# report the heartbeat picks up within three weekdays. A typo should not buzz the
# phone; a broken unit still gets caught.
NO_LLM=0
for arg in "$@"; do
    case "$arg" in
        --no-llm)   NO_LLM=1 ;;
        -h|--help)  usage; exit 0 ;;
        *)          echo "unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

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
    # The lock's identity is pid PLUS process start time, not pid alone.
    # `kill -0` fails both ways as an ownership probe: a recycled pid from a
    # SIGKILLed run makes it succeed forever (busy every night, silently), and
    # EPERM on another user's pid makes a live lock look stale. Start time
    # survives neither failure -- a recycled pid has a new one, and ps reads
    # other users' processes where kill cannot signal them.
    lock_id() {
        local started
        started="$(ps -o lstart= -p "$1" 2>/dev/null | sed 's/^ *//;s/ *$//')"
        [[ -n "$started" ]] && echo "$1|$started"
    }
    # Is the process recorded in the lock still the one that took it?
    lock_held() {
        local target="$1" owner="${1%%|*}"
        [[ -n "$owner" ]] || return 1
        if [[ "$target" != *"|"* ]]; then
            # A bare-pid lock written by the previous format. There is no start
            # time to compare, so fall back to the old liveness probe: reaping
            # it on format alone would steal the lock from a run that is still
            # going, which is the one thing this block exists to prevent. Only
            # reachable in the single upgrade window where a pre-existing run
            # is still holding the old-format link.
            kill -0 "$owner" 2>/dev/null
            return
        fi
        # Same pid AND same start time: the recorded owner is genuinely running.
        # RHS quoted -- unquoted it is a glob pattern, not a literal.
        [[ "$(lock_id "$owner")" == "$target" ]]
    }
    SELF_ID="$(lock_id $$)"
    if [[ -z "$SELF_ID" ]]; then
        # No usable `ps -o lstart` (busybox, a stripped container). Record the
        # bare pid and let lock_held fall back with it: the recycled-pid hole
        # reopens, but refusing to start would trade a rare wrong answer for a
        # certain silent nightly no-op, which is the worse of the two.
        echo "WARNING: ps -o lstart unavailable; lock falls back to pid only" >&2
        SELF_ID="$$"
    fi
    if ! ln -s "$SELF_ID" "$LOCKLINK" 2>/dev/null; then
        target="$(readlink "$LOCKLINK" 2>/dev/null || true)"
        owner="${target%%|*}"
        if [[ -z "$target" ]]; then
            # The link vanished between our attempt and the read: its owner
            # just exited. One retry; failing again is genuine contention.
            ln -s "$SELF_ID" "$LOCKLINK" 2>/dev/null || busy
        elif lock_held "$target"; then
            busy
        else
            # Stale lock from a killed run. Reap it by atomic rename, so
            # exactly one contender wins the takeover -- the loser's mv fails
            # and it yields. A third starter that claims the freed path first
            # wins instead: our own claim then fails and we yield to it.
            mv "$LOCKLINK" "$LOCKLINK.reap.$$" 2>/dev/null || busy
            rm -f "$LOCKLINK.reap.$$"
            echo "removed stale lock left by pid ${owner:-unknown}" >&2
            ln -s "$SELF_ID" "$LOCKLINK" 2>/dev/null || busy
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

# --- 0. network -----------------------------------------------------------
# systemd declares After=network-online.target; launchd has no equivalent for a
# calendar job, and a Mac waking at 09:00 runs the missed 23:18 job before Wi-Fi
# has associated. Living here rather than in the launchd job means one wait that
# every scheduler gets -- a hand-rolled cron and a manual run included -- so the
# desk job no longer waits separately. (The heartbeat still does: it never comes
# through this script.)
#
# http://captive.apple.com, not a bare TCP connect: a captive portal completes
# the handshake for anything and would read as "network up" while every fetch
# came back as the portal's login page. It is also the endpoint macOS itself
# probes, so it exercises DNS, TCP and HTTP.
#
# run_with_timeout bounds each attempt because the socket timeout does not: it
# covers connect(), not getaddrinfo(), so a stalled resolver has no ceiling of
# its own -- and this loop runs holding the lock. Worst case is 12 probes capped
# at 6s and 11 pauses of 5s, so a little over two minutes, then it proceeds
# regardless: a genuinely offline machine should fail loudly through fetch.py
# rather than stall here. The happy path costs one probe, ~0.3s.
NET_ATTEMPTS=12
net_ok=0
for attempt in $(seq 1 "$NET_ATTEMPTS"); do
    if run_with_timeout 6 "$PY" -c \
        'import urllib.request; urllib.request.urlopen("http://captive.apple.com", timeout=5).read(64)' \
        >/dev/null 2>&1; then
        net_ok=1
        break
    fi
    [[ "$attempt" -eq 1 ]] && echo "--- waiting for network"
    [[ "$attempt" -eq "$NET_ATTEMPTS" ]] || sleep 5
done
[[ "$net_ok" -eq 1 ]] || echo "WARNING: no network after ~2min -- running anyway" >&2

# --- 1. facts -------------------------------------------------------------
echo "--- fetch"
run_with_timeout 1800 "$PY" "$ROOT/scripts/fetch.py" || fail "fetch.py returned $? (all price sources down?)"

# --- 2. arithmetic --------------------------------------------------------
echo "--- signals"
run_with_timeout 900 "$PY" "$ROOT/scripts/signals.py" || fail "signals.py returned $?"

# The report is named for the session it analyses, not for the day the run
# fired. Those differ whenever the newest bar is not today's -- a hand run
# before the close, a market holiday, or the provider not having published yet
# -- and publish.py pairs reports/<d>.md with data/summaries/<d>.json by that
# name, so naming the report after the wall clock would leave every such day
# with no stats in the archive. Falls back to $DATE only if signals.json cannot
# name a session, which is the same degraded case signals.py warns about.
SESSION="$("$PY" -c '
import json, sys
try:
    print(json.load(open(sys.argv[1]))["session_date"] or "")
except Exception:
    print("")
' "$ROOT/data/signals.json" 2>/dev/null)"
if [[ -z "$SESSION" ]]; then
    echo "WARNING: signals.json names no session; filing the report under $DATE" >&2
    SESSION="$DATE"
fi
[[ "$SESSION" == "$DATE" ]] || echo "[run] session $SESSION (run date $DATE)"
REPORT="$ROOT/reports/$SESSION.md"

# Capture a stage's output into a file only if the stage succeeded. Redirecting
# straight onto the destination truncated it before the command ran, so a
# transient failure replaced the last good scorecard with a Python traceback --
# and the analysis pass reads that file, so the report then quoted the traceback
# as the desk's performance record. Keeping the previous content is strictly
# better: it is stale by one day and says so, rather than being wrong.
capture_if_ok() {
    local label="$1" dest="$2" secs="$3"; shift 3
    local tmp="$dest.partial.$$"
    if run_with_timeout "$secs" "$@" > "$tmp" 2>&1; then
        mv -f "$tmp" "$dest"
        return 0
    fi
    echo "WARNING: $label failed; keeping the previous $(basename "$dest")" >&2
    tail -15 "$tmp" | sed 's/^/    | /' >&2
    rm -f "$tmp"
    return 1
}

# --- 2b. grade past alerts ------------------------------------------------
# Non-fatal: a scoring failure must not cost you the day's report.
echo "--- scorecard"
capture_if_ok score_alerts.py "$ROOT/data/scorecard.txt" 600 \
    "$PY" "$ROOT/scripts/score_alerts.py" || true
tail -20 "$ROOT/data/scorecard.txt" 2>/dev/null || true

# --- 2c. open paper positions ---------------------------------------------
echo "--- paper positions"
capture_if_ok paper.py "$ROOT/data/paper_status.txt" 300 \
    "$PY" "$ROOT/scripts/paper.py" status || true
cat "$ROOT/data/paper_status.txt" 2>/dev/null || true

if [[ $NO_LLM -eq 1 ]]; then
    echo "--- skipping analysis (--no-llm)"
    exit 0
fi

# --- 3. judgement ---------------------------------------------------------
# The report is written by Claude directly; we only check that it appeared.
echo "--- analysis"

# Two runs on different days can name their report for the same session. The
# session is the newest bar, not the run date, so whenever one run wins the race
# with the provider and the next loses it, both land on the same file: Friday's
# run wrote reports/2026-08-14.md at 23:39, Monday's run analysed the same
# Friday bar and wrote straight over it. reports/ is gitignored, so Friday's
# analysis survived only as the attachment on Friday's email.
#
# So displace it first, stamped with the time it was written. The new report
# keeps the canonical name because publish.py pairs reports/<session>.md with
# data/summaries/<session>.json by that name, and the superseded copy is both in
# a subdirectory and outside the YYYY-MM-DD stem the archive globs for, so it is
# preserved without appearing twice in the index. signals.py displaces the
# summary half the same way.
#
# Copied rather than moved: if the analysis pass then produces nothing, moving
# would have emptied the canonical slot and cost the archive a day it already
# had. The hash below is what stops that leniency from turning into a silent
# re-send of a report already delivered.
PRIOR_REPORT=""
PRIOR_HASH=""
if [[ -f "$REPORT" ]]; then
    # $PY, not `date -r`: GNU date reads a file there and BSD date reads epoch
    # seconds, and this script runs under both.
    stamp="$("$PY" -c '
import datetime, os, sys
print(datetime.datetime.fromtimestamp(os.path.getmtime(sys.argv[1])).strftime("%Y%m%dT%H%M%S"))
' "$REPORT" 2>/dev/null)"
    [[ -n "$stamp" ]] || stamp="$(date +%Y%m%dT%H%M%S)"
    mkdir -p "$ROOT/reports/superseded"
    PRIOR_REPORT="$ROOT/reports/superseded/$SESSION.written-$stamp.md"
    if cp -p "$REPORT" "$PRIOR_REPORT" 2>/dev/null; then
        PRIOR_HASH="$("$PY" -c '
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
' "$REPORT" 2>/dev/null)"
        echo "[run] $SESSION already has a report; kept it as superseded/$(basename "$PRIOR_REPORT")"
        # The guard below is skipped without this hash, so an unreadable one
        # would quietly restore the very behaviour it exists to stop.
        [[ -n "$PRIOR_HASH" ]] || echo "WARNING: could not hash $REPORT; an unchanged report will not be detected" >&2
    else
        # Not fatal on its own, but the run is about to overwrite the only copy,
        # so say plainly that the old one is going.
        echo "WARNING: could not preserve the existing report at $REPORT; it will be overwritten" >&2
        PRIOR_REPORT=""
    fi
fi
PROMPT="$(cat "$ROOT/prompts/daily.md")

The session being analysed is $SESSION -- every price, filing age and catalyst
countdown in data/signals.json is measured from that date, not from now. Today's
wall-clock date is $DATE; use $SESSION for anything about the data.
Write the report to reports/$SESSION.md.
Run python scripts with: $PY (e.g. \`$PY scripts/detail.py TICKER\`)."

# The analysis pass drills down via scripts/detail.py, so it needs to run the
# same vetted interpreter as the pipeline -- not whatever python3 happens to be
# on PATH, which on the machine the preflight protects against is precisely the
# interpreter that failed the probe.
ALLOWED_TOOLS="Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Bash(python3:*)"
[[ "$PY" != "python3" ]] && ALLOWED_TOOLS+=",Bash($PY:*)"

# 9>&- closes the flock descriptor for this child and everything it starts. An
# open file descriptor is inherited across fork and exec, so the lock is held by
# any process still holding fd 9 -- not by this script. The analysis pass is the
# one stage that starts a process tree of its own (tool subprocesses, MCP
# servers); if a single grandchild outlived the run, the lock would be held by it
# forever and every later start would exit 0 as "already in progress". Silently,
# nightly, until someone noticed the reports had stopped -- which is precisely
# the failure the lock exists to avoid, arriving through the lock itself.
# Harmless where fd 9 was never opened: the launchd path locks with a symlink.
run_with_timeout 1800 claude -p "$PROMPT" \
    --permission-mode acceptEdits \
    --allowedTools "$ALLOWED_TOOLS" \
    --add-dir "$ROOT" \
    9>&- \
    || echo "WARNING: claude exited $? -- checking for a report anyway"

if [[ ! -f "$REPORT" ]]; then
    fail "no report produced at reports/$SESSION.md"
fi

# A report left byte-identical to the one already on disk is not a new report --
# the analysis pass wrote nothing and this is the previous run's file. Without
# this check the existence test above passes on the old content and the run
# cheerfully emails a report that was already delivered, which is precisely the
# shape of failure the desk cannot see: a full inbox and no new analysis.
if [[ -n "$PRIOR_HASH" ]]; then
    now_hash="$("$PY" -c '
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
' "$REPORT" 2>/dev/null)"
    if [[ "$now_hash" == "$PRIOR_HASH" ]]; then
        # The preserved copy is a duplicate of the live file, so drop it:
        # superseded/ means "a version that said something else".
        [[ -n "$PRIOR_REPORT" ]] && rm -f "$PRIOR_REPORT"
        fail "the analysis pass left reports/$SESSION.md unchanged -- no new report was written"
    fi
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
