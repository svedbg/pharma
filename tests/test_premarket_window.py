"""The pre-market pass must actually be pre-market.

`CLAUDE.md` calls this "the one property this unit cannot lose", and explains
why a late pass is worse than none: it emails a note headed "pre-market" about a
session already trading, and it reads as current. Until this existed the rule
was enforced *only* by the timer's schedule -- which says nothing about a hand
run, and a hand run is not exotic. `Persistent=false` means a machine that is
off at 14:30 gets no pass, so the natural response is to run it by hand when the
machine comes up. That is exactly what happened on 2026-08-31: the machine was
off from 23:35 to 15:19, systemd recorded no premarket service run, and the pass
went out from a hand run at 15:42 Sofia. That was 08:42 ET -- inside the window
by 48 minutes, entirely by luck.

So these tests pin the arithmetic, and `test_the_premarket_run_consults_the_
window_guard` pins that the shell script actually asks.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import premarket_window as win
from signals import ROOT

ET = ZoneInfo("America/New_York")


def _et(hh: int, mm: int) -> datetime:
    return datetime(2026, 8, 31, hh, mm, tzinfo=ET)


def test_the_scheduled_alignments_both_clear_the_cutoff():
    """14:30 Sofia is 07:30 ET normally and 08:30 ET for the fortnight each
    spring and autumn when EU and US DST are out of step. A cutoff that blocked
    either would silently kill the pass for two weeks at a time -- and silence
    is this desk's normal output, so nobody would notice."""
    assert win.is_premarket(_et(7, 30)), "the normal 07:30 ET alignment must run"
    assert win.is_premarket(_et(8, 30)), "the DST-mismatch 08:30 ET alignment must run"


def test_the_hand_run_that_prompted_this_still_passes():
    """08:42 ET on 2026-08-31 was legitimate -- late, but genuinely pre-market.
    A guard that failed it would be punishing the wrong thing."""
    assert win.is_premarket(_et(8, 42))


def test_it_refuses_at_and_after_the_cutoff():
    """Inclusive at the boundary: 09:00 leaves 30 minutes, which is not enough
    to read a filing and act, and the whole point of the pass is that there is
    time to decide."""
    assert not win.is_premarket(_et(9, 0)), "the cutoff itself must not pass"
    assert not win.is_premarket(_et(9, 15))


def test_it_refuses_after_the_open_which_is_the_failure_it_exists_for():
    """The 17:00 catch-up CLAUDE.md describes, and the 09:45 near-miss that the
    old 15:45 Sofia schedule would have produced."""
    assert not win.is_premarket(_et(9, 45))
    assert not win.is_premarket(_et(17, 0))


def test_the_cutoff_sits_before_the_open_with_room():
    """If these ever cross, the guard would be permitting runs that deliver
    after the bell while reporting itself as pre-market."""
    assert win.DEFAULT_CUTOFF < win.MARKET_OPEN
    gap = ((win.MARKET_OPEN.hour * 60 + win.MARKET_OPEN.minute)
           - (win.DEFAULT_CUTOFF.hour * 60 + win.DEFAULT_CUTOFF.minute))
    assert gap >= 30, f"only {gap} minutes between the cutoff and the open"


def test_exit_status_is_the_interface_the_shell_reads():
    """run_premarket.sh branches on the exit code and nothing else."""
    assert win.main(["--asof", "2026-08-31T07:30"]) == 0
    assert win.main(["--asof", "2026-08-31T09:45"]) == 1


def test_an_aware_timestamp_is_converted_rather_than_reinterpreted():
    """15:42+03:00 -- the Sofia wall clock of the run that prompted this -- is
    08:42 ET and must be judged as such. Reading the local hour as if it were
    ET would refuse a run that was correctly inside the window."""
    assert win.main(["--asof", "2026-08-31T15:42:20+03:00"]) == 0
    # and the same wall clock an hour later is not
    assert win.main(["--asof", "2026-08-31T16:42:20+03:00"]) == 1


def test_a_custom_cutoff_is_honoured():
    assert win.is_premarket(_et(9, 15), cutoff=time(9, 30))
    assert not win.is_premarket(_et(9, 15), cutoff=time(9, 0))


def test_the_premarket_run_consults_the_window_guard():
    """The arithmetic being right is worth nothing if the script never asks.

    Pinned by text because the alternative is executing a run that fetches ~60
    names. Three things have to hold together: the call exists, it is skipped
    for runs that cannot reach the mailbox, and it exits 0 rather than failing
    -- a non-zero exit would fire the failure notice for a pass that was right
    to stop.
    """
    src = (ROOT / "run_premarket.sh").read_text()
    assert "scripts/premarket_window.py" in src, (
        "run_premarket.sh must consult the window guard")
    assert "NO_LLM -eq 0 && $NO_EMAIL -eq 0 && $FORCE_LATE -eq 0" in src, (
        "the guard must be skipped when the run cannot send anything, and "
        "overridable with --force-late")
    guard = src.split("scripts/premarket_window.py")[1].split("fi")[0]
    assert "exit 0" in guard, (
        "refusing must exit 0 like a held lock -- not running is the correct "
        "outcome here, and a non-zero exit fires the failure notice")


def test_the_daily_run_has_no_such_guard():
    """The nightly run is an account of a session that has already closed. It
    has no deadline and must never acquire one by copy-paste."""
    assert "premarket_window" not in (ROOT / "run_daily.sh").read_text()
