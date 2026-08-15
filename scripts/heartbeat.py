#!/usr/bin/env python3
"""Alert when the desk stops producing reports.

This exists because silence is the desk's *normal* output. A week with nothing
to buy and a week where the timer never fired look identical from the outside,
so a broken run can go unnoticed indefinitely.

It deliberately runs from its own systemd timer and shares no code path with
run_daily.sh beyond notify.py. If the main run dies early -- a bad PATH, a
Python failure before notify.py is reachable -- run_daily.sh's own failure
handler dies with it. This does not.

    python3 scripts/heartbeat.py            # check, alert if stale
    python3 scripts/heartbeat.py --status   # print state, never notify
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

REPORTS = ROOT / "reports"
# Two missed weekdays: one is a holiday or a slow night, two is a fault.
MAX_WEEKDAYS_STALE = 2


def weekdays_between(start: date, end: date) -> int:
    days, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def latest_report() -> tuple[date | None, Path | None]:
    if not REPORTS.exists():
        return None, None
    best, best_path = None, None
    for f in REPORTS.glob("*.md"):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.md$", f.name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if best is None or d > best:
            best, best_path = d, f
    return best, best_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Alert if the desk has gone quiet")
    ap.add_argument("--status", action="store_true", help="print state without notifying")
    ap.add_argument("--max-stale", type=int, default=MAX_WEEKDAYS_STALE)
    args = ap.parse_args()

    today = date.today()
    last, path = latest_report()

    if last is None:
        msg = "No report has ever been written. The desk has never completed a run."
        stale = 99
    else:
        stale = weekdays_between(last, today)
        msg = (f"No report since {last} ({stale} weekday(s) ago). "
               f"Expected one every weekday evening -- check "
               f"'journalctl --user -u pharma-desk.service -n 50' (Linux) "
               f"or ~/Library/Logs/pharma-desk.log (macOS).")

    healthy = last is not None and stale <= args.max_stale
    print(f"latest report: {last} ({path.name if path else 'none'})")
    print(f"weekdays since: {stale}  threshold: {args.max_stale}  "
          f"status: {'OK' if healthy else 'STALE'}")

    if args.status or healthy:
        return 0

    # Reuse the desk's own delivery path so a stale alert reaches the same phone.
    try:
        from notify import load_config, send_email, send_ntfy
        cfg = load_config()
        if not cfg:
            print("no notify config; cannot raise the alarm", file=sys.stderr)
            return 1
        ok_push = send_ntfy(cfg, "Biotech desk has gone quiet", msg,
                            priority="high", tags="warning")
        ok_mail = send_email(cfg, "[biotech desk] no reports - run may be broken", msg)
        print(f"alerted: ntfy={ok_push} email={ok_mail}", file=sys.stderr)
    except Exception as e:
        print(f"heartbeat could not notify: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
