#!/usr/bin/env bash
#
# Shared prologue for every entry point that runs the desk's pipeline:
# run_daily.sh (the nightly report) and run_premarket.sh (the pre-market news
# pass). SOURCED, never executed.
#
# It exists because these two scripts must agree about the hardest parts of
# running unattended, and the hardest parts are the ones nobody looks at again:
# the lock, the interpreter probe, the bounded-stage helper and the network
# wait. Every one of them encodes a failure that already happened here once --
# a recycled pid making a lock look held forever, a bare `flock` exiting 127 on
# macOS and being read as "already running", an interpreter missing pyexpat
# silently deleting the insider layer from every report. Two copies of that
# would drift, and the copy that drifts is the one that is not the nightly run,
# so the drift would show up as the pre-market pass quietly doing nothing.
#
# This is the same argument CLAUDE.md makes for NOT_CANDIDATES living in one
# place: a list written three times gets updated once.
#
# The caller must set, BEFORE sourcing:
#   ROOT       the project directory
#   DATE       the run date (names the log)
#   RUN_LABEL  what to call this run in a failure notification
#   RUN_KIND   `daily` or `premarket`. Which delivery record a failure notice
#              writes. RUN_LABEL is prose for a human; this is the key the
#              heartbeat reads back, and the two must not be inferred from one
#              another -- a failure notice that wrote the wrong record made a
#              dead nightly run read as healthy, because the morning's failure
#              had delivered successfully into the nightly run's slot.
#
# It defines busy/notify_failure/fail/run_with_timeout/capture_if_ok, and then
# ACQUIRES THE LOCK, picks $PY and waits for the network -- in that order, as
# executed code. Sourcing it is entering the run, not preparing to. It also
# exports $CAVEMAN_DIRECTIVE, the register both analysis prompts are written
# in, for the same one-definition reason as the rest.

if [[ -z "${ROOT:-}" || -z "${DATE:-}" || -z "${RUN_LABEL:-}" || -z "${RUN_KIND:-}" ]]; then
    echo "FATAL: run_preamble.sh sourced without ROOT, DATE, RUN_LABEL and RUN_KIND set" >&2
    exit 1
fi
if [[ "$RUN_KIND" != "daily" && "$RUN_KIND" != "premarket" ]]; then
    echo "FATAL: RUN_KIND is '$RUN_KIND'; expected daily or premarket" >&2
    exit 1
fi
# A guard, not decoration: sourced with `set -e` absent, a missing ROOT would
# otherwise produce a lock at /data/.run.lock and an $PY probe against nothing.

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
        "$c" "$ROOT/scripts/notify.py" --failure "$1" --run "$RUN_KIND" && return 0
    done
    echo "WARNING: could not send the failure notification" >&2
    return 1
}

fail() {
    echo "FAILED: $1"
    notify_failure "$RUN_LABEL failed on $DATE: $1" || true
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

# ---------------------------------------------------------------------------
# The register both reports are written in.
#
# Both scheduled passes end in an unattended `claude -p`, and both produce a
# document whose whole design constraint is that it gets read on a phone in a
# hurry -- prompts/daily.md budgets 250 lines for that reason and
# prompts/premarket.md 80. The `caveman` skill compresses prose by ~65% with
# every number left exact, which is the same goal those budgets encode, so it
# is appended to both prompts from here rather than pasted into each script:
# one definition, because the copy that drifts is the one that is not the
# nightly run.
#
# The override has to be explicit. The skill's own boundary is that anything
# persisted outside the chat -- files, commits, docs -- stays normal prose, and
# the report is exactly that, so a run told only "use caveman" would correctly
# compress its own chatter and leave the one artefact this is for untouched.
#
# What must survive compression is spelled out at length because a shorter
# report is worthless if what it actually became is a *thinner* one. Every
# number, every links_md line, every veto and every required section stays.
# Fewer words for the same findings; never fewer findings.
#
# If the skill is not installed, the run degrades to a normal-prose report,
# which is the behaviour that shipped before this and is safe.
CAVEMAN_DIRECTIVE="$(cat <<'CAVEMAN'

## Register -- write this report compressed

Invoke the `caveman` skill at intensity `full` before you begin, and write the
report itself in that register, not only your own chat output. This overrides
the skill's own rule that text persisted outside the chat stays normal prose:
this report IS the deliverable, it is read on a phone, and it has been asked
for compressed. `full`, not `ultra` -- this is a document where a dropped
conjunction can invert a recommendation.

Compression buys fewer words for the same findings. It never buys fewer
findings. Every section the output spec above asks for still appears, in the
same order, under the same headings, and the line budget still applies on top.

Left exact, and in whole sentences where compression would risk a misread:

- Every number, price, percentage, ratio, share count, date and unit, and every
  ticker. Nothing here is rounded, abbreviated or dropped to save a word.
- The `links_md` line under every name you discuss, verbatim from signals.json.
- Every hard veto, and any refutation of one, stated so a reader cannot
  mistake which way it went. A veto is a warning, and warnings do not become
  fragments.
- Anything that instructs: position size, an exit horizon, the catalyst sizing
  line inside 21 days.
- Fenced code blocks. The thesis and catalyst blocks are pasted into
  hand-edited files and read out of them months later, so their contents are
  normal prose.
- The closing line that this is research for the user's own decisions and not
  financial advice.
- Tables keep their headers and columns. The signal table is required output,
  not decoration.

Never invent an abbreviation to save characters, and never drop a "not", "no",
"only" or "never" -- an inverted meaning costs more than every token it saves.
CAVEMAN
)"
