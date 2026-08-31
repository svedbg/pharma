#!/usr/bin/env python3
"""The whole list in one screen, so the analysis pass never reads signals.json whole.

`data/signals.json` is "the compact file" only relative to the several megabytes
of `data/latest.json` that the daily prompt has always forbidden reading. At 60+
names it is 487KB -- about 122,000 tokens -- and the prompt said to start there
and read all of it. That is fifteen times the size of the report it produces,
re-sent on every turn of the run, and most of it is drill-down material the
list-wide view never touches: seven chart URLs per name sitting next to the
ready-made markdown line that supersedes them, the XBI regime block repeated
identically for all 62 names, every moving average, every insider transaction,
the full provenance of every balance sheet.

So this prints the list-wide view and `detail.py` still prints the per-name one.
It is the same division the prompt already draws between signals.json and
latest.json, moved up one level now that the list has grown into the same shape.

    python3 scripts/brief.py
    python3 scripts/brief.py --dataset data/premarket
    python3 scripts/brief.py --all       # a block for every name, not just live ones

It reads **both** files behind `--dataset`, for the reason detail.py takes one
flag for the pair: the entry zone and `invalidation_price` live in the snapshot
while the tier and vetoes derived from them live in the signals file, and a view
that mixed a fresh price with last night's veto would look entirely normal. A
directory cannot be half-set.

Three tiers of detail, mirroring the report's own structure: `triage()` names get
a full block, `flagged()` names get one line under "also flagged", and the rest
are named in a single roster line with a count. That last line is not decoration
-- a view that quietly drops two thirds of the watchlist reads as "we looked at
everything, and this is what there was", which is the failure `--limit` in
screen.py already paid for once.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localconfig
from detail import zone_lines

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

TIER_ORDER = {"ACT": 0, "SETUP": 1, "WATCH": 2, "NONE": 3}

# The deep-dive triage bar in prompts/daily.md is "a catalyst inside 30 days";
# the report's catalyst calendar covers 90. So a catalyst inside 30 days earns a
# full block and one inside 90 earns a mention -- and every dated catalyst,
# whatever its distance, appears in the calendar section regardless.
TRIAGE_CATALYST_DAYS = 30
FLAG_CATALYST_DAYS = 90


def triage(sig: dict) -> bool:
    """Whether this name gets a block of its own.

    This is the deep-dive bar out of prompts/daily.md -- tier at WATCH or above,
    a hard veto or a recent_events entry, new filings since the last run, or a
    catalyst inside 30 days -- plus any exit flag. The report is written off
    these blocks, so a name the prompt is told to deep-dive that arrived here as
    one line would be researched from a summary.

    Exit flags are the one addition. They are not in the prompt's triage list
    because that list predates them, and an `invalidation_breached` reduced to a
    one-liner is exactly the buy-side-only view of a name that has already
    broken its thesis that exit_signals() exists to prevent.
    """
    if sig.get("tier") not in (None, "NONE"):
        return True
    for field in ("hard_vetoes", "recent_events", "new_filings_since_last_run",
                  "exit_flags"):
        if sig.get(field):
            return True
    return _catalyst_within(sig, TRIAGE_CATALYST_DAYS)


def flagged(sig: dict) -> bool:
    """Whether a name below the triage bar still gets a line of its own.

    The prompt's own structure: deep-dive the ones that qualify, "list the rest
    under an 'also flagged' heading with one line each". These are the things the
    report has sections for but which do not on their own justify research -- an
    unusual move, a soft flag, insider activity, a divergence from XBI, a
    catalyst inside the calendar window.
    """
    ins = sig.get("insiders") or {}
    if sig.get("soft_flags"):
        return True
    # A tier change worth a line is one that *left* a tier -- a name that was at
    # WATCH last night and is not tonight. Not every tier_changed: on a fresh
    # state file previous_tier is None for all 62 names, so tier_changed is True
    # for all 62, and a bar that trusted it would put the whole watchlist under
    # "also flagged" on the first run and look like a busy night.
    if sig.get("tier_changed") and sig.get("previous_tier") not in (None, "NONE"):
        return True
    if (sig.get("move") or {}).get("big_move"):
        return True
    if ins.get("cluster_buy") or ins.get("notable_buy") or ins.get("net_selling"):
        return True
    if (sig.get("relative_strength") or {}).get("idiosyncratic"):
        return True
    return _catalyst_within(sig, FLAG_CATALYST_DAYS)


def _catalyst_within(sig: dict, days: int) -> bool:
    for c in sig.get("catalysts") or []:
        d = c.get("days_until")
        if d is not None and d <= days:
            return True
    return False


def _one_line(sig: dict) -> str:
    """A flagged-but-not-triaged name, in one line: what it is and why it is here."""
    p = sig.get("price") or {}
    rs = (sig.get("relative_strength") or {}).get("20d") or {}
    ins = sig.get("insiders") or {}
    why = []
    mv = sig.get("move") or {}
    if mv.get("big_move"):
        why.append(f"big move {_pct(mv.get('chg_1d_pct'))} = {mv.get('sigma')} sigma")
    if sig.get("tier_changed") and sig.get("previous_tier") not in (None, "NONE"):
        why.append(f"left {sig.get('previous_tier')} -> {sig.get('tier')}")
    if (sig.get("relative_strength") or {}).get("idiosyncratic"):
        why.append("idiosyncratic vs XBI")
    for k in ("cluster_buy", "notable_buy", "net_selling"):
        if ins.get(k):
            why.append(k)
    for f in sig.get("soft_flags") or []:
        why.append(f"soft: {f['form']}")
    for c in sig.get("catalysts") or []:
        d = c.get("days_until")
        if d is not None and d <= FLAG_CATALYST_DAYS:
            why.append(f"{c.get('kind')} in {d}d ({c.get('confidence')})")
    return (f"  {sig.get('symbol'):6s} {sig.get('bucket') or '-':8s} "
            f"${p.get('close')} 1d {_pct(p.get('chg_1d_pct'))} "
            f"vs XBI 20d {_pct(rs.get('excess_pct'), 'pp')} | " + "; ".join(why))


def _pct(v, suffix: str = "%") -> str:
    return "n/a" if v is None else f"{v:+.1f}{suffix}"


def _runway_line(rw: dict | None) -> str:
    """One line of runway, with the two reasons a figure may not be trusted.

    `stale` and `superseded_by` are not decoration: the veto layer refuses to
    fire on either, and `runway_ok` refuses to clear a name on either. A brief
    that printed the bare number would invite the report to read a superseded
    0.59q as distress, which is the exact contradiction CLAUDE.md removed from
    signals.py.
    """
    if not rw:
        return "runway: unknown (no us-gaap XBRL -- report as unknown, never as healthy)"
    if rw.get("cash_flow_positive"):
        return "runway: cash-flow positive -- funds itself, no runway to exhaust"
    q = rw.get("quarters")
    if q is None:
        return "runway: unknown"
    out = f"runway: {q}q (~{rw.get('months')}mo)"
    if rw.get("stale"):
        out += f"  [STALE {rw.get('age_days')}d -- soft flag only, not distress]"
    sup = rw.get("superseded_by")
    if sup:
        out += (f"  [SUPERSEDED by {sup.get('form')} filed {sup.get('filed')}"
                f" -- may not gate ACT]")
    if rw.get("cash_only_verified"):
        out += "  [cash-only confirmed by a full XBRL tag sweep]"
    return out


def _block(sig: dict, rec: dict) -> list[str]:
    """Everything the list-wide view says about one name."""
    out: list[str] = []
    sym = sig.get("symbol")
    tier = sig.get("tier")
    prev = sig.get("previous_tier")
    changed = "  <- TIER CHANGED" if sig.get("tier_changed") else ""
    out.append(f"### {sym} — {sig.get('company', '')}  [{tier}]"
               f"  bucket {sig.get('bucket')}  max {sig.get('max_position_pct')}%"
               f"  (prev {prev}){changed}")
    if sig.get("thesis"):
        out.append(f"  thesis: {sig['thesis']}")
    else:
        out.append("  thesis: (none set -- candidate for thesis bootstrap)")
    if sig.get("invalidation"):
        out.append(f"  invalidation: {sig['invalidation']}")

    p, t = sig.get("price") or {}, sig.get("technicals") or {}
    out.append(f"  ${p.get('close')} on {p.get('date')} | 1d {_pct(p.get('chg_1d_pct'))} "
               f"5d {_pct(p.get('chg_5d_pct'))} 20d {_pct(p.get('chg_20d_pct'))} | "
               f"off 52wH {_pct(p.get('pct_off_52w_high'))} | 1y pctile {p.get('percentile_1y')}")
    cap = sig.get("capitulation_volume")
    out.append(f"  RSI {t.get('rsi14')} | %B {t.get('bollinger_pct_b')} | "
               f"vol {t.get('volume_vs_20d_avg')}x 20d avg | "
               f"capitulation_volume {'YES' if cap else 'no'}"
               + ("" if cap else "  (ACT requires it)"))

    mv = sig.get("move") or {}
    if mv.get("big_move"):
        out.append(f"  BIG MOVE {_pct(mv.get('chg_1d_pct'))} = {mv.get('sigma')} sigma "
                   f"(typical {mv.get('typical_daily_move_pct')}%), gap {_pct(mv.get('gap_pct'))}, "
                   f"XBI {_pct(mv.get('benchmark_1d_pct'))}, excess {_pct(mv.get('excess_1d_pct'))}")

    rs = sig.get("relative_strength") or {}
    if rs:
        d20 = rs.get("20d") or {}
        tags = []
        if rs.get("idiosyncratic"):
            tags.append("IDIOSYNCRATIC -- company-specific, find the cause")
        if rs.get("sector_wide"):
            tags.append("sector_wide -- most of this is beta")
        out.append(f"  vs XBI 20d: {_pct(d20.get('excess_pct'), 'pp')} "
                   f"(name {_pct(d20.get('stock_pct'))}, XBI {_pct(d20.get('benchmark_pct'))})"
                   + (f"  [{'; '.join(tags)}]" if tags else ""))

    for line in zone_lines(rec, sig):
        out.append(f"  {line}")

    conv = sig.get("conviction") or {}
    if conv:
        out.append(f"  conviction: {conv.get('label')} ({conv.get('score'):+d})")
        for s in conv.get("supporting", []):
            out.append(f"    +  {s}")
        for s in conv.get("against", []):
            out.append(f"    -  {s}")

    # analyse() appends a truncated copy of each exit flag and a "HARD VETO xN"
    # digest of the first three vetoes to `reasons`, and both print in full
    # below. detail.py already drops the exit copies for that reason; here the
    # veto digest goes too, because in a view whose whole purpose is compactness
    # a cut-off restatement of the lines immediately underneath is the worst of
    # both. What is left is what `reasons` alone carries: the oversold reading,
    # the RSI_TRAP distress warning, the divergence from XBI, the insider
    # cluster, the short-selling pressure.
    for r in sig.get("reasons") or []:
        if not r.startswith(("EXIT SIGNAL", "HARD VETO")):
            out.append(f"  - {r}")

    for fl in sig.get("exit_flags") or []:
        out.append(f"  EXIT ({fl['severity']}) {fl['kind']}: {fl['detail']}")
    for h in sig.get("hard_vetoes") or []:
        out.append(f"  HARD VETO  {h['form']} {h['filed']} ({h['days_ago']}d): {h['reason']}")
        if h.get("url"):
            out.append(f"             {h['url']}")
    for s in sig.get("soft_flags") or []:
        out.append(f"  soft flag  {s['form']} {s['filed']} ({s['days_ago']}d): {s['reason']}")
    for e in sig.get("recent_events") or []:
        out.append(f"  EVENT      {e['date']} {e['change_pct']}% "
                   f"({e['kind']}, {e['sessions_ago']} sessions ago)")
        for c in e.get("likely_cause_filings") or []:
            out.append(f"             cause? {c['form']} {c['filed']} "
                       f"{c.get('items', '')} {c.get('url', '')}")

    out.append("  " + _runway_line(sig.get("runway")))
    dl = sig.get("dilution")
    if dl:
        out.append(f"  dilution: {dl['shares_start']:,.0f} -> {dl['shares_now']:,.0f} "
                   f"({dl['change_pct']:+}%) between {dl['from']} and {dl['to']}")

    fl = sig.get("float") or {}
    if fl.get("unusable"):
        out.append(f"  float: unusable -- {fl['unusable']}")
    elif fl.get("float_fraction") is not None:
        out.append(f"  float: {fl['float_fraction'] * 100:.0f}% of shares out, "
                   f"as of {fl.get('as_of')} ({fl.get('age_days')}d)"
                   + ("  [stale]" if fl.get("stale") else ""))

    sh = sig.get("short") or {}
    if sh:
        tags = [k for k in ("crowded_short", "extreme_short", "heavy_short_selling")
                if sh.get(k)]
        dtc = sh.get("days_to_cover")
        out.append(f"  short: {dtc:.1f} days-to-cover" if dtc is not None
                   else "  short: no settled short interest")
        if sh.get("short_volume_pct_5d") is not None:
            out[-1] += f" | {sh['short_volume_pct_5d']}% of 5d volume short"
        if tags:
            out[-1] += f"  [{', '.join(tags)}]"

    ins = sig.get("insiders") or {}
    if ins.get("cluster_buy") or ins.get("notable_buy") or ins.get("net_selling"):
        tags = [k for k in ("cluster_buy", "notable_buy", "net_selling") if ins.get(k)]
        out.append(f"  insiders: {', '.join(tags).upper()} -- "
                   f"{ins.get('distinct_buyers')} buyer(s), "
                   f"${ins.get('buy_value_usd'):,.0f} bought / "
                   f"${ins.get('sell_value_usd'):,.0f} sold in "
                   f"{ins.get('lookback_days')}d (code P only)")

    tr = sig.get("tradability") or {}
    if tr.get("illiquid") or tr.get("very_illiquid"):
        out.append(f"  liquidity: {'VERY ILLIQUID' if tr.get('very_illiquid') else 'illiquid'}"
                   f" -- median ${tr.get('median_dollar_volume_20d'):,.0f}/d, "
                   f"comfortable ${tr.get('comfortable_position_usd'):,.0f}")

    nc = sig.get("next_catalyst")
    if nc:
        out.append(f"  next trial completion: {nc.get('primary_completion')} "
                   f"{nc.get('nct_id')} {nc.get('phase')} {nc.get('status')} "
                   f"-- {(nc.get('title') or '')[:60]}")

    for f in sig.get("new_filings_since_last_run") or []:
        meanings = "; ".join(f.get("item_meanings") or [])
        out.append(f"  NEW FILING {f['filed']}  {f['form']}  {f.get('items', '')}"
                   + (f"  {meanings}" if meanings else ""))
        out.append(f"             {f['url']}")

    if sig.get("links_md"):
        out.append(f"  {sig['links_md']}")
    return out


# --- size, which is the whole reason this file exists ------------------------
#
# brief.py replaced "read signals.json whole" because that read had grown to
# 487KB, about 122,000 tokens, fifteen times the report it produced. CLAUDE.md
# says plainly what happens next: "It will need moving again: the file grows
# with the watchlist, and nothing in the pipeline notices."
#
# This is the pipeline noticing. The output is measured against a budget and the
# run says so on stderr when it goes over -- stderr, so it lands in the run log
# without being read as part of the brief by whatever is consuming stdout.
#
# The budget is generous on purpose. At 62 names the brief is around 18-25k
# tokens depending on how much is live, so 40,000 is roughly double the current
# cost and still a third of the read it replaced. A bar set just above today's
# figure would fire on an ordinary busy session and be silenced; one set here
# fires when the shape of the problem has come back.
CHARS_PER_TOKEN = 4        # the ratio CLAUDE.md's own 487KB -> 122,000 figure uses
BRIEF_TOKEN_BUDGET = 40_000
# What the read this file replaced actually cost. The budget has to stay well
# under it, or the guard would permit the exact regression it is watching for.
SUPERSEDED_READ_TOKENS = 122_000


def oversize_warning(chars: int, budget: int | None = None) -> str | None:
    """The message to print when the brief has grown past its budget, or None.

    Kept separate from the printing so the threshold can be tested without
    generating a watchlist large enough to trip it.

    `budget` resolves at call time rather than defaulting to the constant in the
    signature: a default argument binds once at import, so the budget would be
    frozen at whatever it was then and could not be overridden by a test, a
    future --budget flag, or a setting. The test that drives run() end to end
    caught exactly that.
    """
    budget = BRIEF_TOKEN_BUDGET if budget is None else budget
    tokens = chars // CHARS_PER_TOKEN
    if tokens <= budget:
        return None
    return (f"[brief] WARNING: this brief is ~{tokens:,} tokens ({chars:,} chars), "
            f"over the {budget:,}-token budget. That is the same shape as the "
            f"122,000-token signals.json read brief.py was written to replace. "
            f"Move material to detail.py, or raise the triage bar in "
            f"prompts/daily.md so fewer names get a full block.")


class _CountingStream:
    """Forwards everything, and remembers how much went through.

    A wrapper rather than a refactor: main() prints as it goes, in about a
    hundred places, and rebuilding it around a list of lines to get a length
    would be a large change to working code for a diagnostic.
    """

    def __init__(self, stream):
        self._stream = stream
        self.chars = 0

    def write(self, s: str) -> int:
        self.chars += len(s)
        return self._stream.write(s)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def run() -> int:
    """main(), with its output measured. This is the entry point."""
    counter = _CountingStream(sys.stdout)
    real, sys.stdout = sys.stdout, counter
    try:
        rc = main()
    finally:
        sys.stdout = real
    warning = oversize_warning(counter.chars)
    if warning:
        print(warning, file=sys.stderr)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="The list-wide view of a run, without reading signals.json whole")
    # One flag for both files, for the reason detail.py has one: the zone and
    # invalidation level are in the snapshot, the tier and vetoes derived from
    # them are in the signals file, and half-setting the pair produces a view
    # that is internally inconsistent while looking entirely normal.
    ap.add_argument("--dataset", default=str(DATA), metavar="DIR",
                    help="directory holding latest.json and signals.json "
                         "(default: data/; the pre-market run writes data/premarket/)")
    ap.add_argument("--all", action="store_true",
                    help="print a block for every name, not only the live ones")
    args = ap.parse_args()

    dataset = Path(args.dataset)
    sigfile = localconfig.require_data(dataset / "signals.json")
    snap = localconfig.require_data(dataset / "latest.json")
    tickers = snap.get("tickers") or {}
    sigs = sigfile.get("signals") or []

    reg = sigfile.get("regime") or {}
    exp = sigfile.get("exposure") or {}
    print(f"=== session {sigfile.get('session_date')} "
          f"(generated {sigfile.get('generated_at')}, data {sigfile.get('data_status')}) ===")
    print(f"regime: {reg.get('label')} -- {reg.get('benchmark')} {reg.get('close')}, "
          f"{_pct(reg.get('pct_vs_sma200'), 'pp')} vs SMA200, 60d {_pct(reg.get('chg_60d_pct'))}"
          + ("   [downtrend: ACT additionally requires a STRONG conviction score]"
             if reg.get("label") == "downtrend" else ""))
    print(f"exposure: {exp.get('actionable_names')} actionable "
          f"({'+'.join(exp.get('tiers_counted') or [])}) would request "
          f"{exp.get('requested_pct_if_all_taken')}% against a "
          f"{exp.get('max_investable_pct')}% ceiling; "
          f"scale every size by {exp.get('scale_factor_needed')}"
          + ("  [OVER-COMMITTED]" if exp.get("over_committed") else ""))
    print(f"settings: {json.dumps(sigfile.get('settings') or {})}")

    counts: dict[str, int] = {}
    for s in sigs:
        counts[s.get("tier") or "NONE"] = counts.get(s.get("tier") or "NONE", 0) + 1
    print("tiers: " + ", ".join(f"{k} {counts[k]}" for k in
                                sorted(counts, key=lambda k: TIER_ORDER.get(k, 9))))

    if sigfile.get("table_markdown"):
        print("\n=== signal table (every name, one row) ===")
        print(sigfile["table_markdown"])

    ranked = sorted(sigs, key=lambda s: (TIER_ORDER.get(s.get("tier"), 9),
                                         s.get("symbol") or ""))
    deep = [s for s in ranked if args.all or triage(s)]
    also = [s for s in ranked if not (args.all or triage(s)) and flagged(s)]
    quiet = [s for s in ranked if not (args.all or triage(s)) and not flagged(s)]

    print(f"\n=== {len(deep)} of {len(sigs)} names in detail "
          f"(tier at WATCH+, a hard veto, a recent event, a new filing, an exit "
          f"flag, or a catalyst inside {TRIAGE_CATALYST_DAYS} days) ===")
    for s in deep:
        print("")
        for line in _block(s, tickers.get(s.get("symbol")) or {}):
            print(line)

    if also:
        print(f"\n=== also flagged: {len(also)} names, one line each ===")
        for s in also:
            print(_one_line(s))

    # Anything in neither list is still named. A view that silently drops two
    # thirds of the watchlist reads as completeness, which is the one thing it is
    # not -- the same lesson screen.py's --limit paid for by hiding 91 names
    # behind the alphabet every single month.
    if quiet:
        print(f"\n=== {len(quiet)} names with nothing to say about them ===")
        print(", ".join(s.get("symbol") for s in quiet))
        print("(the report gives these zero lines; `--all` prints a full block "
              "for every name, `detail.py TICKER` opens one)")

    cals = sorted(
        ((c.get("days_until"), s.get("symbol"), c)
         for s in sigs for c in (s.get("catalysts") or [])
         if c.get("days_until") is not None),
        # Sorted on (days_until, symbol) explicitly. Two catalysts may share a
        # date -- a PDUFA and its AdCom -- and comparing whole tuples would fall
        # through to comparing the dicts, which is what cost signals.py a day's
        # report once already.
        key=lambda row: (row[0], row[1]))
    if cals:
        print("\n=== catalyst calendar (every dated binary, nearest first) ===")
        for days, sym, c in cals:
            urgent = "  << SIZE FOR THE OUTCOME, NOT THE CHART" if 0 <= days <= 21 else ""
            print(f"  {c.get('date')}  {days:+5d}d  {sym:6s} {c.get('kind'):8s} "
                  f"{c.get('confidence'):9s} {c.get('description')} "
                  f"[{c.get('source')}]{urgent}")

    no_thesis = sorted(s.get("symbol") for s in sigs if not s.get("thesis"))
    if no_thesis:
        print(f"\n=== {len(no_thesis)} names with no thesis (bootstrap candidates) ===")
        print(", ".join(no_thesis))
    no_cat = sorted(s.get("symbol") for s in sigs
                    if s.get("bucket") == "A" and not (s.get("catalysts") or []))
    if no_cat:
        print(f"\n=== {len(no_cat)} bucket-A names with no dated catalyst "
              f"(bootstrap candidates) ===")
        print(", ".join(no_cat))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
