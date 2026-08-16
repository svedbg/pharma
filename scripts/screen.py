#!/usr/bin/env python3
"""Monthly screen for candidates outside the watchlist.

The daily desk only ever looks at names already on the list, so the best
opportunity in the sector can pass by unseen. This widens the aperture once a
month without adding daily noise.

Two stages, because the wide universe is too expensive to fetch in full:

  1. Cheap  -- prices only, for every biotech-shaped registrant in SEC's company
               list that is not already watched. One request per name.
  2. Deep   -- the shortlist alone gets the full treatment; run the normal
               pipeline against a candidates file to see filings, runway and
               vetoes.

The output is deliberately a *shortlist to research*, not a buy list. It applies
the same technical thresholds as the desk, which are known not to be an edge on
their own -- what they are good at is narrowing 700 names to a dozen.

    python3 scripts/screen.py                 # screen, print the shortlist
    python3 scripts/screen.py --limit 400     # cap the universe (default 500)
    python3 scripts/screen.py --out candidates_screen.toml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import tomllib

import fetch
from signals import (
    CAPITULATION_VOL,
    SETUP_PCTB,
    SETUP_RSI,
    bollinger_pct_b,
    percentile_rank,
    rsi,
    tradability,
)

DATA = ROOT / "data"

# SEC's company list has no sector field, so the universe is built by name.
# Crude but effective and free: these fragments cover the sector's naming
# conventions, and a false positive costs one price request.
NAME_HINTS = (
    "therapeutic", "pharma", "biosci", "bioscience", "biotech", "biopharm",
    "oncolog", "genetic", "genomic", "immuno", "medicin", "laborator",
    "biolog", "vaccin", "cell thera", "gene thera", "neuro", "cardio",
)
NAME_BLOCKERS = (
    "acquisition corp", "capital corp", "trust", "etf", "fund", "index",
    "bancorp", "insurance", "realty", "mining", "energy",
)
MIN_DOLLAR_VOLUME = 500_000       # below this nothing is tradeable at size
MIN_PRICE = 0.50                  # sub-50c is usually a compliance problem


def build_universe(watched: set[str], limit: int) -> list[tuple[str, str]]:
    cik_map = fetch.load_cik_map()
    out = []
    for ticker, info in cik_map.items():
        if ticker in watched or not ticker.isalpha() or len(ticker) > 5:
            continue
        title = (info.get("title") or "").lower()
        if not any(h in title for h in NAME_HINTS):
            continue
        if any(b in title for b in NAME_BLOCKERS):
            continue
        out.append((ticker, info["title"]))
    out.sort()
    return out[:limit]


def score(symbol: str, name: str) -> dict | None:
    """Price-only assessment. Returns None if the name cannot be traded or read."""
    try:
        bars, _meta, source = fetch.fetch_bars(symbol, 400)
    except fetch.FetchError:
        return None
    bars = [b for b in bars if b.get("adjclose") is not None]
    if len(bars) < 60:
        return None

    closes = [b["adjclose"] for b in bars]
    # Index-aligned with bars, gaps included -- see technical_metrics().
    vols = [b.get("volume") for b in bars]
    last = closes[-1]
    r = rsi(closes)
    pctb, _u, _l = bollinger_pct_b(closes)
    if r is None or pctb is None or last < MIN_PRICE:
        return None

    tr = tradability(bars, {}) or {}
    if (tr.get("median_dollar_volume_20d") or 0) < MIN_DOLLAR_VOLUME:
        return None

    volr = None
    window = [v for v in vols[-20:] if v]
    if vols[-1] and len(window) >= 10:
        volr = vols[-1] / (sum(window) / len(window))

    year = closes[-252:]
    return {
        "symbol": symbol, "name": name, "source": source,
        "close": round(last, 4),
        "rsi": round(r, 2),
        "pctb": round(pctb, 3),
        "percentile_1y": round(percentile_rank(year, last) or 100.0, 1),
        "off_52w_high": round((last / max(year) - 1.0) * 100.0, 1),
        "volume_ratio": round(volr, 2) if volr else None,
        "dollar_volume": tr.get("median_dollar_volume_20d"),
        "oversold": bool(r < SETUP_RSI and pctb < SETUP_PCTB),
        "capitulation": bool(volr and volr > CAPITULATION_VOL),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Screen for candidates outside the watchlist")
    ap.add_argument("--limit", type=int, default=500, help="max universe size")
    ap.add_argument("--top", type=int, default=15, help="how many to shortlist")
    ap.add_argument("--watchlist", default=str(ROOT / "watchlist.toml"))
    ap.add_argument("--out", default=None, help="write the shortlist as a TOML candidate file")
    args = ap.parse_args()

    watched = set()
    wl = Path(args.watchlist)
    if wl.exists():
        watched = {t["symbol"].upper() for t in tomllib.loads(wl.read_text()).get("ticker", [])}

    universe = build_universe(watched, args.limit)
    print(f"universe: {len(universe)} biotech-shaped registrants not already watched",
          file=sys.stderr)
    print("(prices only -- one request each; this is the slow part)", file=sys.stderr)

    rows, failed = [], 0
    for i, (sym, name) in enumerate(universe, 1):
        res = score(sym, name)
        if res:
            rows.append(res)
        else:
            failed += 1
        if i % 50 == 0:
            print(f"  {i}/{len(universe)} screened, {len(rows)} usable", file=sys.stderr)

    print(f"screened {len(universe)}: {len(rows)} usable, {failed} skipped "
          f"(illiquid, sub-${MIN_PRICE}, or no data)", file=sys.stderr)

    # Rank the way the desk does: oversold first, volume-confirmed above not,
    # then by how cheap the name is against its own year.
    rows.sort(key=lambda r: (not r["oversold"], not r["capitulation"], r["percentile_1y"]))
    top = rows[: args.top]

    print(f"\n{'Ticker':<8}{'Close':>9}{'RSI':>7}{'%B':>8}{'1y pct':>8}"
          f"{'off high':>10}{'vol x':>7}{'$vol/day':>12}  Name")
    print("-" * 108)
    for r in top:
        mark = "*" if r["oversold"] else " "
        cap = "+" if r["capitulation"] else " "
        print(f"{mark}{cap}{r['symbol']:<6}{r['close']:>9}{r['rsi']:>7}{r['pctb']:>8}"
              f"{r['percentile_1y']:>8}{r['off_52w_high']:>9}%{(r['volume_ratio'] or 0):>7}"
              f"{r['dollar_volume'] or 0:>12,.0f}  {r['name'][:34]}")
    print("\n* oversold on the desk's thresholds   + capitulation volume")
    print("This is a shortlist to research, not a buy list. The technical trigger")
    print("is not an edge on its own -- it is only good at narrowing the field.")

    if args.out:
        blocks = ['# Screen output. Review, then paste wanted names into watchlist.toml.\n']
        for r in top:
            blocks.append(f'''
[[ticker]]
symbol = "{r['symbol']}"
tier = "B"
sponsor = ""
thesis = ""          # {r['name']}
entry_low = 0
entry_high = 0
invalidation_price = 0
invalidation = ""''')
        Path(args.out).write_text("\n".join(blocks) + "\n")
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
