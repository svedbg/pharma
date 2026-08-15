#!/usr/bin/env python3
"""Grade the alerts the desk actually raised.

The backtest measures whether a *rule* has edge. This measures whether the
alerts you were actually sent made money -- which is the only number that
settles the question. Every alert is scored against the sector benchmark, not
just in absolute terms: a +6% bounce while XBI rose 8% is a losing signal.

    python3 scripts/score_alerts.py              # scorecard
    python3 scripts/score_alerts.py --backfill   # replay history, then score
    python3 scripts/score_alerts.py --open       # alerts still too young to grade

`--backfill` replays the stored bars through the current rules and records the
alerts they *would* have produced, tagged source='backfill' so they are never
mixed with live ones. It exists so the scorecard is not empty on day one; it is
retrospective and carries all the biases in scripts/backtest.py.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from signals import (
    BENCHMARK,
    CAPITULATION_VOL,
    SETUP_PCTB,
    SETUP_RSI,
    bollinger_pct_b,
    rsi,
)

DB = ROOT / "data" / "history.sqlite"
HORIZONS = (5, 20, 60)


def bars_for(con, ticker: str):
    return [
        (d, c, v) for d, c, v in con.execute(
            "SELECT date, adjclose, volume FROM bars WHERE ticker=? "
            "AND adjclose IS NOT NULL ORDER BY date", (ticker,)
        )
    ]


def forward_returns(series, idx: int):
    """Return {horizon: pct} for a position opened at index idx."""
    out = {}
    for h in HORIZONS:
        if idx + h < len(series):
            base, later = series[idx][1], series[idx + h][1]
            if base:
                out[h] = (later / base - 1.0) * 100.0
    return out


def backfill(con) -> int:
    """Replay history through the current rules and log the alerts they'd raise."""
    tickers = [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM bars WHERE ticker NOT IN (?,?)", (BENCHMARK, "IBB"))]
    rows, n = [], 0
    for tkr in tickers:
        series = bars_for(con, tkr)
        if len(series) < 80:
            continue
        closes = [c for _, c, _ in series]
        vols = [v for _, _, v in series]
        prev_setup = False
        for i in range(60, len(series)):
            hist = closes[: i + 1]
            r = rsi(hist)
            pctb, _, _ = bollinger_pct_b(hist)
            if r is None or pctb is None:
                continue
            is_setup = r < SETUP_RSI and pctb < SETUP_PCTB
            # Log the transition only, mirroring how live alerts are raised.
            if is_setup and not prev_setup:
                window = [v for v in vols[max(0, i - 19): i + 1] if v]
                volr = (vols[i] / (sum(window) / len(window))) if window and vols[i] else None
                rows.append((
                    series[i][0], tkr, "SETUP", "NONE", closes[i], round(r, 2), round(pctb, 3),
                    int(bool(volr and volr > CAPITULATION_VOL)), 0, None, "", "backfill",
                    "backfilled from stored history", None,
                ))
                n += 1
            prev_setup = is_setup
    cols = {r[1] for r in con.execute("PRAGMA table_info(alerts)")}
    if "context" not in cols:
        con.execute("ALTER TABLE alerts ADD COLUMN context TEXT")
    con.executemany("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return n


def summarise(vals) -> str:
    if not vals:
        return "n=0"
    return (f"n={len(vals):>4}  median {statistics.median(vals):>7.2f}%  "
            f"mean {sum(vals)/len(vals):>7.2f}%  win {100*sum(1 for v in vals if v>0)/len(vals):>5.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description="Score raised alerts")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--open", action="store_true", help="list alerts too young to score")
    args = ap.parse_args()

    if not DB.exists():
        print(f"no history at {DB} -- run fetch.py first", file=sys.stderr)
        return 1
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS alerts (
        session_date TEXT, ticker TEXT, tier TEXT, previous_tier TEXT,
        close REAL, rsi REAL, pctb REAL, capitulation INTEGER,
        vetoes INTEGER, excess_20d REAL, bucket TEXT, source TEXT, reason TEXT,
        context TEXT, PRIMARY KEY (session_date, ticker, source))""")
    if "context" not in {r[1] for r in con.execute("PRAGMA table_info(alerts)")}:
        con.execute("ALTER TABLE alerts ADD COLUMN context TEXT")

    if args.backfill:
        n = backfill(con)
        print(f"backfilled {n} historical alerts\n")

    bench = {d: c for d, c, _ in bars_for(con, BENCHMARK)}
    bench_dates = sorted(bench)

    alerts = list(con.execute(
        "SELECT session_date, ticker, tier, close, capitulation, source, context FROM alerts "
        "ORDER BY session_date"))
    if not alerts:
        print("no alerts recorded yet. Run with --backfill to seed from history,")
        print("or wait for the desk to raise live ones.")
        return 0

    scored: dict = {}
    pending = []
    factor_rows = []
    for sdate, tkr, tier, close, capit, source, ctx_json in alerts:
        series = bars_for(con, tkr)
        idx = next((i for i, (d, _, _) in enumerate(series) if d >= sdate), None)
        if idx is None:
            continue
        fwd = forward_returns(series, idx)
        if not fwd:
            pending.append((sdate, tkr, tier, close, source))
            continue
        # Benchmark-relative: a bounce smaller than the sector's is not a win.
        bidx = next((i for i, d in enumerate(bench_dates) if d >= sdate), None)
        for h, val in fwd.items():
            excess = None
            if bidx is not None and bidx + h < len(bench_dates):
                b0, b1 = bench[bench_dates[bidx]], bench[bench_dates[bidx + h]]
                if b0:
                    excess = val - (b1 / b0 - 1.0) * 100.0
            scored.setdefault((source, h), {"abs": [], "exc": [], "capit": [], "capit_exc": [],
                                            "nocapit_exc": []})
            scored[(source, h)]["abs"].append(val)
            if excess is not None:
                scored[(source, h)]["exc"].append(excess)
                # The decisive split: does volume confirmation beat the ETF, or
                # only beat zero? Absolute returns in a +23% sector prove nothing.
                (scored[(source, h)]["capit_exc"] if capit
                 else scored[(source, h)]["nocapit_exc"]).append(excess)
            if capit:
                scored[(source, h)]["capit"].append(val)
            if excess is not None and ctx_json:
                with contextlib.suppress(ValueError, TypeError):
                    factor_rows.append((json.loads(ctx_json), h, excess))

    for source in ("live", "backfill"):
        keys = [k for k in scored if k[0] == source]
        if not keys:
            continue
        total = len({a[0] + a[1] for a in alerts if a[5] == source})
        label = "LIVE ALERTS" if source == "live" else "BACKFILLED (retrospective, biased -- see docstring)"
        print(f"\n{label}   {total} alert(s) recorded")
        for h in HORIZONS:
            s = scored.get((source, h))
            if not s:
                continue
            print(f"  +{h:>2}d absolute   {summarise(s['abs'])}")
            if s["exc"]:
                print(f"  +{h:>2}d vs {BENCHMARK}     {summarise(s['exc'])}")
            if s["capit"]:
                print(f"  +{h:>2}d capitulation abs   {summarise(s['capit'])}")
            if s["capit_exc"]:
                print(f"  +{h:>2}d capitulation vs {BENCHMARK}  {summarise(s['capit_exc'])}")
            if s["nocapit_exc"]:
                print(f"  +{h:>2}d no-volume    vs {BENCHMARK}  {summarise(s['nocapit_exc'])}")

    # These splits answer questions the current history cannot: does insider
    # buying into a veto predict recovery, does regime change the odds, does the
    # conviction checklist actually rank outcomes? They stay empty until enough
    # live alerts accumulate -- which is the point of recording them now.
    print("\n\nFACTOR SPLITS (excess return vs " + BENCHMARK + ")")
    if not factor_rows:
        print("  No scored alerts carry factor data yet. Backfilled alerts predate these\n"
              "  fields, so these splits fill in only as LIVE alerts age past each horizon.\n"
              "  This is what will eventually answer: does insider buying into a veto predict\n"
              "  recovery? Does regime change the odds? Does the conviction checklist rank\n"
              "  outcomes? Those questions cannot be answered from the current history.")
    if factor_rows:
        factors = [
            ("insider cluster buy", lambda c: c.get("insider_cluster") is True,
             lambda c: c.get("insider_cluster") is False),
            ("alert carried a veto", lambda c: (c.get("conviction") or 0) < 0,
             lambda c: (c.get("conviction") or 0) >= 0),
            ("sector uptrend", lambda c: c.get("regime") == "uptrend",
             lambda c: c.get("regime") == "downtrend"),
            ("conviction strong/moderate", lambda c: c.get("conviction_label") in ("strong", "moderate"),
             lambda c: c.get("conviction_label") in ("weak", "avoid")),
            ("heavily shorted float (>=20%)", lambda c: (c.get("short_pct_float") or 0) >= 20,
             lambda c: (c.get("short_pct_float") or 0) < 20),
        ]
        for label, yes, no in factors:
            for h in HORIZONS:
                a = [e for c, hh, e in factor_rows if hh == h and yes(c)]
                b = [e for c, hh, e in factor_rows if hh == h and no(c)]
                if len(a) >= 10 and len(b) >= 10:
                    print(f"  +{h:>2}d {label:32} yes: {summarise(a)}")
                    print(f"       {'':32} no:  {summarise(b)}")
        if not any(len([e for c, hh, e in factor_rows if hh == HORIZONS[0] and f[1](c)]) >= 10
                   for f in factors):
            print("  not enough scored alerts carrying factor data yet -- "
                  "these fill in as live alerts age past each horizon")

    if pending:
        print(f"\nPENDING  {len(pending)} alert(s) too recent to score:")
        if args.open:
            for sdate, tkr, tier, close, source in pending[-20:]:
                print(f"  {sdate}  {tkr:6} {tier:6} @ ${close}  ({source})")
        else:
            print("  re-run with --open to list them")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
