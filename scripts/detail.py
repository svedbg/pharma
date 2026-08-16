#!/usr/bin/env python3
"""Drill into one ticker without loading the whole snapshot.

With ~60 names on the watchlist, data/latest.json runs to several megabytes --
far too large to read during analysis. The daily run therefore reads the compact
signals file, then calls this for the handful of names that actually matter:

    python3 scripts/detail.py OTLK
    python3 scripts/detail.py OTLK --json          # full record, machine-readable
    python3 scripts/detail.py OTLK --bars 30       # recent price action
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Show one ticker's full record")
    ap.add_argument("symbol")
    ap.add_argument("--json", action="store_true", help="dump the raw record")
    ap.add_argument("--bars", type=int, default=15, help="how many recent bars to print")
    ap.add_argument("--filings", type=int, default=20)
    args = ap.parse_args()

    sym = args.symbol.upper()
    snap = localconfig.require_data(DATA / "latest.json")
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
    sig_path = DATA / "signals.json"
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

        for _label, key in (("REASONS", "reasons"),):
            for x in sig.get(key, []):
                print(f"  - {x}")
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
