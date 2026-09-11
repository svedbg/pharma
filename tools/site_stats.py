#!/usr/bin/env python3
"""Write www/stats.toml — the desk's operating record, computed, not typed.

    python3 tools/site_stats.py            # writes www/stats.toml
    python3 tools/site_stats.py --print    # show, write nothing

Counts nightly reports in reports/ (YYYY-MM-DD.md, the same rule heartbeat.py
uses), the weeknights between the first and the latest, and test functions
under tests/. tools/build_site.py reads the result into the "operating record"
band on the landing page; when the file is absent the band is left out.

Missed weeknights include US market holidays — the timers are weekday-only and
do not know the exchange calendar — so the number is conservative. Say so on
the page rather than adjusting it. Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
TESTS = ROOT / "tests"
OUT = ROOT / "www" / "stats.toml"
DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def report_dates() -> list[dt.date]:
    if not REPORTS.exists():
        return []
    out = []
    for f in REPORTS.glob("*.md"):
        m = DATED.match(f.name)
        if not m:
            continue
        try:
            out.append(dt.date.fromisoformat(m.group(1)))
        except ValueError:
            continue
    return sorted(set(out))


def weekdays_inclusive(a: dt.date, b: dt.date) -> int:
    n, cur = 0, a
    while cur <= b:
        if cur.weekday() < 5:
            n += 1
        cur += dt.timedelta(days=1)
    return n


def count_tests() -> int:
    n = 0
    for f in TESTS.glob("test_*.py"):
        n += len(re.findall(r"^\s*(?:async\s+)?def test_\w+", f.read_text(encoding="utf-8"), re.M))
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="print the TOML, write nothing")
    a = ap.parse_args()

    dates = report_dates()
    tests = count_tests()
    if not dates:
        body = (f"# no dated reports found under reports/ — run the desk first\n"
                f"tests = {tests}\ngenerated = \"{dt.date.today().isoformat()}\"\n")
    else:
        first, last = dates[0], dates[-1]
        expected = weekdays_inclusive(first, last)
        weeknight_reports = sum(1 for d in dates if d.weekday() < 5)
        missed = max(expected - weeknight_reports, 0)
        body = (
            "# Operating record, computed by tools/site_stats.py. Re-run before a release; do not hand-edit.\n"
            f"since = \"{first.isoformat()}\"\n"
            f"latest = \"{last.isoformat()}\"\n"
            f"nights = {len(dates)}\n"
            f"weeknights_expected = {expected}\n"
            f"weeknights_missed = {missed}   # includes US market holidays; see the note on the page\n"
            f"tests = {tests}\n"
            f"generated = \"{dt.date.today().isoformat()}\"\n"
        )

    if a.print:
        print(body, end="")
        return 0
    OUT.write_text(body, encoding="utf-8")
    print(f"wrote www/stats.toml: {len(dates)} nightly reports, {tests} tests")
    if dates:
        print(f"  {dates[0]} → {dates[-1]}, {body.split('weeknights_missed = ')[1].split()[0]} weeknights without a report")
    print("then: make www")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
