#!/usr/bin/env python3
"""Is it still pre-market? The one property the morning pass cannot lose.

`CLAUDE.md` is emphatic that the 14:30 Europe/Sofia pass must land before the
09:30 ET open, and explains why a late one is worse than no pass at all: it
emails a note headed "pre-market" about a session already hours into trading,
and it reads as current. `Persistent=false` on the timer encodes that -- a
missed pass is dropped rather than caught up.

But that only covers the scheduled path. Nothing stopped a *hand* run at any
hour, and the hand run is not the rare case it sounds like: a machine that is
off at 14:30 gets no pass at all (correctly, since the timer does not catch up),
so the natural response is to run it by hand whenever the machine comes up. That
happened on 2026-08-31 -- the machine was off 23:35 to 15:19, systemd recorded
no premarket service run, and the pass went out from a hand run at 15:42 Sofia,
which was 08:42 ET and inside the window by 48 minutes of luck. The same habit
on a day the machine boots an hour later delivers a "pre-market" note after the
bell.

So the schedule is no longer the only thing enforcing this. The rule lives here,
in Python, for the reason `premarket_delta.py` gives for the urgency decision:
it is arithmetic, and arithmetic belongs somewhere it can be tested rather than
in a shell script nothing exercises.

The cutoff is 09:00 ET, half an hour before the open. Both scheduled alignments
clear it with room -- 14:30 Sofia is 07:30 ET normally and 08:30 ET during the
fortnight each spring and autumn when EU and US DST are out of step -- and the
pass itself runs in about ten minutes.

Exit status is the interface: 0 means go ahead, 1 means too late.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MARKET_TZ = "America/New_York"
DEFAULT_CUTOFF = time(9, 0)   # 09:00 ET, 30 minutes before the 09:30 open
MARKET_OPEN = time(9, 30)


def parse_cutoff(raw: str) -> time:
    """HH:MM, so the bar can be moved without editing this file."""
    hh, _, mm = raw.partition(":")
    return time(int(hh), int(mm))


def is_premarket(now_et: datetime, cutoff: time = DEFAULT_CUTOFF) -> bool:
    """True while there is still time to read a filing and act before the open."""
    return now_et.timetz().replace(tzinfo=None) < cutoff


def describe(now_et: datetime, cutoff: time = DEFAULT_CUTOFF) -> str:
    hhmm = now_et.strftime("%H:%M")
    if is_premarket(now_et, cutoff):
        return (f"{hhmm} ET -- pre-market, {_minutes_to(now_et, MARKET_OPEN)} "
                f"minutes before the open")
    if now_et.timetz().replace(tzinfo=None) < MARKET_OPEN:
        return (f"{hhmm} ET -- past the {cutoff.strftime('%H:%M')} cutoff, and "
                f"only {_minutes_to(now_et, MARKET_OPEN)} minutes before the open")
    return f"{hhmm} ET -- the market is already open (09:30 ET)"


def _minutes_to(now_et: datetime, mark: time) -> int:
    now = now_et.hour * 60 + now_et.minute
    return mark.hour * 60 + mark.minute - now


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--asof", help="ISO 8601 instant to judge instead of now; "
                                   "naive values are read as ET")
    ap.add_argument("--cutoff", default="09:00",
                    help="HH:MM ET, the latest acceptable start (default 09:00)")
    args = ap.parse_args(argv)

    try:
        et = ZoneInfo(MARKET_TZ)
    except ZoneInfoNotFoundError:
        # Fail OPEN, loudly. Refusing here would stop the morning pass on every
        # machine with an incomplete tzdata, and this desk's normal output is
        # silence -- so that failure would be invisible, which is the one thing
        # worse than the late note this guard exists to prevent. A warning on
        # stderr reaches the log and the run continues.
        print("[premarket-window] WARNING: no tzdata for "
              f"{MARKET_TZ}; cannot check the clock, proceeding anyway",
              file=sys.stderr)
        return 0

    if args.asof:
        now = datetime.fromisoformat(args.asof)
        now_et = now.astimezone(et) if now.tzinfo else now.replace(tzinfo=et)
    else:
        now_et = datetime.now(et)

    cutoff = parse_cutoff(args.cutoff)
    print(f"[premarket-window] {describe(now_et, cutoff)}")
    return 0 if is_premarket(now_et, cutoff) else 1


if __name__ == "__main__":
    raise SystemExit(main())
