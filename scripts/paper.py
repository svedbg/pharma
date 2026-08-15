#!/usr/bin/env python3
"""Paper-trade log: record intended trades, score them honestly.

The desk's signals are not yet validated forward. Trading them with real money
at 28% sizing is a bet on reasoning rather than evidence, so this records what
you *would* have done and grades it later -- at the cost of patience instead of
capital.

Every trade is scored **against XBI over the identical holding period**. A +9%
trade while the sector rose 12% is a losing decision, and absolute P&L would
have told you the opposite. That distinction already overturned one conclusion
on this project; it applies just as much to your own trades.

    python3 scripts/paper.py open SMMT --size 15 --entry 13.35 --stop 12.06 \
        --thesis "HARMONI-3 readout H2 2026" --horizon catalyst
    python3 scripts/paper.py close SMMT --price 15.10 --note "took profit"
    python3 scripts/paper.py status         # open positions, live P&L vs XBI
    python3 scripts/paper.py report         # closed trades, hit rate vs XBI
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "history.sqlite"
BENCHMARK = "XBI"

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    opened TEXT NOT NULL,
    entry REAL NOT NULL,
    size_pct REAL,
    stop REAL,
    horizon TEXT,          -- 'bounce' (~20 sessions) or 'catalyst'
    thesis TEXT,
    closed TEXT,
    exit_price REAL,
    exit_note TEXT
);
"""


def con():
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    return c


def price_on_or_before(c, ticker: str, day: str):
    row = c.execute(
        "SELECT date, adjclose FROM bars WHERE ticker=? AND date<=? AND adjclose IS NOT NULL "
        "ORDER BY date DESC LIMIT 1", (ticker, day)).fetchone()
    return row if row else (None, None)


def latest_price(c, ticker: str):
    row = c.execute(
        "SELECT date, adjclose FROM bars WHERE ticker=? AND adjclose IS NOT NULL "
        "ORDER BY date DESC LIMIT 1", (ticker,)).fetchone()
    return row if row else (None, None)


def bench_return(c, start: str, end: str):
    _, a = price_on_or_before(c, BENCHMARK, start)
    _, b = price_on_or_before(c, BENCHMARK, end)
    if not a or not b:
        return None
    return (b / a - 1.0) * 100.0


def cmd_open(c, a) -> int:
    ticker = a.ticker.upper()
    # Check for a duplicate before touching prices, so the error is the first
    # thing printed rather than trailing a misleading "using last close" line.
    dup = c.execute("SELECT id FROM paper_trades WHERE ticker=? AND closed IS NULL",
                    (ticker,)).fetchone()
    if dup:
        print(f"{ticker} already has an open paper position (id {dup[0]}). "
              f"Close it first.", file=sys.stderr)
        return 1

    opened = a.date or date.today().isoformat()
    if not a.entry:
        # A backdated entry must use the price on THAT day. Defaulting to the
        # latest close silently records a trade at a price that was never
        # available on the open date, which makes every later number wrong.
        d, px = price_on_or_before(c, ticker, opened)
        if not px:
            print(f"no price for {ticker} on or before {opened}; pass --entry",
                  file=sys.stderr)
            return 1
        a.entry = px
        print(f"using close {px} from {d}")
    c.execute("INSERT INTO paper_trades (ticker,opened,entry,size_pct,stop,horizon,thesis) "
              "VALUES (?,?,?,?,?,?,?)",
              (ticker, opened, a.entry, a.size, a.stop, a.horizon, a.thesis))
    c.commit()
    print(f"opened {ticker} @ {a.entry} on {opened} "
          f"({a.size or '?'}% size, stop {a.stop or '-'}, horizon {a.horizon})")
    return 0


def cmd_close(c, a) -> int:
    row = c.execute("SELECT id, opened, entry FROM paper_trades "
                    "WHERE ticker=? AND closed IS NULL", (a.ticker.upper(),)).fetchone()
    if not row:
        print(f"no open paper position in {a.ticker}", file=sys.stderr)
        return 1
    tid, opened, entry = row
    closed = a.date or date.today().isoformat()
    px = a.price
    if not px:
        _, px = price_on_or_before(c, a.ticker.upper(), closed)
    c.execute("UPDATE paper_trades SET closed=?, exit_price=?, exit_note=? WHERE id=?",
              (closed, px, a.note, tid))
    c.commit()
    ret = (px / entry - 1.0) * 100.0
    bench = bench_return(c, opened, closed)
    excess = f"{ret - bench:+.1f}pp vs {BENCHMARK}" if bench is not None else "benchmark n/a"
    print(f"closed {a.ticker.upper()} @ {px}: {ret:+.1f}% absolute, {excess}")
    return 0


def cmd_status(c, a) -> int:
    rows = c.execute("SELECT ticker,opened,entry,size_pct,stop,horizon,thesis FROM paper_trades "
                     "WHERE closed IS NULL ORDER BY opened").fetchall()
    if not rows:
        print("no open paper positions")
        return 0
    print(f"{'Ticker':<7}{'Opened':<12}{'Entry':>9}{'Last':>9}{'Abs':>9}{'vs XBI':>10}"
          f"{'Size':>6}  Stop / horizon")
    print("-" * 84)
    for t, opened, entry, size, stop, horizon, thesis in rows:
        d, px = latest_price(c, t)
        if not px:
            print(f"{t:<7}{opened:<12}{entry:>9}{'no price':>9}")
            continue
        ret = (px / entry - 1.0) * 100.0
        bench = bench_return(c, opened, d)
        exc = f"{ret - bench:+.1f}pp" if bench is not None else "-"
        hit = " STOP HIT" if stop and px <= stop else ""
        print(f"{t:<7}{opened:<12}{entry:>9.2f}{px:>9.2f}{ret:>8.1f}%{exc:>10}"
              f"{(str(size) + '%') if size else '-':>6}  {stop or '-'} / {horizon or '-'}{hit}")
    return 0


def cmd_report(c, a) -> int:
    rows = c.execute("SELECT ticker,opened,entry,closed,exit_price,horizon,exit_note "
                     "FROM paper_trades WHERE closed IS NOT NULL ORDER BY closed").fetchall()
    if not rows:
        print("no closed paper trades yet -- this is the number that will eventually")
        print("say whether the desk beats simply owning XBI. Nothing to report until")
        print("trades have been opened and closed.")
        return 0

    abs_rets, exc_rets = [], []
    print(f"{'Ticker':<7}{'Opened':<12}{'Closed':<12}{'Abs':>9}{'vs XBI':>10}  Note")
    print("-" * 78)
    for t, opened, entry, closed, exit_px, horizon, note in rows:
        ret = (exit_px / entry - 1.0) * 100.0
        bench = bench_return(c, opened, closed)
        abs_rets.append(ret)
        exc = None
        if bench is not None:
            exc = ret - bench
            exc_rets.append(exc)
        print(f"{t:<7}{opened:<12}{closed:<12}{ret:>8.1f}%"
              f"{(f'{exc:+.1f}pp' if exc is not None else '-'):>10}  {(note or '')[:30]}")

    print()
    print(f"trades: {len(abs_rets)}")
    print(f"  absolute   median {statistics.median(abs_rets):+.1f}%  "
          f"mean {sum(abs_rets)/len(abs_rets):+.1f}%  "
          f"win {100*sum(1 for r in abs_rets if r > 0)/len(abs_rets):.0f}%")
    if exc_rets:
        med = statistics.median(exc_rets)
        print(f"  vs {BENCHMARK}     median {med:+.1f}pp  "
              f"mean {sum(exc_rets)/len(exc_rets):+.1f}pp  "
              f"win {100*sum(1 for r in exc_rets if r > 0)/len(exc_rets):.0f}%")
        print()
        # The whole point of the exercise, stated rather than left implied.
        if med > 0:
            print(f"  Beating {BENCHMARK} by {med:+.1f}pp median. That is the case for sizing up.")
        else:
            print(f"  NOT beating {BENCHMARK} ({med:+.1f}pp median). On this evidence, buying the")
            print(f"  ETF would have been better than taking these trades.")
    if len(abs_rets) < 20:
        print(f"\n  {len(abs_rets)} trades is too few to conclude anything. Treat as provisional.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Paper-trade log scored against XBI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="record an intended entry")
    o.add_argument("ticker")
    o.add_argument("--entry", type=float, help="entry price (defaults to last close)")
    o.add_argument("--size", type=float, help="intended size, %% of allocated capital")
    o.add_argument("--stop", type=float, help="invalidation price")
    o.add_argument("--horizon", default="bounce", choices=["bounce", "catalyst"])
    o.add_argument("--thesis", default="")
    o.add_argument("--date", help="ISO date, defaults to today")

    cl = sub.add_parser("close", help="record an exit")
    cl.add_argument("ticker")
    cl.add_argument("--price", type=float, help="exit price (defaults to last close)")
    cl.add_argument("--note", default="")
    cl.add_argument("--date", help="ISO date, defaults to today")

    sub.add_parser("status", help="open positions with live P&L vs XBI")
    sub.add_parser("report", help="closed trades scored against XBI")

    a = ap.parse_args()
    if not DB.exists():
        print(f"no database at {DB} -- run fetch.py first", file=sys.stderr)
        return 1
    c = con()
    try:
        return {"open": cmd_open, "close": cmd_close,
                "status": cmd_status, "report": cmd_report}[a.cmd](c, a)
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
