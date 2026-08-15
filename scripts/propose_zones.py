#!/usr/bin/env python3
"""Derive entry zones from each name's own trading range.

These are *mechanical starting points*, not valuations. The method is stated so
you can disagree with it precisely, and re-run it whenever your view changes:

  anchor window   If the name suffered a single-session collapse of 25% or worse
                  in the last 120 sessions, only bars AFTER that event are used.
                  Pre-break prices describe a company that no longer exists --
                  averaging them in would place a "buy zone" far above where the
                  stock now trades, which is exactly how you catch a falling
                  knife with arithmetic. Otherwise the trailing year is used.

  too soon        Fewer than 25 sessions in the anchor window means there is not
                  yet a range to speak of. No zone is written; ACT stays disabled
                  for that name until there is one.

  entry_high      The higher of (a) the 25th percentile of the window and (b) a
                  realistic pullback level -- the 50-day average, capped 5% below
                  spot. Percentiles alone are useless for a name in a strong
                  uptrend: they return the price before it re-rated, which it
                  will never revisit if the thesis is working.

  entry_low       A scale-in reference 22% under entry_high. It is NOT a gate --
                  a price below it is cheaper, not disqualifying. Deciding
                  whether a name is broken is the veto layer's job.

ACT requires price at or below entry_high, plus oversold technicals, no hard
veto, and an acceptable financing or catalyst backdrop.

    python3 scripts/propose_zones.py             # dry run, prints proposals
    python3 scripts/propose_zones.py --apply     # writes them into watchlist.toml
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

COLLAPSE_PCT = -25.0
COLLAPSE_LOOKBACK = 120
MIN_WINDOW_BARS = 25
YEAR_BARS = 252
HIGH_PCTILE = 25.0
LOW_PCTILE = 8.0
SCALE_IN_DROP = 0.22   # entry_low sits this far under entry_high, as a reference only


def percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def round_price(v: float) -> float:
    if v >= 100:
        return round(v, 1)
    if v >= 1:
        return round(v, 2)
    return round(v, 4)


def anchor_window(bars: list[dict]) -> tuple[list[float], str]:
    """Closes to derive the zone from, plus a note on why that window."""
    closes = [b["adjclose"] for b in bars if b.get("adjclose") is not None]
    if len(closes) < 2:
        return [], "no price history"

    start = max(1, len(closes) - COLLAPSE_LOOKBACK)
    break_idx = None
    for i in range(start, len(closes)):
        prev = closes[i - 1]
        if prev and (closes[i] / prev - 1.0) * 100.0 <= COLLAPSE_PCT:
            break_idx = i
    if break_idx is not None:
        post = closes[break_idx:]
        sessions = len(closes) - break_idx
        return post, f"post-collapse window ({sessions} sessions since the break)"
    return closes[-YEAR_BARS:], "trailing 1y"


def propose(rec: dict) -> dict:
    sym = rec["symbol"]
    bars = rec.get("bars") or []
    window, note = anchor_window(bars)
    out = {"symbol": sym, "note": note, "entry_low": 0, "entry_high": 0,
           "invalidation_price": 0, "close": None, "status": ""}
    if not window:
        out["status"] = "skip: no data"
        return out

    closes = [b["adjclose"] for b in bars if b.get("adjclose") is not None]
    out["close"] = round_price(closes[-1])
    if len(window) < MIN_WINDOW_BARS:
        out["status"] = f"skip: only {len(window)} sessions since the break -- too soon for a zone"
        return out

    s = sorted(window)
    dist = percentile(s, HIGH_PCTILE)
    if dist <= 0:
        out["status"] = "skip: degenerate range"
        return out

    # A pure distribution percentile is useless for a name in a strong uptrend:
    # SLS at $12.36 would get a "buy zone" of $1.91, the price before it
    # re-rated, which it will never revisit if the thesis is working. So the
    # zone is the higher of the distribution level and a realistic pullback --
    # the 50-day average, capped at 5% below spot so we never define a zone that
    # amounts to chasing.
    last = closes[-1]
    sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
    pullback = min(sma50, last * 0.95) if sma50 else last * 0.95
    hi = max(dist, pullback)
    anchor = "distribution" if dist >= pullback else "pullback to SMA50"

    out["entry_high"] = round_price(hi)
    # Scale-in reference, not a gate: below this, verify the thesis before adding.
    out["entry_low"] = round_price(hi * (1 - SCALE_IN_DROP))
    # Invalidation sits below the anchor window's own low: if the name trades
    # under the worst level of the regime the zone was built from, the setup
    # that justified the zone no longer exists. Mechanical and overridable.
    out["invalidation_price"] = round_price(min(window) * 0.95)
    out["note"] = f"{note}, {anchor}"

    if last <= out["entry_high"]:
        out["status"] = "IN ZONE"
    else:
        out["status"] = f"above zone by {((last / hi) - 1) * 100:.0f}%"
    return out


def apply_to_watchlist(path: Path, zones: dict) -> int:
    """Rewrite entry_low/entry_high in place, preserving comments and layout."""
    lines = path.read_text().splitlines()
    current, changed = None, 0
    for i, line in enumerate(lines):
        m = re.match(r'\s*symbol\s*=\s*"([^"]+)"', line)
        if m:
            current = m.group(1).upper()
            continue
        if current and current in zones:
            z = zones[current]
            if not z["entry_high"]:
                continue
            if re.match(r"\s*entry_low\s*=", line):
                lines[i] = f'entry_low = {z["entry_low"]}'
                changed += 1
            elif re.match(r"\s*entry_high\s*=", line):
                lines[i] = f'entry_high = {z["entry_high"]}'
            elif re.match(r"\s*invalidation_price\s*=", line):
                lines[i] = f'invalidation_price = {z.get("invalidation_price", 0)}'
    path.write_text("\n".join(lines) + "\n")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="Propose entry zones from price history")
    ap.add_argument("--snapshot", default=str(DATA / "latest.json"))
    ap.add_argument("--watchlist", default=str(ROOT / "watchlist.toml"))
    ap.add_argument("--apply", action="store_true", help="write into watchlist.toml")
    args = ap.parse_args()

    snap = json.loads(Path(args.snapshot).read_text())
    zones = {sym: propose(rec) for sym, rec in snap["tickers"].items()}

    order = {"IN ZONE": 0, "below zone": 1}
    rows = sorted(zones.values(), key=lambda z: (order.get(z["status"], 2), z["symbol"]))
    print(f"{'Ticker':<8}{'Close':>10}{'entry_low':>11}{'entry_high':>12}  {'Status':<18} Window")
    print("-" * 92)
    for z in rows:
        lo = f"{z['entry_low']}" if z["entry_high"] else "-"
        hi = f"{z['entry_high']}" if z["entry_high"] else "-"
        print(f"{z['symbol']:<8}{z['close'] or '-'!s:>10}{lo:>11}{hi:>12}  {z['status']:<18} {z['note']}")

    priced = sum(1 for z in zones.values() if z["entry_high"])
    print(f"\n{priced}/{len(zones)} names have a zone; "
          f"{sum(1 for z in zones.values() if z['status'] == 'IN ZONE')} currently in it.")

    if args.apply:
        n = apply_to_watchlist(Path(args.watchlist), zones)
        print(f"applied {n} zones to {args.watchlist}")
    else:
        print("dry run -- re-run with --apply to write these into watchlist.toml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
