#!/usr/bin/env bash
#
# Biotech desk run: the desk's record of the session that closed the previous
# evening. Invoked by the scheduler (a systemd timer on Linux, a launchd job on
# macOS) at 09:00 local, the morning after that session. The hour is measured
# rather than derived from the close -- Nasdaq publishes the daily bar overnight,
# not in the evening -- see systemd/pharma-desk.timer for the log it was read
# from and for the two earlier times that were argued from the close and lost.
# Safe to run by hand at any time; it analyses whatever the newest bar is and
# names its report for that session, never for the wall clock.
#
# The pre-market counterpart is run_premarket.sh, which asks what has happened
# since and records nothing. Both source lib/run_preamble.sh for the lock, the
# interpreter probe, run_with_timeout and the network wait.
#
#   ./run_daily.sh              full run
#   ./run_daily.sh --no-llm     fetch + signals only (fast, free, no API usage)
#   ./run_daily.sh --no-email   full run, but nothing goes to the mailbox
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
usage: run_daily.sh [--no-llm] [--no-email]

  --no-llm    fetch + signals only. Skips the analysis pass, the archive build
              and notification. Fast, free, no API usage.
  --no-email  run everything, write the report, send no email -- not the report
              and not a failure notice. ntfy is unaffected and still fires on a
              new setup or an exit. For running the research by hand without
              filling the mailbox. Such a run also writes no delivery record, so
              it cannot overwrite the scheduled run's verdict with one where
              email was never attempted.
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
NO_EMAIL=0
for arg in "$@"; do
    case "$arg" in
        --no-llm)   NO_LLM=1 ;;
        --no-email) NO_EMAIL=1 ;;
        -h|--help)  usage; exit 0 ;;
        *)          echo "unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

# Set before the preamble is sourced, because the preamble's own failure notice
# has to honour it: a hand run told not to mail must not mail its own death.
# A scalar rather than an array -- `"${arr[@]}"` on an empty array is an unbound
# variable under `set -u` in bash 3.2, which is what macOS ships. Unquoted
# expansion is safe for a fixed literal with no spaces and no glob characters,
# and expands to nothing at all when it is empty.
NO_EMAIL_FLAG=""
[[ $NO_EMAIL -eq 1 ]] && NO_EMAIL_FLAG="--no-email"

# --- shared prologue ------------------------------------------------------
# The lock, the interpreter probe, run_with_timeout, capture_if_ok and the
# network wait live in lib/run_preamble.sh because run_premarket.sh needs the
# identical machinery and two copies would drift. Sourcing it acquires the lock
# and sets $PY: everything below assumes both.
RUN_LABEL="Biotech desk run"
RUN_KIND="daily"
# shellcheck source=lib/run_preamble.sh
source "$ROOT/lib/run_preamble.sh"


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
Run python scripts with: $PY
Start with the list-wide view: \`$PY scripts/brief.py\`
Then drill into a name: \`$PY scripts/detail.py TICKER\`
$CAVEMAN_DIRECTIVE"

# The analysis pass drills down via scripts/detail.py, so it needs to run the
# same vetted interpreter as the pipeline -- not whatever python3 happens to be
# on PATH, which on the machine the preflight protects against is precisely the
# interpreter that failed the probe.
# Skill is on the list because the register the report is written in comes from
# the `caveman` skill, and a tool the run cannot call is a directive it cannot
# follow -- it would write a normal-prose report and say nothing about why.
ALLOWED_TOOLS="Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Skill,Bash(python3:*)"
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
# shellcheck disable=SC2086  # deliberately unquoted; see NO_EMAIL_FLAG above
run_with_timeout 300 "$PY" "$ROOT/scripts/notify.py" --report "$REPORT" $NO_EMAIL_FLAG \
    || echo "WARNING: notify failed"

echo "=== done $(date +%Y-%m-%dT%H:%M:%S%z) ==="
