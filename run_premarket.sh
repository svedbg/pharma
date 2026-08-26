#!/usr/bin/env bash
#
# Pre-market news pass. Invoked by the weekday scheduler at 14:30 Europe/Sofia
# (07:30 ET), about two hours before the 09:30 ET open.
#
# It answers a different question from the nightly run. The 09:00 run is the
# desk's record of the session that just closed; this one asks what has happened
# SINCE, in the hours where nothing shows up in a daily bar: an 8-K filed at
# 06:40 ET, a priced takedown, a catalyst whose date is today, news no
# structured feed carries.
#
# Three properties make it safe to run over a day the nightly run has already
# recorded:
#
#   * fetch.py --no-persist writes nothing to history.sqlite. Without it this
#     pass would consume `new_filings_since_last_run` and the nightly report
#     would never mention the 8-K, having been beaten to it by an email that is
#     not the desk's record of anything.
#   * signals.py --screening with its own --state writes no alert row and no
#     archive summary, and cannot consume a tier transition the nightly run
#     should report.
#   * every artefact lands under data/premarket/, so nothing the nightly run,
#     score_alerts.py or publish.py reads is touched. The report goes to
#     reports/premarket/, which is outside the reports/*.md glob that the
#     archive and the heartbeat use -- a pre-market note must not be able to
#     satisfy the check that asks whether the desk still produces reports.
#
#   ./run_premarket.sh            full pass
#   ./run_premarket.sh --no-llm   fetch + signals + delta only (fast, free)
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

DATE="$(date +%F)"
LOG="$ROOT/logs/$DATE.premarket.log"
# Its own log, not the nightly one. They run 13 hours apart and describe
# different questions; interleaving them into logs/$DATE.log would put the
# morning's news pass and the small hours' report in one file where `tail` shows
# whichever ran last.
PM="$ROOT/data/premarket"
mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/reports/premarket" "$PM" "$ROOT/state"

exec > >(tee -a "$LOG") 2>&1
echo "=== premarket $(date +%Y-%m-%dT%H:%M:%S%z) ==="

usage() {
    cat <<'EOF'
usage: run_premarket.sh [--no-llm]

  --no-llm    fetch + signals + delta only. Skips the analysis pass and the
              email. Fast, free, no API usage.
  -h, --help  this message.
EOF
}

NO_LLM=0
for arg in "$@"; do
    case "$arg" in
        --no-llm)   NO_LLM=1 ;;
        -h|--help)  usage; exit 0 ;;
        *)          echo "unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

# --- shared prologue ------------------------------------------------------
# Same lock as run_daily.sh, deliberately. This pass reads history.sqlite and
# data/ while the nightly run writes both, so they must not overlap -- and at
# 14:30 against 09:00 they never do unless one is a hand run, which is exactly
# when the lock earns its keep. A held lock exits 0: skipping the news pass is
# the right answer when the desk is mid-report.
RUN_LABEL="Biotech desk pre-market pass"
# Which delivery record a failure notice writes. Not inferred from RUN_LABEL:
# this pass failing at 14:30 used to stamp the nightly run's record, so a dead
# nightly run read as healthy because this pass's failure notice had delivered.
RUN_KIND="premarket"
# shellcheck source=lib/run_preamble.sh
source "$ROOT/lib/run_preamble.sh"

# --- 1. facts, recorded nowhere -------------------------------------------
echo "--- fetch (no-persist)"
run_with_timeout 1800 "$PY" "$ROOT/scripts/fetch.py" \
    --out "$PM/latest.json" --no-persist \
    || fail "fetch.py returned $? (all price sources down?)"

# --- 2. arithmetic, in its own namespace ----------------------------------
echo "--- signals (screening)"
run_with_timeout 900 "$PY" "$ROOT/scripts/signals.py" \
    --snapshot "$PM/latest.json" \
    --out "$PM/signals.json" \
    --state "$ROOT/state/premarket_alerts.json" \
    --screening \
    || fail "signals.py returned $?"

# --- 3. what actually changed ---------------------------------------------
# Deterministic, and the only thing allowed to decide that the phone should
# buzz. premarket_delta.py refuses when the two sides name different sessions,
# which is the one failure that would make every line below an artefact rather
# than an event -- so it is fatal here, not warned about.
echo "--- delta"
run_with_timeout 300 "$PY" "$ROOT/scripts/premarket_delta.py" \
    --baseline "$ROOT/data/signals.json" \
    --current "$PM/signals.json" \
    --out "$PM/delta.json" \
    --asof "$DATE" \
    > "$PM/delta.txt" 2>&1 \
    || delta_rc=$?
# $? has to be captured BEFORE anything else runs, or it is the status of that
# other thing. `{ cat ...; fail "... returned $?"; }` reported cat's status, so
# every one of these failures logged "returned 0" -- a line that reads as a
# success and sent whoever went looking to the wrong stage.
if [[ -n "${delta_rc:-}" ]]; then
    cat "$PM/delta.txt"
    fail "premarket_delta.py returned $delta_rc"
fi
cat "$PM/delta.txt"

if [[ $NO_LLM -eq 1 ]]; then
    echo "--- skipping analysis (--no-llm)"
    exit 0
fi

# --- 4. judgement ---------------------------------------------------------
# Named for the RUN date, not for the session. The nightly report is named for
# the session because it is an account of that session; this is an account of a
# morning, and two mornings can follow the same session (a Monday holiday leaves
# Friday's bar newest all through Tuesday). Naming it by session would make the
# second morning overwrite the first, which is the collision run_daily.sh
# already has to displace around.
REPORT="$ROOT/reports/premarket/$DATE.md"

SESSION="$("$PY" -c '
import json, sys
try:
    print(json.load(open(sys.argv[1]))["session_date"] or "")
except Exception:
    print("")
' "$PM/signals.json" 2>/dev/null)"
echo "[premarket] $DATE, against session ${SESSION:-unknown}"

PRIOR_HASH=""
if [[ -f "$REPORT" ]]; then
    PRIOR_HASH="$("$PY" -c '
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
' "$REPORT" 2>/dev/null)"
fi

PROMPT="$(cat "$ROOT/prompts/premarket.md")

Today is $DATE. The session already analysed and reported on is $SESSION --
data/premarket/signals.json is measured from that date, and there is no newer
bar because the US market has not opened yet.

Read data/premarket/delta.txt first: it is the deterministic list of what
changed since the nightly run, and it is the spine of this report.

Write the report to reports/premarket/$DATE.md.
Run python scripts with: $PY
Start with the list-wide view: $PY scripts/brief.py --dataset data/premarket
Drill into a name with: $PY scripts/detail.py TICKER --dataset data/premarket
(--dataset matters on both: without it they read the nightly snapshot and you
would be quoting last night's filings against this morning's delta.)
$CAVEMAN_DIRECTIVE"

# Skill is on the list because the register the report is written in comes from
# the `caveman` skill, and a tool the run cannot call is a directive it cannot
# follow -- it would write a normal-prose report and say nothing about why.
ALLOWED_TOOLS="Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Skill,Bash(python3:*)"
[[ "$PY" != "python3" ]] && ALLOWED_TOOLS+=",Bash($PY:*)"

# 9>&- closes the flock descriptor for this child and everything it starts, for
# the same reason run_daily.sh does it: a single surviving grandchild would hold
# the lock forever and every later run -- nightly and pre-market alike -- would
# exit 0 as "already in progress".
run_with_timeout 1200 claude -p "$PROMPT" \
    --permission-mode acceptEdits \
    --allowedTools "$ALLOWED_TOOLS" \
    --add-dir "$ROOT" \
    9>&- \
    || echo "WARNING: claude exited $? -- checking for a report anyway"

if [[ ! -f "$REPORT" ]]; then
    fail "no pre-market report produced at reports/premarket/$DATE.md"
fi

# A byte-identical report means the analysis pass wrote nothing and this is a
# previous attempt's file, exactly as in run_daily.sh. Sending it would deliver
# the same email twice, and a full inbox with no new content is the failure this
# desk cannot see.
if [[ -n "$PRIOR_HASH" ]]; then
    now_hash="$("$PY" -c '
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
' "$REPORT" 2>/dev/null)"
    if [[ "$now_hash" == "$PRIOR_HASH" ]]; then
        fail "the analysis pass left reports/premarket/$DATE.md unchanged"
    fi
fi
echo "--- report: $REPORT ($(wc -l < "$REPORT") lines)"

# --- 5. delivery ----------------------------------------------------------
echo "--- notify"
run_with_timeout 300 "$PY" "$ROOT/scripts/notify.py" \
    --premarket "$REPORT" \
    --signals "$PM/signals.json" \
    --delta "$PM/delta.json" \
    || echo "WARNING: notify failed"

echo "=== done $(date +%Y-%m-%dT%H:%M:%S%z) ==="
