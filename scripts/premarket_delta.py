#!/usr/bin/env python3
"""What changed between the desk's record of a session and this morning.

The nightly run at 01:30 is the desk's record of the session that just closed.
By the time the US pre-market opens, things have happened that no bar reflects:
an 8-K filed at 06:40 ET, a priced takedown, a catalyst whose date is today.
This is the deterministic half of the pre-market run -- it compares the
pre-market pass's signals against the nightly ones and says, in arithmetic, what
is different. The analysis pass then explains it and the email carries both.

Deterministic on purpose. The pre-market email pushes to the phone when
something is urgent, and that decision cannot come from a model: `notify.py`
reads `urgent` out of this file. An LLM must never be the source of a price, a
share count or a cash balance, and it must not be the source of "wake him up"
either.

    python3 scripts/premarket_delta.py                       # text to stdout
    python3 scripts/premarket_delta.py --out data/premarket/delta.json

Both sides must describe the SAME session. The pre-market pass runs before any
new bar exists, so it re-derives the previous session -- if the two disagree,
the comparison is between two different days and every difference is spurious,
so it refuses rather than reporting nonsense.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localconfig
from fetch import ITEM_MEANINGS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# A catalyst this close is the dominant fact about a name, whatever the chart
# says -- CLAUDE.md's "size for the outcome, not the chart". Measured from the
# RUN date, not from the session date: see _days_from(), below.
CATALYST_URGENT_DAYS = 1

# 424B7 registers shares existing holders already own, so it is a resale and not
# an issuance. The veto layer excludes it from the 424B hard veto for exactly
# that reason; excluding it here too keeps one answer to "is this dilution?"
# instead of two that can drift apart.
RESALE_FORM = "424B7"

# Severity that counts as urgent in exit_signals()' own vocabulary. `high` is
# invalidation_breached and catalyst_resolved -- a thesis that has broken or a
# binary that has already resolved. `medium` (a live veto) is reported but does
# not push: it is usually a standing condition that was already in last night's
# report, and a nightly buzz about it is how a channel gets muted.
URGENT_EXIT_SEVERITY = "high"


def _days_from(asof: date, iso: str) -> int | None:
    """Whole days from `asof` to an ISO date, or None if it will not parse.

    Deliberately NOT the `days_until` the signals file already carries. That is
    measured from the snapshot's session date, which is the correct clock for
    everything in stage 2 -- and the wrong one here, because the pre-market pass
    re-derives the PREVIOUS session (there is no new bar yet). So a catalyst
    resolving this morning arrives from signals.py as `days_until: 1`, and
    reading that as "tomorrow" would put the single strongest sizing instruction
    the desk emits one day out on the morning it actually matters. The catalyst
    carries its own ISO date; this measures from that against the day the email
    is being sent.
    """
    try:
        return (datetime.strptime(iso, "%Y-%m-%d").date() - asof).days
    except (TypeError, ValueError):
        return None


def by_symbol(sig: dict) -> dict[str, dict]:
    return {s["symbol"]: s for s in sig.get("signals") or []}


def _veto_key(v: dict) -> str:
    """Identity of a veto across two runs.

    Not the whole dict: `days_ago` and the rendered `reason` both move with the
    clock, so comparing dicts would report every standing veto as new every
    morning -- the report would be all noise and the phone would buzz for
    nothing. Form plus filing date is what actually identifies the event.
    """
    return f"{v.get('form', '')}|{v.get('filed', '')}"


def _exit_key(f: dict) -> str:
    """Exit flags carry no date, so `kind` is the identity."""
    return str(f.get("kind", ""))


def _filing_note(f: dict) -> str:
    """One line describing a filing, with its 8-K items spelled out.

    Item meanings are imported from fetch.ITEM_MEANINGS rather than restated,
    so the pre-market email cannot describe item 8.01 differently from the
    nightly report that quotes the same table.
    """
    bits = [f"{f.get('form', '?')} {f.get('filed', '')}"]
    meanings = f.get("item_meanings") or []
    if meanings:
        bits.append("(" + "; ".join(meanings) + ")")
    elif f.get("items"):
        bits.append(f"(items {f['items']})")
    if f.get("doc_desc") and f["doc_desc"] != f.get("form"):
        bits.append(f["doc_desc"])
    return " ".join(bits)


def _filing_is_urgent(f: dict) -> str | None:
    """Why this filing should reach the phone before the open, or None.

    Two forms qualify. An 8-K carrying any item the pipeline already considers
    material -- the whole of ITEM_MEANINGS, which includes 8.01, where trial
    data, CRLs and FDA correspondence land, and which the veto layer has no
    opinion on precisely because it is news rather than a mechanical condition.
    And a 424B other than a resale, which is dilution being priced.

    Everything else is reported without pushing. A Form 4 matters, and the
    insider layer reads it every night; it is not a reason to look at a screen
    at 07:30 ET.
    """
    form = (f.get("form") or "").upper()
    if form.startswith("8-K"):
        codes = [c.strip() for c in (f.get("items") or "").split(",") if c.strip()]
        material = [c for c in codes if c in ITEM_MEANINGS]
        if material:
            return "8-K " + ", ".join(f"{c} ({ITEM_MEANINGS[c]})" for c in material)
    if form.startswith("424B") and form != RESALE_FORM:
        return f"{form} prospectus supplement -- read it before the open"
    return None


def diff_symbol(asof: date, base: dict, cur: dict) -> dict | None:
    """What changed for one name. None when nothing did."""
    out: dict = {
        "symbol": cur["symbol"],
        "company": cur.get("company", ""),
        "close": (cur.get("price") or {}).get("close"),
        "tier": cur.get("tier"),
        "links_md": cur.get("links_md", ""),
        "new_filings": [],
        "new_hard_vetoes": [],
        "cleared_hard_vetoes": [],
        "new_soft_flags": [],
        "new_exit_flags": [],
        "imminent_catalysts": [],
        "tier_change": None,
        "urgent_because": [],
    }

    # Filings the pre-market fetch saw and the nightly one had not. This is only
    # meaningful because the pre-market fetch runs --no-persist: it reads the
    # filings table without spending it, so this list is genuinely "since the
    # last run that recorded", and the nightly report still gets to mention them.
    out["new_filings"] = list(cur.get("new_filings_since_last_run") or [])

    base_v = {_veto_key(v) for v in (base.get("hard_vetoes") or [])}
    for v in cur.get("hard_vetoes") or []:
        if _veto_key(v) not in base_v:
            out["new_hard_vetoes"].append(v)
    cur_v = {_veto_key(v) for v in (cur.get("hard_vetoes") or [])}
    for v in base.get("hard_vetoes") or []:
        if _veto_key(v) not in cur_v:
            out["cleared_hard_vetoes"].append(v)

    base_s = {_veto_key(f) for f in (base.get("soft_flags") or [])}
    for f in cur.get("soft_flags") or []:
        if _veto_key(f) not in base_s:
            out["new_soft_flags"].append(f)

    base_e = {_exit_key(f) for f in (base.get("exit_flags") or [])}
    for f in cur.get("exit_flags") or []:
        if _exit_key(f) not in base_e:
            out["new_exit_flags"].append(f)

    if base.get("tier") != cur.get("tier"):
        out["tier_change"] = {"from": base.get("tier"), "to": cur.get("tier")}

    # Measured from the run date, not from the session's `days_until`.
    for c in cur.get("catalysts") or []:
        d = _days_from(asof, c.get("date", ""))
        if d is not None and 0 <= d <= CATALYST_URGENT_DAYS:
            out["imminent_catalysts"].append(dict(c, days_from_today=d))

    for f in out["new_filings"]:
        why = _filing_is_urgent(f)
        if why:
            out["urgent_because"].append(why)
    for v in out["new_hard_vetoes"]:
        out["urgent_because"].append(f"new hard veto: {v.get('reason', '')}")
    for f in out["new_exit_flags"]:
        if f.get("severity") == URGENT_EXIT_SEVERITY:
            out["urgent_because"].append(f"{f.get('kind')}: {f.get('detail', '')}")
    for c in out["imminent_catalysts"]:
        when = "TODAY" if c["days_from_today"] == 0 else f"in {c['days_from_today']}d"
        out["urgent_because"].append(
            f"catalyst {when}: {c.get('kind')} {c.get('date')} "
            f"({c.get('confidence')}) {c.get('description', '')[:120]}")

    changed = any(out[k] for k in (
        "new_filings", "new_hard_vetoes", "cleared_hard_vetoes", "new_soft_flags",
        "new_exit_flags", "imminent_catalysts")) or out["tier_change"]
    return out if changed else None


def build(asof: date, baseline: dict, current: dict) -> dict:
    base_by = by_symbol(baseline)
    cur_by = by_symbol(current)

    changes = []
    for sym in sorted(cur_by):
        d = diff_symbol(asof, base_by.get(sym, {}), cur_by[sym])
        if d:
            changes.append(d)

    # A name that vanished from the watchlist between the two runs is a hand
    # edit, not an event, but silence about it would be wrong: the pre-market
    # pass covers the whole watchlist and a reader counting names would come up
    # short with nothing saying why.
    dropped = sorted(set(base_by) - set(cur_by))
    added = sorted(set(cur_by) - set(base_by))

    urgent = [c for c in changes if c["urgent_because"]]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "asof": asof.isoformat(),
        "baseline_session": baseline.get("session_date"),
        "baseline_generated_at": baseline.get("generated_at"),
        "current_session": current.get("session_date"),
        "regime": current.get("regime"),
        "watchlist_added": added,
        "watchlist_dropped": dropped,
        "urgent": urgent,
        "changes": changes,
        "counts": {
            "names": len(cur_by),
            "changed": len(changes),
            "urgent": len(urgent),
            "new_filings": sum(len(c["new_filings"]) for c in changes),
            "new_hard_vetoes": sum(len(c["new_hard_vetoes"]) for c in changes),
        },
    }


def render(delta: dict) -> str:
    c = delta["counts"]
    lines = [
        f"PRE-MARKET DELTA — {delta['asof']}",
        f"against the {delta['baseline_session']} session, recorded "
        f"{delta['baseline_generated_at']}",
        f"{c['changed']} of {c['names']} names changed; {c['urgent']} urgent; "
        f"{c['new_filings']} new filings; {c['new_hard_vetoes']} new hard vetoes",
        "",
    ]

    if delta["urgent"]:
        lines.append(f"URGENT ({len(delta['urgent'])}) — before the open")
        for ch in delta["urgent"]:
            lines.append(f"  {ch['symbol']:6} ${ch['close']}  [{ch['tier'] or 'NONE'}]")
            for why in ch["urgent_because"]:
                lines.append(f"         {why}")
        lines.append("")

    quiet = [ch for ch in delta["changes"] if not ch["urgent_because"]]
    if quiet:
        lines.append(f"ALSO CHANGED ({len(quiet)}) — no action implied")
        for ch in quiet:
            bits = []
            if ch["tier_change"]:
                bits.append(f"tier {ch['tier_change']['from']}->{ch['tier_change']['to']}")
            for f in ch["new_filings"]:
                bits.append(_filing_note(f))
            for f in ch["new_soft_flags"]:
                bits.append(f"soft: {f.get('reason', '')}")
            for f in ch["new_exit_flags"]:
                bits.append(f"exit {f.get('kind')} ({f.get('severity')})")
            for v in ch["cleared_hard_vetoes"]:
                bits.append(f"veto cleared: {v.get('form')} {v.get('filed')}")
            lines.append(f"  {ch['symbol']:6} " + "; ".join(bits))
        lines.append("")

    if delta["watchlist_added"]:
        lines.append(f"added to the watchlist since: {', '.join(delta['watchlist_added'])}")
    if delta["watchlist_dropped"]:
        lines.append(f"no longer on the watchlist: {', '.join(delta['watchlist_dropped'])}")

    if not delta["changes"]:
        lines.append("Nothing has changed since the nightly run. No filings, no new "
                     "vetoes, no catalyst inside "
                     f"{CATALYST_URGENT_DAYS + 1} day(s).")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Diff the pre-market signals against the nightly ones")
    ap.add_argument("--baseline", default=str(DATA / "signals.json"),
                    help="the nightly run's signals (the desk's record)")
    ap.add_argument("--current", default=str(DATA / "premarket" / "signals.json"),
                    help="the pre-market pass's signals")
    ap.add_argument("--out", default=None, help="write the delta as JSON here")
    # Explicit and recorded in the output. Unlike stage 2 this genuinely needs a
    # wall clock -- "should I look at this before the open today" is a question
    # about today -- but a clock read silently is how a days_until ends up
    # measured from the wrong day with nothing in the output to say so.
    ap.add_argument("--asof", default=date.today().isoformat(),
                    help="the date the email is being sent (default: today)")
    args = ap.parse_args()

    try:
        asof = datetime.strptime(args.asof, "%Y-%m-%d").date()
    except ValueError:
        print(f"--asof {args.asof!r} is not an ISO date", file=sys.stderr)
        return 2

    baseline = localconfig.require_data(Path(args.baseline))
    current = localconfig.require_data(Path(args.current))

    # Refuse rather than compare two different days. The pre-market pass runs
    # before any new bar exists, so it must re-derive the same session the
    # nightly run analysed; if it did not, either a bar landed mid-morning or
    # one of the two files is from another day, and every "change" below would
    # be an artefact of the mismatch rather than something that happened.
    if baseline.get("session_date") != current.get("session_date"):
        print(f"session mismatch: baseline is {baseline.get('session_date')}, "
              f"pre-market pass is {current.get('session_date')} -- these describe "
              f"different days and the diff would be meaningless", file=sys.stderr)
        return 1

    delta = build(asof, baseline, current)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(delta, indent=2))
    print(render(delta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
