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
                  realistic pullback level -- the average of the window's last
                  50 sessions, capped 5% below spot. Percentiles alone are
                  useless for a name in a strong uptrend: they return the price
                  before it re-rated, which it will never revisit if the thesis
                  is working. Both legs read the anchor window and nothing
                  outside it, so a post-collapse zone cannot be lifted by prices
                  from before the break.

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
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localconfig

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

COLLAPSE_PCT = -25.0
COLLAPSE_LOOKBACK = 120
MIN_WINDOW_BARS = 25
YEAR_BARS = 252
HIGH_PCTILE = 25.0
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


def zone_from_closes(closes: list[float]) -> dict | None:
    """The zone a close series implies, or None when it cannot support one.

    Split out of propose() so scripts/backtest.py can reconstruct the zone as of
    any historical day and score it, without a second copy of the rule. The
    threshold table in CLAUDE.md is only worth anything if the instrument
    measures the rule the desk actually runs -- backtest.py already learned that
    once, by scoring an abandoned RSI pair while calling it live.

    Takes closes rather than bars because that is all the arithmetic needs, and
    a caller replaying history has the closes already.
    """
    window, note = anchor_window([{"adjclose": c} for c in closes])
    if not window or len(window) < MIN_WINDOW_BARS:
        return None
    dist = percentile(sorted(window), HIGH_PCTILE)
    if dist <= 0:
        return None

    # A pure distribution percentile is useless for a name in a strong uptrend:
    # SLS at $12.36 would get a "buy zone" of $1.91, the price before it
    # re-rated, which it will never revisit if the thesis is working. So the
    # zone is the higher of the distribution level and a realistic pullback --
    # the trailing average, capped at 5% below spot so we never define a zone
    # that amounts to chasing.
    #
    # Averaged over the ANCHOR WINDOW, not over `closes`. Taking closes[-50:]
    # reached back through the break for any name whose post-collapse window is
    # shorter than 50 sessions, which is the whole reason the window exists:
    # pre-break prices describe a company that no longer trades. The percentile
    # leg respected that and the pullback leg quietly did not, so the excluded
    # prices came back in through the other half of the same max(). The cap hid
    # most of it -- min() with last*0.95 only lets the average through when the
    # name is already above it -- and it went wrong in the one direction that
    # matters, raising the zone on the freshest collapses, which is exactly the
    # falling-knife case. The window is never shorter than MIN_WINDOW_BARS, so
    # this always averages at least 25 sessions.
    last = closes[-1]
    tail = window[-50:]
    pullback = min(sum(tail) / len(tail), last * 0.95)
    hi = max(dist, pullback)
    return {
        "entry_high": round_price(hi),
        # Scale-in reference, not a gate: below this, verify the thesis before adding.
        "entry_low": round_price(hi * (1 - SCALE_IN_DROP)),
        # Invalidation sits below the anchor window's own low: if the name trades
        # under the worst level of the regime the zone was built from, the setup
        # that justified the zone no longer exists. Mechanical and overridable.
        "invalidation_price": round_price(min(window) * 0.95),
        "window_note": note,
        "anchor": "distribution" if dist >= pullback else "pullback to the window average",
        "window_bars": len(window),
    }


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

    z = zone_from_closes(closes)
    if z is None:
        out["status"] = "skip: degenerate range"
        return out

    last = closes[-1]
    out["entry_high"] = z["entry_high"]
    out["entry_low"] = z["entry_low"]
    out["invalidation_price"] = z["invalidation_price"]
    out["note"] = f"{note}, {z['anchor']}"

    if last <= out["entry_high"]:
        out["status"] = "IN ZONE"
    else:
        out["status"] = f"above zone by {((last / out['entry_high']) - 1) * 100:.0f}%"
    return out


def apply_to_watchlist(path: Path, zones: dict) -> int:
    """Rewrite entry_low/entry_high in place, preserving comments and layout.

    This edits text rather than reserialising the TOML, because the file is a
    hand-maintained trading plan whose comments and grouping matter more than
    the writer's convenience. That means tracking which section each line
    belongs to by hand -- see the reset below.
    """
    KEYS = ("entry_low", "entry_high", "invalidation_price")
    lines = path.read_text().splitlines()
    current, changed = None, 0
    # Which of KEYS each ticker's block already contains, and where that block
    # ends. A key the block does not have cannot be rewritten in place, and
    # rewriting is all this used to do -- so a ticker added by hand without an
    # `invalidation_price` line never got one, and --apply reported success
    # having silently skipped the highest-severity exit flag the desk has. That
    # is the same shape as the bug that left invalidation_price unreachable for
    # every name on the list: nothing looked broken, the field simply was not
    # there.
    seen: dict[str, set[str]] = {}
    last_line: dict[str, int] = {}

    for i, line in enumerate(lines):
        # Any section header ends the previous ticker's scope. Without this the
        # last ticker in the file stayed "current" through everything after it,
        # so a key named entry_low/entry_high/invalidation_price in a later
        # section -- [settings] sits at the top today, but nothing enforces that
        # -- would be silently rewritten with a ticker's zone.
        if re.match(r"\s*\[", line):
            current = None
        m = re.match(r'\s*symbol\s*=\s*"([^"]+)"', line)
        if m:
            current = m.group(1).upper()
            seen.setdefault(current, set())
            last_line[current] = i
            continue
        if not current or current not in zones:
            continue
        if line.strip():
            last_line[current] = i
        z = zones[current]
        if not z["entry_high"]:
            continue
        for key in KEYS:
            if re.match(rf"\s*{key}\s*=", line):
                lines[i] = f"{key} = {z[key]}"
                seen[current].add(key)
                if key == "entry_low":
                    changed += 1
                break

    # Insert whatever the block was missing, at the end of that block, deepest
    # first so the earlier insertions do not shift the later line numbers.
    added = 0
    for sym, at in sorted(last_line.items(), key=lambda kv: -kv[1]):
        z = zones.get(sym)
        if not z or not z["entry_high"]:
            continue
        missing = [k for k in KEYS if k not in seen.get(sym, set())]
        if not missing:
            continue
        lines[at + 1:at + 1] = [f"{k} = {z[k]}" for k in missing]
        added += len(missing)
        print(f"[zones] {sym}: added missing {', '.join(missing)}", file=sys.stderr)

    path.write_text("\n".join(lines) + "\n")
    return changed + added


def main() -> int:
    ap = argparse.ArgumentParser(description="Propose entry zones from price history")
    ap.add_argument("--snapshot", default=str(DATA / "latest.json"))
    ap.add_argument("--watchlist", default=str(ROOT / "watchlist.toml"))
    ap.add_argument("--apply", action="store_true", help="write into watchlist.toml")
    args = ap.parse_args()

    snap = localconfig.require_data(Path(args.snapshot))
    zones = {sym: propose(rec) for sym, rec in snap["tickers"].items()}

    # "below zone" was in this map but propose() has never produced it -- the
    # statuses are "IN ZONE", "above zone by N%" and the skips. A sort key for a
    # value that cannot occur reads as though the vocabulary is wider than it is.
    order = {"IN ZONE": 0}
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
