#!/usr/bin/env python3
"""Measure whether the signal thresholds catch anything, and whether it pays.

Without this, the desk is unfalsifiable: it emits tiers nobody has ever scored.
This walks the stored bar history, recomputes the live indicator logic as it
would have been seen on each day (no lookahead), and reports:

  frequency  how often each rule would have fired
  edge       forward returns after a signal vs the all-days baseline

Known biases, stated so the numbers are not over-read:
  * survivorship -- the watchlist contains companies that still exist. Names
    acquired or delisted are absent, which flatters returns.
  * small sample -- ~60 names over ~15 months is not a strategy backtest. Treat
    it as a smoke test of the thresholds, not proof of edge.
  * no costs -- micro-cap spreads are wide and are not modelled here.

    python3 scripts/backtest.py
    python3 scripts/backtest.py --horizon 20 --min-obs 20
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from signals import bollinger_pct_b, percentile_rank, rsi  # noqa: E402

DB = ROOT / "data" / "history.sqlite"

# Rules to compare. Each takes the indicator dict and returns True/False.
RULES = {
    "live SETUP (RSI<30 and %B<0.05)":
        lambda d: d["rsi"] < 30 and d["pctb"] < 0.05,
    "RSI<30 only":
        lambda d: d["rsi"] < 30,
    "RSI<25 only":
        lambda d: d["rsi"] < 25,
    "%B<0.05 only":
        lambda d: d["pctb"] < 0.05,
    "RSI<35 and %B<0.15 (looser)":
        lambda d: d["rsi"] < 35 and d["pctb"] < 0.15,
    "bottom decile of 1y range":
        lambda d: d["pctile"] <= 10,
    "RSI<30 and volume>1.5x avg (capitulation)":
        lambda d: d["rsi"] < 30 and d["volr"] is not None and d["volr"] > 1.5,
    "RSI<35 and %B<0.15 and vol>1.5x":
        lambda d: d["rsi"] < 35 and d["pctb"] < 0.15 and d["volr"] is not None and d["volr"] > 1.5,
}


def load_bars(con) -> dict:
    out: dict = {}
    for tkr, d, close, vol in con.execute(
        "SELECT ticker, date, adjclose, volume FROM bars "
        "WHERE adjclose IS NOT NULL ORDER BY ticker, date"
    ):
        out.setdefault(tkr, []).append((d, close, vol))
    return out


def evaluate(series, horizons: list[int], min_history: int = 60):
    """Yield (rule_hits, forward_returns) per day, using only prior data."""
    closes = [c for _, c, _ in series]
    vols = [v for _, _, v in series]
    rows = []
    for i in range(min_history, len(closes) - max(horizons)):
        hist = closes[: i + 1]
        r = rsi(hist)
        pctb, _, _ = bollinger_pct_b(hist)
        if r is None or pctb is None:
            continue
        year = hist[-252:]
        vol_window = [v for v in vols[max(0, i - 19): i + 1] if v]
        volr = None
        if len(vol_window) >= 10 and sum(vol_window):
            volr = vols[i] / (sum(vol_window) / len(vol_window)) if vols[i] else None

        d = {
            "rsi": r,
            "pctb": pctb,
            "pctile": percentile_rank(year, hist[-1]) or 100.0,
            "volr": volr,
        }
        fwd = {h: (closes[i + h] / closes[i] - 1.0) * 100.0 for h in horizons}
        rows.append((d, fwd))
    return rows


def summarise(vals: list[float]) -> str:
    if not vals:
        return "n=0"
    med = statistics.median(vals)
    mean = sum(vals) / len(vals)
    win = 100.0 * sum(1 for v in vals if v > 0) / len(vals)
    return f"n={len(vals):>5}  median {med:>7.2f}%  mean {mean:>7.2f}%  win {win:>5.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the signal thresholds")
    ap.add_argument("--horizons", default="5,20,60")
    ap.add_argument("--min-obs", type=int, default=15)
    args = ap.parse_args()
    horizons = [int(h) for h in args.horizons.split(",")]

    if not DB.exists():
        print(f"no history at {DB} -- run fetch.py first", file=sys.stderr)
        return 1
    con = sqlite3.connect(DB)
    bars = load_bars(con)

    all_rows = []
    for tkr, series in bars.items():
        if len(series) < 60 + max(horizons) + 5:
            continue
        all_rows.extend(evaluate(series, horizons))

    if not all_rows:
        print("not enough history to backtest", file=sys.stderr)
        return 1

    print(f"universe: {len(bars)} tickers | {len(all_rows):,} ticker-days evaluated")
    print(f"horizons: {horizons} sessions\n")

    baseline = {h: [f[h] for _, f in all_rows] for h in horizons}
    print("BASELINE (every day, every name)")
    for h in horizons:
        print(f"  +{h:>2}d  {summarise(baseline[h])}")
    print()

    print("RULES")
    for name, fn in RULES.items():
        hits = [f for d, f in all_rows if fn(d)]
        rate = 100.0 * len(hits) / len(all_rows)
        print(f"\n  {name}")
        print(f"    fires on {len(hits):,} of {len(all_rows):,} ticker-days ({rate:.2f}%)")
        if len(hits) < args.min_obs:
            print(f"    too few observations to score (<{args.min_obs})")
            continue
        for h in horizons:
            vals = [f[h] for f in hits]
            base = baseline[h]
            edge = statistics.median(vals) - statistics.median(base)
            print(f"    +{h:>2}d  {summarise(vals)}  | median edge vs baseline {edge:+.2f}pp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
