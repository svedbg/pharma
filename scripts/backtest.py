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
    python3 scripts/backtest.py --horizons 20 --min-obs 20
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from propose_zones import zone_from_closes
from signals import (
    BENCHMARK,
    CAPITULATION_VOL,
    NOT_CANDIDATES,
    RSI_TRAP,
    SETUP_PCTB,
    SETUP_RSI,
    ZONE_STALE_DRIFT_PCT,
    bollinger_pct_b,
    percentile_rank,
    rsi,
)

DB = ROOT / "data" / "history.sqlite"

# Rules to compare. Each takes the indicator dict and returns True/False.
#
# The live rule is built from the constants signals.py actually uses, never from
# numbers retyped here. This file used to label RSI<30 & %B<0.05 as "live SETUP"
# and the real rule as "(looser)" -- so the instrument that justifies the
# thresholds was silently scoring a threshold the desk had already abandoned.
# The superseded rule is kept as a named comparison, which is what it is.
RULES = {
    f"live SETUP (RSI<{SETUP_RSI:g} and %B<{SETUP_PCTB:g})":
        lambda d: d["rsi"] < SETUP_RSI and d["pctb"] < SETUP_PCTB,
    f"live SETUP + volume>{CAPITULATION_VOL:g}x (the ACT bar)":
        lambda d: (d["rsi"] < SETUP_RSI and d["pctb"] < SETUP_PCTB
                   and d["volr"] is not None and d["volr"] > CAPITULATION_VOL),
    "superseded SETUP (RSI<30 and %B<0.05)":
        lambda d: d["rsi"] < 30 and d["pctb"] < 0.05,
    "RSI<30 only":
        lambda d: d["rsi"] < 30,
    # Kept at the documented 25 so the table in CLAUDE.md stays reproducible,
    # alongside the threshold the reasons actually warn at.
    "RSI<25 only":
        lambda d: d["rsi"] < 25,
    f"RSI<{RSI_TRAP:g} only (the distress bucket)":
        lambda d: d["rsi"] < RSI_TRAP,
    "%B<0.05 only":
        lambda d: d["pctb"] < 0.05,
    "bottom decile of 1y range":
        lambda d: d["pctile"] <= 10,
    f"RSI<30 and volume>{CAPITULATION_VOL:g}x avg (capitulation)":
        lambda d: d["rsi"] < 30 and d["volr"] is not None and d["volr"] > CAPITULATION_VOL,
    # Does a zone that has gone stale to the DOWNSIDE still deserve to open ACT?
    #
    # `zone_stale` fires at ZONE_STALE_DRIFT_PCT in either direction, but only
    # gates the upward one: above the zone `in_zone` is false and ACT cannot
    # fire, while below it `in_zone` is true and ACT fires normally. Below is
    # the direction this desk hunts, so the question is whether that permission
    # is earned. Split the live ACT bar by it and let the numbers answer.
    #
    # The zone is reconstructed as of each day from prior bars only, through the
    # same zone_from_closes() propose_zones.py uses, so this scores the rule the
    # desk runs rather than a paraphrase of it.
    "ACT bar, in zone, zone fresh":
        lambda d: _act_bar(d) and d["in_zone"] and not d["zone_stale_down"],
    f"ACT bar, in zone, zone stale by {ZONE_STALE_DRIFT_PCT:g}%+ to the downside":
        lambda d: _act_bar(d) and d["in_zone"] and d["zone_stale_down"],
}


def _act_bar(d: dict) -> bool:
    """The technical half of ACT: oversold, confirmed by capitulation volume."""
    return bool(d["rsi"] < SETUP_RSI and d["pctb"] < SETUP_PCTB
                and d["volr"] is not None and d["volr"] > CAPITULATION_VOL)


def load_bars(con) -> dict:
    out: dict = {}
    for tkr, d, close, vol in con.execute(
        "SELECT ticker, date, adjclose, volume FROM bars "
        "WHERE adjclose IS NOT NULL ORDER BY ticker, date"
    ):
        out.setdefault(tkr, []).append((d, close, vol))
    return out


def evaluate(series, horizons: list[int], bench: dict | None = None,
             min_history: int = 60):
    """Yield (indicators, forward_returns, excess_returns) per day, prior data only.

    `bench` maps date -> close for the benchmark. Excess return is measured
    between the ticker's OWN bar dates, not between two index offsets: a halted
    session in the name but not the ETF shifts the comparison by a day
    otherwise, which is the same misalignment relative_strength() in signals.py
    already exists to avoid.
    """
    closes = [c for _, c, _ in series]
    vols = [v for _, _, v in series]
    dates = [d for d, _, _ in series]
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

        # The zone as it would have been proposed that evening, from the same
        # function propose_zones.py calls. None when the window is too short --
        # which is also when the real desk writes no zone and ACT stays off.
        zone = zone_from_closes(hist)
        zone_hi = (zone or {}).get("entry_high") or 0
        drift = ((hist[-1] / zone_hi - 1.0) * 100.0) if zone_hi else None

        d = {
            "rsi": r,
            "pctb": pctb,
            "pctile": percentile_rank(year, hist[-1]) or 100.0,
            "volr": volr,
            "in_zone": bool(zone_hi and hist[-1] <= zone_hi),
            # Stale specifically downward: the case where ACT still fires.
            "zone_stale_down": bool(drift is not None
                                    and drift <= -ZONE_STALE_DRIFT_PCT),
        }
        fwd = {h: (closes[i + h] / closes[i] - 1.0) * 100.0 for h in horizons}
        exc = {}
        if bench:
            for h in horizons:
                b0, b1 = bench.get(dates[i]), bench.get(dates[i + h])
                if b0 and b1:
                    exc[h] = fwd[h] - (b1 / b0 - 1.0) * 100.0
        rows.append((d, fwd, exc))
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
    bench = {d: c for d, c, _ in bars.get(BENCHMARK, [])}
    if not bench:
        print(f"no {BENCHMARK} bars -- excess returns unavailable", file=sys.stderr)

    all_rows = []
    for tkr, series in bars.items():
        if tkr in NOT_CANDIDATES:
            continue
        if len(series) < 60 + max(horizons) + 5:
            continue
        all_rows.extend(evaluate(series, horizons, bench))

    if not all_rows:
        print("not enough history to backtest", file=sys.stderr)
        return 1

    candidates = [t for t in bars if t not in NOT_CANDIDATES]
    print(f"universe: {len(candidates)} tickers | {len(all_rows):,} ticker-days evaluated")
    print(f"horizons: {horizons} sessions")
    print(f"excess is vs {BENCHMARK} over the same calendar dates\n")

    baseline = {h: [f[h] for _, f, _ in all_rows] for h in horizons}
    base_exc = {h: [e[h] for _, _, e in all_rows if h in e] for h in horizons}
    print("BASELINE (every day, every name)")
    for h in horizons:
        print(f"  +{h:>2}d  {summarise(baseline[h])}")
        if base_exc[h]:
            print(f"        vs {BENCHMARK}: {summarise(base_exc[h])}")
    print()

    print("RULES")
    for name, fn in RULES.items():
        hits = [(f, e) for d, f, e in all_rows if fn(d)]
        rate = 100.0 * len(hits) / len(all_rows)
        print(f"\n  {name}")
        print(f"    fires on {len(hits):,} of {len(all_rows):,} ticker-days ({rate:.2f}%)")
        if len(hits) < args.min_obs:
            print(f"    too few observations to score (<{args.min_obs})")
            continue
        for h in horizons:
            vals = [f[h] for f, _ in hits]
            edge = statistics.median(vals) - statistics.median(baseline[h])
            print(f"    +{h:>2}d  {summarise(vals)}  | median edge vs baseline {edge:+.2f}pp")
            # The baseline is the average ticker-day in this universe, which is
            # not something anyone can buy. The ETF is. Where the two disagree,
            # the ETF is the one that settles it.
            ex = [e[h] for _, e in hits if h in e]
            if ex:
                print(f"          vs {BENCHMARK}: {summarise(ex)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
