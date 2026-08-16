#!/usr/bin/env python3
"""Alert when the desk stops producing reports.

This exists because silence is the desk's *normal* output. A week with nothing
to buy and a week where the timer never fired look identical from the outside,
so a broken run can go unnoticed indefinitely.

It deliberately runs from its own timer -- systemd on Linux, launchd on macOS --
and shares no code path with run_daily.sh beyond notify.py. If the main run
dies early -- a bad PATH, a Python failure before notify.py is reachable --
run_daily.sh's own failure handler dies with it. This does not.

    python3 scripts/heartbeat.py            # check, alert if stale
    python3 scripts/heartbeat.py --status   # print state, never notify
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

REPORTS = ROOT / "reports"
DELIVERY_LOG = ROOT / "data" / "last_delivery.json"
# Two missed weekdays of slack -- one is a holiday or a slow night, two is a
# fault -- plus one for the gap between a session and the day it is checked on.
#
# Reports are named for the *session* they analyse, not the day the run fired,
# and those differ whenever the newest bar is not today's. The scheduled run is
# 18 minutes after the US close, so losing that race to the provider dates the
# report a day back. At a threshold of 2 a persistent one-day lag sat exactly on
# the limit: still passing, but with none of the holiday slack this number
# exists to provide, so the first real miss would look like the second.
MAX_WEEKDAYS_STALE = 3


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


def delivery_fault() -> str | None:
    """Why the last run's notifications did not arrive, or None if they did.

    A report on disk is not evidence that anything was sent. Watching only
    reports meant a wrong SMTP password read as a completely healthy desk: the
    run wrote its file, this check passed, and the phone stayed silent for as
    long as nobody thought to wonder. notify.py now records what each channel
    did, and that record is the only place the answer exists.

    A missing file is not a fault. It means notify.py has not run since this was
    added, and inventing an alarm out of that would fire once on every install.
    """
    if not DELIVERY_LOG.exists():
        return None
    try:
        rec = json.loads(DELIVERY_LOG.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return f"delivery record at {DELIVERY_LOG} is unreadable ({e})"
    failed = [name for name, ok in (rec.get("channels") or {}).items() if ok is False]
    if failed:
        return (f"the last run ({rec.get('session_date') or 'undated'}, sent "
                f"{rec.get('at')}) could not deliver via {', '.join(sorted(failed))} "
                f"-- the report was written but never reached you.")
    # Sending nothing is not a fault: a quiet day with EMAIL_ALWAYS=0 correctly
    # sends nothing at all. Having nowhere to send is.
    if not any((rec.get("configured") or {}).values()):
        return (f"the last run ({rec.get('session_date') or 'undated'}) had no delivery "
                f"channel configured -- set NTFY_TOPIC, or SMTP_HOST and EMAIL_TO, in "
                f"~/.config/pharma/pharma.env")
    return None


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
        # Name the log this machine actually has. Listing both taught the
        # reader to skip the line to find their half of it.
        where = ("~/Library/Logs/pharma-desk.log" if sys.platform == "darwin"
                 else "'journalctl --user -u pharma-desk.service -n 50'")
        msg = (f"No report since {last} ({stale} weekday(s) ago). "
               f"Expected one every weekday evening -- check {where}.")

    reports_ok = last is not None and stale <= args.max_stale
    fault = delivery_fault()
    healthy = reports_ok and fault is None
    # Reports arriving but not being delivered is its own failure, and it needs
    # its own message: "no report since ..." would be a lie about a run that
    # completed fine.
    if reports_ok and fault:
        msg = f"Reports are being written (latest {last}), but {fault}"

    print(f"latest report: {last} ({path.name if path else 'none'})")
    print(f"weekdays since: {stale}  threshold: {args.max_stale}  "
          f"status: {'OK' if reports_ok else 'STALE'}")
    print(f"delivery: {'OK' if fault is None else fault}")

    if args.status or healthy:
        return 0

    # Reuse the desk's own delivery path so a stale alert reaches the same phone.
    try:
        from notify import load_config, send_email, send_ntfy
        cfg = load_config()
        if not cfg:
            print("no notify config; cannot raise the alarm", file=sys.stderr)
            return 1
        title, subject = (
            ("Biotech desk cannot deliver", "[biotech desk] reports are not reaching you")
            if reports_ok else
            ("Biotech desk has gone quiet", "[biotech desk] no reports - run may be broken")
        )
        # Sent through the very channels being reported on, which is why this
        # also exits non-zero: when the fault is delivery itself the alarm may
        # not arrive either, and the scheduler's journal is then the only record.
        ok_push = send_ntfy(cfg, title, msg, priority="high", tags="warning")
        ok_mail = send_email(cfg, subject, msg)
        print(f"alerted: ntfy={ok_push} email={ok_mail}", file=sys.stderr)
    except Exception as e:
        print(f"heartbeat could not notify: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
