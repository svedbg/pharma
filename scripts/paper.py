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
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Imported, not retyped. This module scores every trade against the benchmark,
# and a second literal here would keep grading against XBI on the day the desk
# moved to something else -- silently, since the label in the output is built
# from this name too, so the report would agree with itself and be wrong.
from signals import BENCHMARK

DB = ROOT / "data" / "history.sqlite"
# Below this, `report` prints the numbers but refuses to draw the conclusion.
# Not a statistical threshold, a humility one: at single-digit trade counts the
# median moves on any one result.
MIN_TRADES_FOR_A_VERDICT = 20

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


def iso_date(raw: str | None, label: str) -> str | None:
    """A hand-typed --date as canonical YYYY-MM-DD, or None if unusable.

    Every date in this table is compared as a string -- against bar dates, and
    against each other -- so an unpadded "2026-8-1" sorts above "2026-08-14" and
    silently picks the wrong bar or none at all. Same failure the session date
    and catalysts.toml both had; this is the third file where a date arrives by
    hand and is compared lexically.
    """
    if raw is None:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except (ValueError, TypeError):
        print(f"{label} {raw!r} is not a date. Use YYYY-MM-DD.", file=sys.stderr)
        raise SystemExit(2) from None


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

    opened = iso_date(a.date, "--date") or date.today().isoformat()
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
    closed = iso_date(a.date, "--date") or date.today().isoformat()
    # A close before the open inverts the holding period: the trade's return is
    # measured forwards from `entry` while bench_return() runs the benchmark
    # backwards over the same pair, so the excess is nonsense in a way no output
    # here would show. Easy to type on a backdated close.
    if closed < opened:
        print(f"close date {closed} is before the open date {opened}; "
              f"a trade cannot be closed before it was opened.", file=sys.stderr)
        return 1
    px = a.price
    if not px:
        _, px = price_on_or_before(c, a.ticker.upper(), closed)
    if not px:
        # Mirrors cmd_open: a missing price is a thing to say, not a TypeError
        # three lines later inside the return calculation.
        print(f"no price for {a.ticker.upper()} on or before {closed}; pass --price",
              file=sys.stderr)
        return 1
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
    for t, opened, entry, size, stop, horizon, _thesis in rows:
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
    for t, opened, entry, closed, exit_px, _horizon, note in rows:
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
        # The whole point of the exercise, stated rather than left implied --
        # but only once there is enough of a sample to mean it. The verdict used
        # to print unconditionally with "too few to conclude anything" appended
        # underneath, which is not a refusal to conclude, it is a conclusion
        # with a disclaimer: the reader has already been told the case for
        # sizing up by the time the caveat arrives. Below the threshold the
        # numbers still print; the sentence that acts on them does not.
        if len(abs_rets) < MIN_TRADES_FOR_A_VERDICT:
            print(f"  {len(abs_rets)} closed trade(s) -- too few to say anything. A verdict needs")
            print(f"  at least {MIN_TRADES_FOR_A_VERDICT}. The numbers above are the sample so far,")
            print("  not a finding, and at this size the median moves on any single trade.")
        elif med > 0:
            print(f"  Beating {BENCHMARK} by {med:+.1f}pp median over {len(abs_rets)} trades.")
            print("  That is the case for sizing up.")
        else:
            print(f"  NOT beating {BENCHMARK} ({med:+.1f}pp median over {len(abs_rets)} trades).")
            print("  On this evidence, buying the ETF would have been better than taking")
            print("  these trades.")
    elif len(abs_rets) < MIN_TRADES_FOR_A_VERDICT:
        print(f"\n  {len(abs_rets)} closed trade(s) -- too few to conclude anything.")
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
