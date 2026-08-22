#!/usr/bin/env python3
"""Drill into one ticker without loading the whole snapshot.

With ~60 names on the watchlist, data/latest.json runs to several megabytes --
far too large to read during analysis. The daily run therefore reads the compact
signals file, then calls this for the handful of names that actually matter:

    python3 scripts/detail.py OTLK
    python3 scripts/detail.py OTLK --json          # full record, machine-readable
    python3 scripts/detail.py OTLK --bars 30       # recent price action
    python3 scripts/detail.py OTLK --dataset data/premarket   # a different run's data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localconfig

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def zone_lines(rec: dict, sig: dict) -> list[str]:
    """The two levels the tier actually turns on, rendered in one place.

    ACT requires price <= entry_high, and invalidation_price is what the
    highest-severity exit flag is measured against. This drill-down printed
    neither -- fetch.py carries invalidation_price into the snapshot with a
    comment saying it was added so this file could show it, and it still did
    not. Reading a name without them is reading it without the two numbers a
    decision uses.

    brief.py needs the same two levels for the whole list, so they are rendered
    here rather than in each caller. Two spellings of "in zone" is how a rule
    ends up being scored against a paraphrase of itself -- the same argument
    zone_from_closes() settles for the zone arithmetic.
    """
    price = sig.get("price") or {}
    close = price.get("close")
    lo, hi = rec.get("entry_low") or 0, rec.get("entry_high") or 0
    stop = rec.get("invalidation_price") or 0
    if not (hi or stop):
        return []

    out = []
    zone = (f"zone ${lo} - ${hi}" if hi else "no entry zone declared -- ACT disabled")
    where = ""
    if hi and close is not None:
        where = ("  [IN ZONE]" if close <= hi
                 else f"  [above zone by {(close / hi - 1) * 100:.0f}%]")
    if sig.get("zone_stale"):
        drift = sig.get("zone_drift_pct")
        where += f"  [ZONE STALE: {drift:+.0f}% from it -- re-run propose_zones.py]"
    out.append(f"{zone}{where}")
    if stop:
        gap = ((close / stop - 1) * 100) if close is not None else None
        out.append(f"invalidation ${stop}"
                   + (f" ({gap:+.0f}% away)" if gap is not None else ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Show one ticker's full record")
    ap.add_argument("symbol")
    ap.add_argument("--json", action="store_true", help="dump the raw record")
    ap.add_argument("--bars", type=int, default=15, help="how many recent bars to print")
    ap.add_argument("--filings", type=int, default=20)
    # ONE flag for both files, not a --snapshot and a --signals. They are two
    # halves of a single run: the snapshot holds the prices and filings, the
    # signals file holds the tier, vetoes and zone derived from exactly those.
    # Overriding one and not the other would print a fresh price under last
    # night's veto -- a drill-down that is internally inconsistent while looking
    # entirely normal, which is the failure mode this codebase keeps paying for.
    # A directory cannot be half-set.
    ap.add_argument("--dataset", default=str(DATA), metavar="DIR",
                    help="directory holding latest.json and signals.json "
                         "(default: data/; the pre-market run writes data/premarket/)")
    args = ap.parse_args()

    sym = args.symbol.upper()
    dataset = Path(args.dataset)
    snap = localconfig.require_data(dataset / "latest.json")
    rec = snap["tickers"].get(sym)
    if not rec:
        print(f"{sym} not in snapshot. Available: {', '.join(sorted(snap['tickers']))}", file=sys.stderr)
        return 1

    if args.json:
        rec = dict(rec)
        rec["bars"] = rec.get("bars", [])[-args.bars:]
        print(json.dumps(rec, indent=2))
        return 0

    sig = {}
    sig_path = dataset / "signals.json"
    if sig_path.exists():
        for s in json.loads(sig_path.read_text())["signals"]:
            if s["symbol"] == sym:
                sig = s
                break

    print(f"=== {sym} — {rec.get('company', '')} [{rec.get('tier', '')}] ===")
    if rec.get("thesis"):
        print(f"thesis: {rec['thesis']}")
    if rec.get("invalidation"):
        print(f"invalidation: {rec['invalidation']}")
    if sig:
        p, t = sig.get("price", {}), sig.get("technicals", {})
        print(f"\ntier={sig.get('tier')} (prev {sig.get('previous_tier')})  max size {sig.get('max_position_pct')}%")
        print(f"close ${p.get('close')} on {p.get('date')} | 1d {p.get('chg_1d_pct')}% "
              f"5d {p.get('chg_5d_pct')}% 20d {p.get('chg_20d_pct')}%")
        print(f"52w {p.get('week52_low')}-{p.get('week52_high')} | off high {p.get('pct_off_52w_high')}% "
              f"| 1y pctile {p.get('percentile_1y')}")
        print(f"RSI {t.get('rsi14')} | %B {t.get('bollinger_pct_b')} | vs SMA50 {t.get('pct_vs_sma50')}% "
              f"| vs SMA200 {t.get('pct_vs_sma200')}% | ATR {t.get('atr_pct_of_price')}% of price")
        print(f"vol vs 20d avg: {t.get('volume_vs_20d_avg')}x")

        lines = zone_lines(rec, sig)
        if lines:
            print("")
            for line in lines:
                print(line)

        conv = sig.get("conviction") or {}
        if conv:
            print(f"conviction: {conv.get('label')} ({conv.get('score'):+d})")
            for s in conv.get("supporting", []):
                print(f"  +  {s}")
            for s in conv.get("against", []):
                print(f"  -  {s}")

        # analyse() appends each exit flag to `reasons` as well, truncated to
        # 150 characters. Printing both put every exit on the screen twice, once
        # cut off mid-sentence, so the reasons list drops them and the block
        # below carries them in full.
        for x in sig.get("reasons", []):
            if not x.startswith("EXIT SIGNAL"):
                print(f"  - {x}")

        # The exit half of the desk. Omitting it made this a buy-side-only view
        # of a name that may already have broken its own thesis.
        for fl in sig.get("exit_flags", []):
            print(f"  EXIT ({fl['severity']}) {fl['kind']}: {fl['detail']}")
        for h in sig.get("hard_vetoes", []):
            print(f"  HARD VETO  {h['form']} {h['filed']} ({h['days_ago']}d): {h['reason']}")
            if h.get("url"):
                print(f"             {h['url']}")
        for s in sig.get("soft_flags", []):
            print(f"  soft flag  {s['form']} {s['filed']} ({s['days_ago']}d): {s['reason']}")
        for e in sig.get("recent_events", []):
            print(f"  EVENT      {e['date']} {e['change_pct']}% ({e['kind']}, {e['sessions_ago']} sessions ago)")
            for c in e.get("likely_cause_filings", []):
                print(f"             cause? {c['form']} {c['filed']} {c.get('items','')} {c.get('url','')}")

        rw, dl = sig.get("runway"), sig.get("dilution")
        if rw:
            stale = "  [STALE - verify before sizing]" if rw.get("stale") else ""
            head = ("cash-flow positive -- no runway to exhaust"
                    if rw.get("cash_flow_positive")
                    else f"{rw['quarters']}q (~{rw['months']}mo)")
            print(f"\nrunway: {head}{stale}")
            print(f"  liquidity ${rw['liquidity_usd']:,.0f} as of {rw['cash_as_of']} "
                  f"({rw.get('age_days')}d old) = cash ${(rw.get('cash_usd') or 0):,.0f} "
                  f"+ short-term investments ${(rw.get('investments_usd') or 0):,.0f}")
            flow = ("generated" if rw.get("cash_flow_positive") else "burn")
            print(f"  {flow} ${rw['quarterly_burn_usd']:,.0f}/q"
                  + (f" | dry ~{rw['estimated_exhaustion']}"
                     if rw.get("estimated_exhaustion") else ""))
        if dl:
            print(f"dilution: {dl['shares_start']:,.0f} -> {dl['shares_now']:,.0f} "
                  f"({dl['change_pct']:+}%) between {dl['from']} and {dl['to']}")

    print(f"\n--- last {args.bars} sessions")
    for b in (rec.get("bars") or [])[-args.bars:]:
        v = b.get("volume") or 0
        print(f"  {b['date']}  O {b.get('open')}  H {b.get('high')}  L {b.get('low')}  C {b['close']}  V {v:,.0f}")

    print(f"\n--- filings (most recent {args.filings})")
    for f in (rec.get("filings") or [])[: args.filings]:
        meanings = "; ".join(f.get("item_meanings", []))
        print(f"  {f['filed']}  {f['form']:<12} {f.get('items', ''):<14} {meanings}")
        print(f"      {f['url']}")

    trials = rec.get("trials") or []
    if trials:
        print("\n--- trials")
        for t in trials[:10]:
            print(f"  {t['nct_id']}  {t['status']:<24} {t.get('phase', ''):<12} "
                  f"primary completion {t.get('primary_completion')}  {(t.get('title') or '')[:60]}")

    if rec.get("errors"):
        print("\n--- errors")
        for e in rec["errors"]:
            print(f"  {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
