#!/usr/bin/env python3
"""Mechanical signal computation + the veto layer.

Split from fetch.py on purpose: this file turns facts into flags using fixed
arithmetic, so the same input always produces the same tier. Nothing here is a
judgement call -- judgement happens downstream, in the report, where it can be
argued with.

The veto layer is the important part. "Cheap" is easy to compute and dangerous
on its own: in micro-cap biotech, most large drawdowns are correctly pricing a
broken thesis. A price in the bottom decile sitting on top of a priced offering
or a listing-deficiency notice is not an opportunity, and this file refuses to
label it one.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import sys
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localconfig

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = ROOT / "state"

# form prefix -> (days the signal stays hot, human reason)
HARD_VETO_FORMS = {
    # NB: a 424B5 is sometimes an at-the-market *programme* (registered capacity,
    # no discounted pricing) rather than a priced takedown. The two are not
    # equivalent and the form type alone cannot distinguish them, so this stays a
    # hard veto and the report must open the document to refute it.
    "424B": (10, "prospectus supplement -- likely active dilution; READ IT: a priced "
                 "takedown is a hard stop, an ATM programme is only capacity"),
    "NT 10-Q": (45, "late quarterly filing"),
    "NT 10-K": (45, "late annual filing"),
}
HARD_VETO_ITEMS = {
    "3.01": (45, "listing rule deficiency / delisting notice"),
    "3.02": (10, "unregistered sale of equity (dilution)"),
    "4.02": (90, "previously issued financials not to be relied upon"),
    "4.01": (60, "change of certifying accountant"),
}
SOFT_FLAG_FORMS = {
    "S-3": (30, "shelf registration -- future dilution capacity"),
    "S-1": (30, "registration statement -- future dilution capacity"),
    "424B7": (20, "resale prospectus"),
}
SOFT_FLAG_ITEMS = {
    "5.02": (30, "officer/director departure or appointment"),
    "5.03": (60, "charter amendment (reverse split mechanics live here)"),
    "1.01": (10, "material definitive agreement"),
    "2.03": (10, "new direct financial obligation (debt)"),
}
ACTIVE_TRIAL = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING"}

# Thresholds below are measured, not chosen by feel. `scripts/backtest.py` walks
# the stored history (29,077 ticker-days) and scores each rule's forward return
# against an all-days baseline. Re-run it before changing any of these.
#
#   RSI<30 & %B<0.05   +3.63pp median edge at 20 sessions, but only +0.26pp at 60
#   RSI<35 & %B<0.15   +2.90pp at 20 sessions and fires 3.6x more often
#   bottom decile      +0.49pp at 20 and -2.68pp at 60 -- effectively noise
#   RSI<25             -9.59pp at 60 sessions. More oversold is NOT better.
#   volume >1.5x avg   the only variant that still has edge at 60 sessions
SETUP_RSI, SETUP_PCTB = 35.0, 0.15
WATCH_RSI, WATCH_PCTB = 40.0, 0.25
CAPITULATION_VOL = 1.5
RSI_TRAP = 22.0
# Edge decays to nothing by 60 sessions: this is a bounce signal, not a hold.
SIGNAL_HORIZON_SESSIONS = 20
# Equal-weighted small/mid-cap biotech -- the honest comparison for this list.
BENCHMARK = "XBI"
# Price this far from its entry zone means the zone predates the current regime.
ZONE_STALE_DRIFT_PCT = 25.0


# ------------------------------------------------------------------ indicators


def sma(xs: list[float], n: int):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi(xs: list[float], n: int = 14):
    """Wilder-smoothed RSI."""
    if len(xs) < n + 1:
        return None
    deltas = [b - a for a, b in itertools.pairwise(xs)]
    seed = deltas[:n]
    ag = sum(d for d in seed if d > 0) / n
    al = sum(-d for d in seed if d < 0) / n
    for d in deltas[n:]:  # Wilder smoothing forward through the rest of the series
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def stdev(xs: list[float]):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def bollinger_pct_b(xs: list[float], n: int = 20, k: float = 2.0):
    """0 = at lower band, 1 = at upper band. Below 0 means outside the band."""
    if len(xs) < n:
        return None, None, None
    window = xs[-n:]
    mid = sum(window) / n
    sd = stdev(window)
    if not sd:
        return None, None, None
    upper, lower = mid + k * sd, mid - k * sd
    if upper == lower:
        return None, upper, lower
    return (xs[-1] - lower) / (upper - lower), upper, lower


def atr(bars: list[dict], n: int = 14):
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-(n + 1) : -1], bars[-n:], strict=False):
        hi, lo, pc = cur.get("high"), cur.get("low"), prev.get("close")
        if None in (hi, lo, pc):
            continue
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    return sum(trs) / len(trs) if trs else None


def pct_change(xs: list[float], n: int):
    if len(xs) <= n or xs[-(n + 1)] in (None, 0):
        return None
    return (xs[-1] / xs[-(n + 1)] - 1.0) * 100.0


def percentile_rank(xs: list[float], v: float):
    """Where the current price sits in its own history, 0 = cheapest ever seen."""
    if not xs:
        return None
    return 100.0 * sum(1 for x in xs if x <= v) / len(xs)


# ---------------------------------------------------------------- veto layer


def _days_between(a: str, b: str) -> int:
    """Days from a to b; -1 if either date is unusable."""
    try:
        return (datetime.strptime(b, "%Y-%m-%d").date()
                - datetime.strptime(a, "%Y-%m-%d").date()).days
    except (ValueError, TypeError):
        return -1


def _days_ago(d: str) -> int:
    try:
        return (date.today() - datetime.strptime(d, "%Y-%m-%d").date()).days
    except (ValueError, TypeError):
        return 10**6


def evaluate_filings(filings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split recent filings into hard vetoes and soft flags."""
    hard, soft = [], []
    for f in filings or []:
        age = _days_ago(f.get("filed", ""))
        form = (f.get("form") or "").upper()
        items = [c.strip() for c in (f.get("items") or "").split(",") if c.strip()]

        for prefix, (window, reason) in HARD_VETO_FORMS.items():
            if form.startswith(prefix) and age <= window:
                # fetch.py reads the document and tells us which kind it is. An
                # ATM programme is registered capacity and belongs with the
                # shelf registrations; only a priced takedown is a hard stop.
                if prefix == "424B" and f.get("offering_type") == "atm":
                    soft.append({
                        "form": form, "filed": f["filed"], "days_ago": age,
                        "reason": "at-the-market programme (read from the filing) -- "
                                  "registered capacity, not a priced discount; track usage",
                        "url": f.get("url")})
                    continue
                detail = f.get("offering_type")
                hard.append({"form": form, "filed": f["filed"], "days_ago": age,
                             "reason": (reason if detail in (None, "unknown")
                                        else "priced takedown confirmed from the filing "
                                             "-- active dilution at a discount"),
                             "url": f.get("url")})
        for code, (window, reason) in HARD_VETO_ITEMS.items():
            if code in items and age <= window:
                hard.append({"form": f"8-K item {code}", "filed": f["filed"], "days_ago": age,
                             "reason": reason, "url": f.get("url")})
        for prefix, (window, reason) in SOFT_FLAG_FORMS.items():
            if form.startswith(prefix) and age <= window:
                soft.append({"form": form, "filed": f["filed"], "days_ago": age,
                             "reason": reason, "url": f.get("url")})
        for code, (window, reason) in SOFT_FLAG_ITEMS.items():
            if code in items and age <= window:
                soft.append({"form": f"8-K item {code}", "filed": f["filed"], "days_ago": age,
                             "reason": reason, "url": f.get("url")})
    return hard, soft


def crash_scan(bars: list[dict], closes: list[float], filings: list[dict],
               lookback: int = 10, crash_pct: float = -25.0, spike_pct: float = 40.0):
    """Find violent recent moves and the filing that most likely explains them.

    This exists because oversold indicators are at their most seductive exactly
    when they are most wrong. A stock that fell 91% in a session is not "due for
    a bounce" -- something happened, and RSI has no idea what. Any collapse in
    the recent window is treated as a hard veto until a human has read the news.
    """
    events = []
    start = max(1, len(closes) - lookback)
    for i in range(start, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if not prev:
            continue
        chg = (cur / prev - 1.0) * 100.0
        if chg > crash_pct and chg < spike_pct:
            continue
        d = bars[i]["date"]
        # An 8-K filed within a couple of days of the move is almost always the
        # cause; surfacing it saves the analyst from guessing.
        nearby = [
            {"form": f["form"], "filed": f["filed"], "items": f.get("items", ""),
             "meanings": f.get("item_meanings", []), "url": f.get("url")}
            for f in filings or []
            if f.get("filed") and abs((datetime.strptime(f["filed"], "%Y-%m-%d").date()
                                       - datetime.strptime(d, "%Y-%m-%d").date()).days) <= 2
        ]
        events.append({
            "date": d,
            "change_pct": round(chg, 1),
            "kind": "collapse" if chg <= crash_pct else "spike",
            "volume": bars[i].get("volume"),
            "sessions_ago": len(closes) - 1 - i,
            "likely_cause_filings": nearby[:4],
        })
    return events


def chart_links(symbol: str, cik: str | None = None) -> dict:
    """One-click destinations for eyeballing a name.

    TradingView for drawing on the chart, Finviz for a fast chart-plus-stats
    glance, StockAnalysis for financials, and EDGAR for the filing history the
    veto layer is reasoning about.
    """
    # Canonical URLs, verified to return 200 without a redirect hop -- some mail
    # clients handle redirects poorly, and finviz moved quote.ashx -> /stock.
    # Finviz p= controls the candle interval, which is how you get period views:
    # d ~= 6 months daily, w ~= 3 years weekly, m ~= 10 years monthly.
    links = {
        "chart_6m": f"https://finviz.com/stock?t={symbol}&p=d",
        "chart_3y": f"https://finviz.com/stock?t={symbol}&p=w",
        "chart_10y": f"https://finviz.com/stock?t={symbol}&p=m",
        "tradingview": f"https://www.tradingview.com/chart/?symbol={symbol}&interval=D",
        "finviz": f"https://finviz.com/stock?t={symbol}&p=d",
        "stockanalysis": f"https://stockanalysis.com/stocks/{symbol.lower()}/",
    }
    if cik:
        links["sec_filings"] = (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik}&type=&dateb=&owner=include&count=40"
        )
    return links


def links_markdown(symbol: str, links: dict) -> str:
    """Compact one-liner: chart periods first, then reference material."""
    parts = [
        f"Chart: [6M]({links['chart_6m']})",
        f"[3Y]({links['chart_3y']})",
        f"[10Y]({links['chart_10y']})",
        f"[Interactive]({links['tradingview']})",
        f"· [Financials]({links['stockanalysis']})",
    ]
    if links.get("sec_filings"):
        parts.append(f"[EDGAR]({links['sec_filings']})")
    return " · ".join(parts).replace("· ·", "·")


def relative_strength(bars: list[dict], bench_bars: list[dict], horizons=(5, 20, 60)):
    """Return vs the sector benchmark over each horizon, date-aligned.

    Without this, one sector drawdown flags all 60+ names at once and reads as
    60 signals when it is really one. Aligning on dates matters: a halted
    session in the name but not the ETF would otherwise shift the comparison by
    a day and quietly corrupt every number.
    """
    if not bars or not bench_bars:
        return None
    bench = {b["date"]: b["adjclose"] for b in bench_bars if b.get("adjclose") is not None}
    pairs = [(b["date"], b["adjclose"], bench[b["date"]])
             for b in bars if b.get("adjclose") is not None and b["date"] in bench]
    if len(pairs) < max(horizons) + 1:
        return None

    out = {}
    for h in horizons:
        _, p_now, b_now = pairs[-1]
        _, p_then, b_then = pairs[-(h + 1)]
        if not p_then or not b_then:
            continue
        stock = (p_now / p_then - 1.0) * 100.0
        market = (b_now / b_then - 1.0) * 100.0
        out[f"{h}d"] = {
            "stock_pct": round(stock, 2),
            "benchmark_pct": round(market, 2),
            "excess_pct": round(stock - market, 2),
        }
    if not out:
        return None

    # A name falling while the sector holds up is company-specific and demands an
    # explanation; a name falling with the sector is mostly beta.
    ref = out.get("20d") or next(iter(out.values()))
    out["idiosyncratic"] = bool(ref["excess_pct"] <= -10.0 and ref["benchmark_pct"] > -5.0)
    out["sector_wide"] = bool(ref["benchmark_pct"] <= -5.0)
    return out


def load_catalysts(path: Path) -> dict:
    """symbol -> catalysts sorted by date. Missing file is fine, not an error."""
    if not path.exists():
        return {}
    try:
        raw = tomllib.loads(path.read_text()).get("catalyst", [])
    except (tomllib.TOMLDecodeError, OSError) as e:
        print(f"[signals] WARNING: could not read {path}: {e}", file=sys.stderr)
        return {}
    out: dict = {}
    today = date.today().isoformat()
    for c in raw:
        sym = (c.get("symbol") or "").upper()
        d = c.get("date") or ""
        if not sym or d < today:
            continue  # past catalysts are history, not a clock
        entry = dict(c)
        entry["days_until"] = _days_between(today, d)
        out.setdefault(sym, []).append(entry)
    for sym in out:
        out[sym].sort(key=lambda c: c["date"])
    return out


def resolved_catalysts(path: Path, window_days: int = 21) -> dict:
    """Catalysts whose date has just passed -- the trigger to re-underwrite.

    Without this the desk silently keeps carrying a thesis whose binary has
    already resolved: on 23 August it would have no idea CAPR's PDUFA happened.
    """
    if not path.exists():
        return {}
    try:
        raw = tomllib.loads(path.read_text()).get("catalyst", [])
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    out: dict = {}
    for c in raw:
        sym, d = (c.get("symbol") or "").upper(), c.get("date") or ""
        if sym and cutoff <= d < today:
            entry = dict(c)
            entry["days_ago"] = _days_between(d, today)
            out.setdefault(sym, []).append(entry)
    return out


def exit_signals(rec: dict, out: dict, last: float, hard: list,
                 resolved: list, last_alert: dict | None) -> list:
    """Reasons to reduce or exit, as opposed to reasons to buy.

    The desk was entirely buy-side until this existed, which left the harder
    half of the job unanswered. None of these are automatic sells -- they are
    prompts to re-underwrite, which is what a thesis change actually demands.
    """
    flags = []

    inval = rec.get("invalidation_price") or 0
    if inval and last <= inval:
        flags.append({
            "kind": "invalidation_breached",
            "severity": "high",
            "detail": (f"price ${last} is at or below the invalidation level ${inval} "
                       f"declared in watchlist.toml: {rec.get('invalidation','')[:110]}"),
        })

    for c in resolved:
        flags.append({
            "kind": "catalyst_resolved",
            "severity": "high",
            "detail": (f"{c.get('kind','catalyst')} on {c['date']} passed {c['days_ago']} days "
                       f"ago ({c.get('description','')[:80]}) -- the binary has resolved, so the "
                       f"old thesis no longer applies. Re-underwrite or archive it."),
        })

    if hard:
        flags.append({
            "kind": "veto_active",
            "severity": "medium",
            "detail": (f"{len(hard)} hard veto(es) now active: "
                       + "; ".join(h["reason"][:60] for h in hard[:2])
                       + " -- if this name is held, the reason for holding has changed"),
        })

    # The measured edge decays to nothing by 60 sessions, so a signal that has
    # not worked within its own horizon is finished, not merely early.
    if last_alert and last_alert.get("sessions_ago") is not None:
        n = last_alert["sessions_ago"]
        if n >= SIGNAL_HORIZON_SESSIONS:
            ret = last_alert.get("return_pct")
            flags.append({
                "kind": "horizon_elapsed",
                "severity": "low",
                "detail": (f"the alert on {last_alert['date']} is {n} sessions old, past the "
                           f"{SIGNAL_HORIZON_SESSIONS}-session horizon where the edge was measured"
                           + (f" (price {ret:+.1f}% since)" if ret is not None else "")
                           + ". A technical thesis this old has expired; only a catalyst justifies holding."),
            })
    return flags


def load_last_alerts(db: Path) -> dict:
    """Most recent live alert per ticker, for measuring signal age."""
    if not db.exists():
        return {}
    try:
        con = sqlite3.connect(db)
        rows = con.execute(
            "SELECT ticker, MAX(session_date), close FROM alerts "
            "WHERE source='live' GROUP BY ticker").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    return {t: {"date": d, "close": c} for t, d, c in rows if d}


def market_regime(bench_bars: list[dict]):
    """Is the sector itself rising or falling?

    Dip-buying into a rising sector and into a falling one are different bets
    with different odds, and treating them identically is how a strategy that
    worked in one regime quietly stops working in the next.
    """
    closes = [b["adjclose"] for b in bench_bars if b.get("adjclose") is not None]
    if len(closes) < 210:
        return None
    last, s50, s200 = closes[-1], sma(closes, 50), sma(closes, 200)
    prior200 = sma(closes[:-21], 200) if len(closes) > 221 else None
    rising = bool(prior200 and s200 and s200 > prior200)
    if last > s200 and rising:
        label = "uptrend"
    elif last < s200:
        label = "downtrend"
    else:
        label = "mixed"
    return {
        "benchmark": BENCHMARK,
        "close": round(last, 2),
        "sma50": round(s50, 2) if s50 else None,
        "sma200": round(s200, 2) if s200 else None,
        "pct_vs_sma200": round((last / s200 - 1.0) * 100.0, 2) if s200 else None,
        "chg_60d_pct": round(pct_change(closes, 60), 2) if len(closes) > 60 else None,
        "label": label,
    }


def float_metrics(fin: dict, bars: list[dict], short_interest: list[dict]):
    """Short interest as a percentage of actual float.

    Days-to-cover uses shares outstanding, which includes insider and fund
    holdings that will never be sold to a short. Float is what shorts must
    actually buy back, so percent-of-float is the sharper squeeze measure.

    The float figure comes from the 10-K cover and is up to ~14 months old; any
    dilution since then means the true float is larger and this number is an
    overestimate. Flagged as stale rather than silently trusted.
    """
    pf = (fin or {}).get("public_float") or {}
    if not pf.get("value_usd") or not pf.get("as_of"):
        return None
    # Price on (or nearest before) the float measurement date turns USD into shares.
    px = None
    for b in bars:
        if b.get("date") and b["date"] <= pf["as_of"] and b.get("adjclose"):
            px = b["adjclose"]
        elif b.get("date", "") > pf["as_of"]:
            break
    if not px:
        return None
    float_then = pf["value_usd"] / px
    out = {
        "public_float_usd": pf["value_usd"],
        "as_of": pf["as_of"],
        "age_days": pf.get("age_days"),
        "price_then": round(px, 4),
        "stale": bool((pf.get("age_days") or 0) > 400),
    }

    # Float is carried forward as a *fraction* of shares outstanding, not as an
    # absolute share count. These companies dilute violently -- XFOR went from
    # 5.8M to 99.1M shares in a year -- so a year-old absolute float compared
    # against today's short interest produces nonsense (XFOR read as 93% of
    # float short). The fraction is the part that stays roughly stable.
    hist = (fin or {}).get("share_history") or []
    prior = [h for h in hist if h.get("filed") and h["filed"] <= pf["as_of"] and h.get("shares")]
    shares_then = prior[-1]["shares"] if prior else None
    shares_now = ((fin or {}).get("shares") or {}).get("value")
    if not shares_then or not shares_now:
        out["unusable"] = "no share count to anchor the float against"
        return out

    ratio = float_then / shares_then
    # A float below 5% or above 100% of shares outstanding means the inputs
    # disagree -- a filer error (PTCT reports a $3.35M float on 83M shares) or a
    # reverse split that shifted prices without shifting the recorded count.
    if not 0.05 <= ratio <= 1.0:
        out["unusable"] = (f"derived float is {ratio:.0%} of shares outstanding, "
                           f"which is not credible; filer data or a split is inconsistent")
        return out

    float_now = ratio * shares_now
    out["float_fraction"] = round(ratio, 3)
    out["float_shares_est"] = round(float_now)
    out["dilution_adjusted"] = bool(abs(shares_now / shares_then - 1.0) > 0.05)

    if short_interest:
        si = short_interest[-1].get("short_interest")
        if si and float_now > 0:
            pct = 100.0 * si / float_now
            if pct > 100:
                out["unusable"] = "short interest exceeds estimated float; inputs inconsistent"
                return out
            out["short_pct_of_float"] = round(pct, 1)
            out["heavily_shorted_float"] = bool(pct >= 20.0)
    return out


def move_profile(bars: list[dict], bench_bars: list[dict]):
    """Characterise today's move against the stock's OWN normal daily range.

    A raw percentage is the wrong yardstick across this watchlist: 10% is a
    quiet session for a $0.55 micro-cap and an earthquake for MDGL at $507. The
    move is therefore expressed in standard deviations of that name's own recent
    daily returns, which makes "big" mean the same thing for every ticker.
    """
    closes = [b["adjclose"] for b in bars if b.get("adjclose") is not None]
    if len(closes) < 25:
        return None
    rets = [(closes[i] / closes[i - 1] - 1.0) * 100.0
            for i in range(len(closes) - 21, len(closes)) if closes[i - 1]]
    if len(rets) < 10:
        return None
    today = rets[-1]
    sd = stdev(rets[:-1])  # exclude today so a huge move does not inflate its own yardstick
    z = (today / sd) if sd else None

    prev_close, bar = closes[-2], bars[-1]
    gap = None
    if bar.get("open") and prev_close:
        gap = (bar["open"] / prev_close - 1.0) * 100.0
    intraday = None
    if bar.get("high") and bar.get("low") and bar["low"]:
        intraday = (bar["high"] / bar["low"] - 1.0) * 100.0

    bench_1d = None
    if bench_bars:
        bc = [b["adjclose"] for b in bench_bars if b.get("adjclose") is not None]
        if len(bc) >= 2 and bc[-2]:
            bench_1d = (bc[-1] / bc[-2] - 1.0) * 100.0

    out = {
        "chg_1d_pct": round(today, 2),
        "sigma": round(z, 2) if z is not None else None,
        "typical_daily_move_pct": round(sd, 2) if sd else None,
        "gap_pct": round(gap, 2) if gap is not None else None,
        "intraday_range_pct": round(intraday, 2) if intraday is not None else None,
        "benchmark_1d_pct": round(bench_1d, 2) if bench_1d is not None else None,
        "excess_1d_pct": round(today - bench_1d, 2) if bench_1d is not None else None,
    }
    # Big = unusual for this name, or simply large in absolute terms. The
    # absolute floor matters: a near-flat series has almost no measured
    # volatility, so an ordinary 0.1% drift divides out to a huge sigma. A move
    # that small is never news regardless of how quiet the name normally is.
    MIN_MEANINGFUL_MOVE_PCT = 3.0
    out["big_move"] = bool(
        (z is not None and abs(z) >= 2.0 and abs(today) >= MIN_MEANINGFUL_MOVE_PCT)
        or abs(today) >= 10.0
    )
    out["direction"] = "up" if today > 0 else "down"
    return out


def tradability(bars: list[dict], settings: dict):
    """Can a real position actually be built and exited here?

    A perfect signal in a stock trading $80k a day is not actionable at a 28%
    position size -- you would be the market. This is the "don't buy" filter that
    pure price analysis never surfaces, and it matters most in exactly the
    micro-caps where the best-looking signals appear.
    """
    rows = [b for b in bars if b.get("volume") and b.get("close")][-20:]
    if len(rows) < 10:
        return None
    dollar = sorted(b["close"] * b["volume"] for b in rows)
    median_adv = dollar[len(dollar) // 2]
    # Taking more than ~10% of a day's volume moves the price against you.
    comfortable = median_adv * 0.10
    return {
        "median_dollar_volume_20d": round(median_adv),
        "comfortable_position_usd": round(comfortable),
        "illiquid": bool(median_adv < 500_000),
        "very_illiquid": bool(median_adv < 100_000),
    }


def conviction(out: dict, hard: list, soft: list, catalyst_soon: bool) -> dict:
    """Count independent lines of evidence for and against.

    This is a transparent checklist, NOT a validated model -- the components are
    listed so any one of them can be disputed. Its value is that it refuses to
    let a single indicator carry a decision: agreement across price, volume,
    insiders, the balance sheet and the filing record is what justifies trust.
    """
    plus, minus = [], []

    if out.get("capitulation_volume"):
        plus.append("capitulation volume (the only variant with edge past 20 sessions)")
    ins = out.get("insiders") or {}
    if ins.get("cluster_buy"):
        plus.append(f"{ins['distinct_buyers']} insiders bought ${ins['buy_value_usd']:,.0f}")
    elif ins.get("notable_buy"):
        plus.append(f"insider bought ${ins['buy_value_usd']:,.0f}")
    rw = out.get("runway") or {}
    if rw.get("quarters", 0) >= 4 and not rw.get("stale") and not rw.get("superseded_by"):
        plus.append(f"{rw['quarters']}q of liquidity, no near-term financing pressure")
    if catalyst_soon:
        plus.append("catalyst inside 90 days")
    rs = out.get("relative_strength") or {}
    if rs.get("sector_wide"):
        plus.append("weakness is sector-wide, not company-specific")
    sh = out.get("short") or {}
    if sh.get("extreme_short") and catalyst_soon:
        plus.append(f"{sh['days_to_cover']:.0f} days to cover into a catalyst (squeeze fuel)")

    for h in hard:
        minus.append(f"HARD VETO: {h['reason'][:80]}")
    if rs.get("idiosyncratic"):
        minus.append("falling while the sector rises -- company-specific repudiation")
    if ins.get("net_selling"):
        minus.append(f"insiders sold ${ins['sell_value_usd']:,.0f}")
    tr = out.get("tradability") or {}
    if tr.get("very_illiquid"):
        minus.append(f"very illiquid: ~${tr['median_dollar_volume_20d']:,.0f}/day median volume")
    elif tr.get("illiquid"):
        minus.append(f"thin: ~${tr['median_dollar_volume_20d']:,.0f}/day median volume")
    t = out.get("technicals") or {}
    if t.get("rsi14") is not None and t["rsi14"] < RSI_TRAP:
        minus.append(f"RSI {t['rsi14']} -- the distress bucket, not the discount bucket")
    if rw.get("stale") or rw.get("superseded_by"):
        minus.append("balance sheet is stale or superseded; runway unverified")

    score = len(plus) - 2 * len([m for m in minus if m.startswith("HARD VETO")]) - len(
        [m for m in minus if not m.startswith("HARD VETO")])
    if score >= 3:
        label = "strong"
    elif score >= 1:
        label = "moderate"
    elif score >= -1:
        label = "weak"
    else:
        label = "avoid"
    return {"score": score, "label": label, "supporting": plus, "against": minus}


def insider_signal(ins: dict, price: float | None):
    """Summarise Form 4 activity into something decision-shaped.

    Only open-market purchases (code P) reached here -- grants and option
    exercises are filtered upstream. A cluster of insiders buying their own
    stock is one of the few genuinely *informational* signals available free,
    and it is the natural counterweight to the veto layer: people with the
    fullest picture putting their own money in while the tape says broken.
    """
    if not ins:
        return None
    buyers, buy_v, sell_v = ins["distinct_buyers"], ins["buy_value_usd"], ins["sell_value_usd"]
    out = {
        "distinct_buyers": buyers,
        "buy_value_usd": round(buy_v),
        "sell_value_usd": round(sell_v),
        "net_value_usd": round(ins["net_value_usd"]),
        "lookback_days": ins["lookback_days"],
        "recent_buys": ins["buys"][:5],
        "cluster_buy": bool(buyers >= 2 and buy_v >= 50_000),
        "notable_buy": bool(buyers >= 1 and buy_v >= 100_000),
        # Buys and sells are wildly asymmetric in information content. An
        # insider buys for one reason; they sell for a dozen (10b5-1 plans, RSU
        # vesting, tax, diversification). Selling fired on 34 of 63 names at a
        # $250k bar -- wallpaper, not signal -- so the threshold is deliberately
        # severe and it is still reported as context rather than a flag.
        "net_selling": bool(sell_v >= 25_000_000 and sell_v > buy_v * 10),
    }
    return out


def short_signal(short_interest: list[dict], short_volume: list[dict]):
    """Squeeze fuel: settled short interest plus daily short-volume pressure."""
    out = {}
    if short_interest:
        latest = short_interest[-1]
        out["settlement_date"] = latest["settlement_date"]
        out["short_interest"] = latest["short_interest"]
        out["days_to_cover"] = latest["days_to_cover"]
        if len(short_interest) >= 2 and short_interest[-2]["short_interest"]:
            prev = short_interest[-2]["short_interest"]
            out["short_interest_change_pct"] = round(
                (latest["short_interest"] / prev - 1.0) * 100.0, 1
            )
        # Days-to-cover is the squeeze metric: it is how many normal sessions of
        # buying it would take shorts to get out, so a catalyst is the fuse.
        # Days-to-cover above 5 describes most of small-cap biotech (47 of 63
        # names here), so it discriminates nothing. The bar has to sit where the
        # distribution actually thins out.
        d2c = latest.get("days_to_cover") or 0
        out["crowded_short"] = bool(d2c >= 10.0)
        out["extreme_short"] = bool(d2c >= 15.0)
    if short_volume:
        recent = short_volume[-5:]
        avg = sum(r["short_pct"] for r in recent) / len(recent)
        out["short_volume_pct_5d"] = round(avg, 1)
        out["heavy_short_selling"] = bool(avg >= 55.0)
    return out or None


def runway(fin: dict):
    """Quarters of liquidity left at the last reported operating burn rate.

    Uses cash *plus* short-term investments -- counting only cash understates a
    biotech's runway by an order of magnitude and would veto perfectly solvent
    companies. Stale inputs return a result flagged `stale`, which the caller
    must not turn into a veto: an old balance sheet is ignorance, not distress.
    """
    liq = (fin or {}).get("liquidity") or {}
    burn = (fin or {}).get("burn") or {}
    total, q = liq.get("total_usd"), burn.get("quarterly_usd")
    if not total or q is None or q >= 0:
        return None  # cash-flow positive, or not reported
    quarters = total / abs(q)
    return {
        "liquidity_usd": total,
        "cash_usd": liq.get("cash_usd"),
        "investments_usd": liq.get("investments_usd"),
        "investments_tag": liq.get("investments_tag"),
        "investments_discovered": bool(liq.get("investments_discovered")),
        # True only when a full fact sweep confirmed the company really does
        # hold no short-term investments. Until then a cash-only balance is
        # more likely a missing tag than distress.
        "cash_only_verified": bool(
            liq.get("investments_discovery_ran") and not liq.get("investments_discovered")
        ),
        "cash_as_of": liq.get("as_of"),
        "age_days": liq.get("age_days"),
        "stale": bool(liq.get("stale") or burn.get("stale")),
        "quarterly_burn_usd": abs(q),
        "quarters": round(quarters, 2),
        "months": round(quarters * 3, 1),
        "estimated_exhaustion": (
            (datetime.strptime(liq["as_of"], "%Y-%m-%d").date()
             + timedelta(days=int(quarters * 91))).isoformat()
            if liq.get("as_of") else None
        ),
    }


def dilution(fin: dict):
    hist = (fin or {}).get("share_history") or []
    if len(hist) < 2:
        return None
    first, last = hist[0], hist[-1]
    if not first["shares"]:
        return None
    return {
        "from": first["filed"], "to": last["filed"],
        "shares_start": first["shares"], "shares_now": last["shares"],
        "change_pct": round((last["shares"] / first["shares"] - 1.0) * 100.0, 1),
    }


def next_catalyst(trials: list[dict]):
    today = date.today().isoformat()
    upcoming = [
        t for t in trials or []
        if t.get("primary_completion") and t["primary_completion"] >= today[:7]
        and t.get("status") in ACTIVE_TRIAL
    ]
    upcoming.sort(key=lambda t: t["primary_completion"])
    return upcoming[0] if upcoming else None


# --------------------------------------------------------------------- tiers


def financial_vetoes(out: dict, rec: dict, hard: list, soft: list, settings: dict) -> None:
    """Turn the balance sheet and recent price action into vetoes and flags.

    Mutates `hard` and `soft` in place. Extracted from analyse() because it is
    the densest cluster of hard-won rules in the codebase -- every branch here
    corresponds to a case that once produced a wrong answer, and it needs to be
    readable on its own.
    """
    # Financing pressure is its own veto: a company inside one quarter of cash
    # will raise, and it will raise at a discount to wherever the price is.
    for ev in out["recent_events"]:
        if ev["kind"] == "collapse":
            cause = "; ".join(
                f"{c['form']} {c['filed']}" + (f" ({', '.join(c['meanings'])})" if c["meanings"] else "")
                for c in ev["likely_cause_filings"]
            ) or "no filing within 2 days -- check press releases and news"
            hard.append({
                "form": "price collapse", "filed": ev["date"], "days_ago": ev["sessions_ago"],
                "reason": (
                    f"{ev['change_pct']}% single-session collapse {ev['sessions_ago']} sessions ago "
                    f"-- oversold readings here are a broken thesis until proven otherwise. "
                    f"Likely cause: {cause}"
                ),
                "url": (ev["likely_cause_filings"][0]["url"] if ev["likely_cause_filings"] else None),
            })

    # XBRL lags the filing itself: a 10-Q can be on file for days before its
    # figures appear in companyconcept, leaving a runway computed from the prior
    # quarter. If a periodic report covers a later period than the balance sheet
    # we used, the number is superseded and must not drive a decision.
    rw = out["runway"]
    if rw:
        as_of = rw.get("cash_as_of") or ""
        newer = [
            f for f in (rec.get("filings") or [])
            if (f.get("form") or "").upper() in ("10-Q", "10-K")
            and (f.get("report_date") or "") > as_of
        ]
        # Companies routinely announce a quarter by 8-K weeks before the 10-Q,
        # so the freshest balance sheet often exists only in an earnings release
        # and never reaches XBRL in time. A results 8-K filed well after our
        # balance-sheet date is reporting a later quarter; 45 days clears the
        # normal same-quarter reporting gap.
        earnings = [
            f for f in (rec.get("filings") or [])
            if "2.02" in (f.get("items") or "")
            and as_of and _days_between(as_of, f.get("filed") or "") > 45
        ]
        if newer or earnings:
            n = (sorted(newer, key=lambda f: f["report_date"])[-1] if newer
                 else sorted(earnings, key=lambda f: f["filed"])[-1])
            rw["superseded_by"] = {
                "form": n["form"], "report_date": n.get("report_date"),
                "filed": n["filed"], "url": n.get("url"),
            }
            soft.append({
                "form": "superseded financials", "filed": n["filed"],
                "days_ago": _days_ago(n["filed"]),
                "reason": (
                    f"runway uses a {rw['cash_as_of']} balance sheet, but {n['form']} "
                    f"filed {n['filed']} reports a later period -- read it for current "
                    f"liquidity before sizing"
                ),
                "url": n.get("url"),
            })

    if rw and not rw["investments_usd"] and not rw["cash_only_verified"]:
        soft.append({
            "form": "liquidity unverified", "filed": rw["cash_as_of"], "days_ago": rw.get("age_days"),
            "reason": "balance shows cash and no short-term investments, and the tag sweep did "
                      "not run -- verify before treating a short runway as distress",
            "url": None,
        })
    if rw and rw["quarters"] < 1.5 and not rw["stale"]:
        hard.append({
            "form": "cash runway", "filed": rw["cash_as_of"], "days_ago": _days_ago(rw["cash_as_of"] or ""),
            "reason": (
                f"only {rw['quarters']} quarters of liquidity "
                f"(${rw['liquidity_usd']:,.0f} as of {rw['cash_as_of']}) at current burn "
                f"-- a dilutive raise is near-certain"
                + (" [cash-only balance confirmed by a full XBRL tag sweep]"
                   if rw["cash_only_verified"] else "")
            ),
            "url": None,
        })
    elif rw and rw["stale"]:
        soft.append({
            "form": "stale financials", "filed": rw["cash_as_of"], "days_ago": rw.get("age_days"),
            "reason": f"balance sheet is {rw.get('age_days')} days old -- runway estimate unreliable, verify before sizing",
            "url": None,
        })


def price_metrics(bars: list[dict], closes: list[float]) -> dict:
    """Where the price sits: against its own year, its recent high, and itself."""
    last = closes[-1]
    year = closes[-252:] if len(closes) >= 252 else closes
    hi52, lo52 = max(year), min(year)
    hi30 = max(closes[-30:])
    return {
        "close": round(last, 4),
        "date": bars[-1]["date"],
        "chg_1d_pct": _r(pct_change(closes, 1)),
        "chg_5d_pct": _r(pct_change(closes, 5)),
        "chg_20d_pct": _r(pct_change(closes, 20)),
        "week52_high": round(hi52, 4),
        "week52_low": round(lo52, 4),
        "pct_off_52w_high": _r((last / hi52 - 1.0) * 100.0) if hi52 else None,
        "pct_above_52w_low": _r((last / lo52 - 1.0) * 100.0) if lo52 else None,
        "drawdown_from_30d_high_pct": _r((last / hi30 - 1.0) * 100.0) if hi30 else None,
        "percentile_1y": _r(percentile_rank(year, last)),
    }


def technical_metrics(bars: list[dict], closes: list[float], vols: list[float]) -> dict:
    """Indicator readings. RSI and %B are the two the tier rules actually use."""
    last = closes[-1]
    pctb, _upper, lower = bollinger_pct_b(closes)
    a = atr(bars)
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    return {
        "rsi14": _r(rsi(closes)),
        "bollinger_pct_b": _r(pctb, 3),
        "bollinger_lower": _r(lower),
        "sma20": _r(s20), "sma50": _r(s50), "sma200": _r(s200),
        "pct_vs_sma50": _r((last / s50 - 1.0) * 100.0) if s50 else None,
        "pct_vs_sma200": _r((last / s200 - 1.0) * 100.0) if s200 else None,
        "atr14": _r(a),
        "atr_pct_of_price": _r(a / last * 100.0) if a and last else None,
        "volume_last": vols[-1] if vols else None,
        "volume_vs_20d_avg": (
            _r(vols[-1] / (sum(vols[-20:]) / 20), 2)
            if len(vols) >= 20 and sum(vols[-20:]) else None
        ),
    }


def analyse(rec: dict, settings: dict, bench_bars: list | None = None,
            catalysts: dict | None = None, regime: dict | None = None,
            resolved: dict | None = None, last_alerts: dict | None = None) -> dict:
    # bars and closes must stay index-aligned: crash_scan maps a closes index
    # back to bars[i]["date"], so filtering one list and not the other would
    # pin a collapse to the wrong session.
    bars = [b for b in (rec.get("bars") or []) if b.get("adjclose") is not None]
    closes = [b["adjclose"] for b in bars]
    vols = [b["volume"] for b in bars if b.get("volume") is not None]
    bucket = rec.get("tier", "") or "A"
    out: dict = {"symbol": rec["symbol"], "company": rec.get("company", ""),
                 "bucket": bucket, "thesis": rec.get("thesis", ""),
                 "invalidation": rec.get("invalidation", ""),
                 "price_source": rec.get("price_source"),
                 # Sizing ceiling is set by the watchlist bucket, not by the signal.
                 "max_position_pct": (
                     float(settings.get("max_position_pct_lottery", 5))
                     if bucket == "lottery"
                     else float(settings.get("max_position_pct", 28))
                 )}

    if len(closes) < 30:
        out["tier"] = "NO_DATA"
        out["reasons"] = ["insufficient price history"]
        return out

    last = closes[-1]
    out["price"] = price_metrics(bars, closes)
    out["technicals"] = technical_metrics(bars, closes, vols)

    # A >15% single-day move is an event, not noise. It must be explained by a
    # filing or news before any tier is acted on.
    chg1 = out["price"]["chg_1d_pct"]
    out["event_move"] = bool(chg1 is not None and abs(chg1) >= 15.0)
    out["recent_events"] = crash_scan(bars, closes, rec.get("filings"))
    out["relative_strength"] = relative_strength(bars, bench_bars or [])
    out["insiders"] = insider_signal(rec.get("insiders"), last)
    out["short"] = short_signal(rec.get("short_interest") or [], rec.get("short_volume") or [])
    out["move"] = move_profile(bars, bench_bars or [])
    out["float"] = float_metrics(rec.get("financials") or {}, bars, rec.get("short_interest") or [])
    out["catalysts"] = (catalysts or {}).get(rec["symbol"], [])[:3]
    out["tradability"] = tradability(bars, settings)
    out["links"] = chart_links(rec["symbol"], rec.get("cik"))
    out["links_md"] = links_markdown(rec["symbol"], out["links"])

    hard, soft = evaluate_filings(rec.get("filings"))
    out["hard_vetoes"], out["soft_flags"] = hard, soft
    out["runway"] = runway(rec.get("financials"))
    out["dilution"] = dilution(rec.get("financials"))
    out["next_catalyst"] = next_catalyst(rec.get("trials"))
    out["new_filings_since_last_run"] = rec.get("new_filings_since_last_run") or []

    financial_vetoes(out, rec, hard, soft, settings)
    rw = out["runway"]
    min_rw = float(settings.get("min_runway_quarters_for_act", 3))

    reasons, tier = [], "NONE"
    p = out["price"]
    t = out["technicals"]

    rsi_v, pctb, volr = t["rsi14"], t["bollinger_pct_b"], t["volume_vs_20d_avg"]
    have = rsi_v is not None and pctb is not None

    # Cheapness within the 1y range is reported as context but no longer sets a
    # tier: the backtest scored it at +0.49pp over 20 sessions and -2.68pp over
    # 60, i.e. indistinguishable from noise. It was previously the WATCH rule.
    if p["percentile_1y"] is not None and p["percentile_1y"] <= 25:
        reasons.append(
            f"bottom quartile of 1y range (pctile {p['percentile_1y']}) -- context, not a signal"
        )

    oversold = have and rsi_v < SETUP_RSI and pctb < SETUP_PCTB
    if oversold:
        reasons.append(f"oversold: RSI {rsi_v}, %B {pctb}")
        tier = "SETUP" if not hard else "WATCH"
    elif have and rsi_v < WATCH_RSI and pctb < WATCH_PCTB:
        tier = "WATCH"
        reasons.append(f"approaching oversold: RSI {rsi_v}, %B {pctb}")

    # Volume confirmation is what separates a bounce that holds from one that
    # fades: it is the only variant that still showed edge at 60 sessions.
    out["capitulation_volume"] = bool(oversold and volr and volr > CAPITULATION_VOL)
    if out["capitulation_volume"]:
        reasons.append(f"capitulation volume ({volr}x the 20d average) -- signal holds up longer")

    rs = out.get("relative_strength")
    if rs and "20d" in rs:
        r20 = rs["20d"]
        if rs.get("sector_wide"):
            reasons.append(
                f"sector-wide: XBI {r20['benchmark_pct']}% over 20d vs this name "
                f"{r20['stock_pct']}% -- much of this move is beta, not the company"
            )
        elif rs.get("idiosyncratic"):
            reasons.append(
                f"idiosyncratic weakness: {r20['excess_pct']}pp behind XBI over 20d "
                f"while the sector held up -- find the company-specific cause"
            )

    ins = out.get("insiders")
    if ins and ins["cluster_buy"]:
        reasons.append(
            f"INSIDER CLUSTER BUY: {ins['distinct_buyers']} insiders bought "
            f"${ins['buy_value_usd']:,.0f} of open-market stock in the last "
            f"{ins['lookback_days']}d -- the strongest evidence available for refuting a veto"
        )
    elif ins and ins["notable_buy"]:
        reasons.append(
            f"insider buying: ${ins['buy_value_usd']:,.0f} open-market purchases "
            f"by {ins['distinct_buyers']} insider(s)"
        )
    elif ins and ins["net_selling"]:
        reasons.append(
            f"insider net selling: ${ins['sell_value_usd']:,.0f} sold vs "
            f"${ins['buy_value_usd']:,.0f} bought over {ins['lookback_days']}d"
        )

    sh = out.get("short")
    if sh and sh.get("extreme_short"):
        reasons.append(
            f"EXTREME short crowding: {sh['days_to_cover']:.1f} days to cover on "
            f"{sh['short_interest']:,.0f} shares -- heavy squeeze fuel if a catalyst lands"
        )
    elif sh and sh.get("crowded_short"):
        reasons.append(
            f"crowded short: {sh['days_to_cover']:.1f} days to cover "
            f"(note: >5 is normal in this sector)"
        )
    if sh and sh.get("heavy_short_selling"):
        reasons.append(
            f"heavy daily short selling: {sh['short_volume_pct_5d']}% of volume over 5 sessions"
        )

    # Extreme readings are a trap, not a bigger opportunity.
    if have and rsi_v < RSI_TRAP:
        reasons.append(
            f"RSI {rsi_v} is below {RSI_TRAP}: historically this bucket UNDERPERFORMS "
            f"at 60 sessions -- treat as distress, not as a deeper discount"
        )

    if hard:
        reasons.append(f"HARD VETO x{len(hard)}: " + "; ".join(h["reason"] for h in hard[:3]))

    # A zone describes the price regime it was built from. Once price has moved
    # far beyond that regime the zone is describing a company that no longer
    # trades there -- and because a stale zone fails CLOSED (no signal), the
    # failure is silent. Surface it rather than let ACT quietly never fire.
    zone_hi = rec.get("entry_high", 0) or 0
    if zone_hi:
        drift = (last / zone_hi - 1.0) * 100.0
        out["zone_drift_pct"] = round(drift, 1)
        out["zone_stale"] = bool(abs(drift) >= ZONE_STALE_DRIFT_PCT)
        if out["zone_stale"]:
            soft.append({
                "form": "stale entry zone", "filed": "", "days_ago": None,
                "reason": (f"price is {drift:+.0f}% from its entry zone "
                           f"(high ${zone_hi}); the zone predates this regime. "
                           f"Re-run propose_zones.py or set it by hand -- until then "
                           f"ACT cannot fire for this name"),
                "url": None,
            })

    # At or below entry_high is the gate. A price *below* entry_low is cheaper,
    # not disqualifying -- gating on a floor would reject a name making new lows
    # while oversold and unvetoed, which is precisely the setup this system
    # exists to find. "Broken" is the veto layer's job, not the zone's.
    # entry_low is carried through as a scale-in reference for the report.
    in_zone = bool(rec.get("entry_high", 0) and last <= rec["entry_high"])
    catalyst_soon = bool(
        out["next_catalyst"] and out["next_catalyst"].get("primary_completion", "9999")
        <= (date.today() + timedelta(days=90)).isoformat()
    )
    runway_ok = bool(
        rw and not rw["stale"] and not rw.get("superseded_by") and rw["quarters"] >= min_rw
    )

    # ACT requires volume confirmation. Scored against XBI on this watchlist's
    # own history, oversold alerts WITHOUT capitulation volume returned -2.68%
    # median excess at 60 sessions on a 44% win rate -- worse than just owning
    # the ETF. With it, +0.25% median and +7.08% mean. An unconfirmed bounce is
    # worth surfacing for review, never worth calling actionable.
    out["regime"] = regime
    # In a falling sector the same setup is a worse bet, so ACT demands more
    # corroboration rather than being blocked outright.
    regime_down = bool(regime and regime.get("label") == "downtrend")
    if tier == "SETUP" and in_zone and (runway_ok or catalyst_soon):
        conv_now = conviction(out, hard, soft, catalyst_soon)
        if regime_down and conv_now["label"] != "strong":
            reasons.append(
                f"{BENCHMARK} is below its 200-day average: in a falling sector ACT requires "
                f"a strong conviction score, and this one is '{conv_now['label']}'"
            )
        elif out["capitulation_volume"]:
            tier = "ACT"
            reasons.append(
                "inside entry zone, acceptable financing/catalyst backdrop, "
                "and confirmed by capitulation volume"
            )
        else:
            reasons.append(
                "in zone and oversold, but NO capitulation volume -- unconfirmed bounces "
                "have underperformed XBI by ~2.7pp median over 60 sessions, so this stays "
                "SETUP rather than ACT"
            )
    elif tier == "SETUP" and not rec.get("entry_high", 0):
        reasons.append("no entry zone declared in watchlist.toml -- ACT tier disabled for this name")

    cats = out.get("catalysts") or []
    if cats:
        c = cats[0]
        conf = c.get("confidence", "expected")
        if c["days_until"] <= 21:
            reasons.append(
                f"CATALYST IN {c['days_until']} DAYS ({c['date']}, {c.get('kind','event')}, "
                f"{conf}): {c.get('description','')[:110]} -- a binary this close dominates "
                f"any technical read; size for the outcome, not the chart"
            )
        elif c["days_until"] <= 90:
            reasons.append(
                f"catalyst in {c['days_until']} days ({c['date']}, {c.get('kind','event')}, {conf})"
            )

    fl = out.get("float") or {}
    if fl.get("heavily_shorted_float"):
        reasons.append(
            f"{fl['short_pct_of_float']}% of estimated float is short"
            + (" (float figure is stale; treat as an upper bound)" if fl.get("stale") else "")
        )

    mv = out.get("move") or {}
    if mv.get("big_move"):
        bits = [f"{mv['chg_1d_pct']:+.1f}% today"]
        if mv.get("sigma") is not None:
            bits.append(f"{abs(mv['sigma']):.1f} sigma vs its own normal "
                        f"{mv['typical_daily_move_pct']}% day")
        if mv.get("excess_1d_pct") is not None:
            bits.append(f"{mv['excess_1d_pct']:+.1f}pp vs XBI")
        if mv.get("gap_pct") is not None and abs(mv["gap_pct"]) >= 3:
            bits.append(f"gapped {mv['gap_pct']:+.1f}% at the open")
        reasons.append("BIG MOVE: " + ", ".join(bits))

    tr = out.get("tradability") or {}
    if tr.get("very_illiquid"):
        reasons.append(
            f"NOT REALISTICALLY TRADEABLE: median ${tr['median_dollar_volume_20d']:,.0f}/day. "
            f"A position above ~${tr['comfortable_position_usd']:,.0f} moves the price against you"
        )
    elif tr.get("illiquid"):
        reasons.append(
            f"thin liquidity: median ${tr['median_dollar_volume_20d']:,.0f}/day -- "
            f"size below ~${tr['comfortable_position_usd']:,.0f} and use limit orders only"
        )

    la = (last_alerts or {}).get(rec["symbol"])
    if la:
        la = dict(la)
        la["sessions_ago"] = sum(1 for b in bars if b["date"] > la["date"])
        if la.get("close"):
            la["return_pct"] = (last / la["close"] - 1.0) * 100.0
    out["exit_flags"] = exit_signals(
        rec, out, last, hard, (resolved or {}).get(rec["symbol"], []), la)
    for fl in out["exit_flags"]:
        reasons.append(f"EXIT SIGNAL ({fl['severity']}) {fl['kind']}: {fl['detail'][:150]}")

    out["tier"] = tier
    out["conviction"] = conviction(out, hard, soft, catalyst_soon)
    out["reasons"] = reasons
    return out


def _r(v, nd: int = 2):
    return None if v is None else round(v, nd)


# ---------------------------------------------------------------------- output


def render_table(rows: list[dict]) -> str:
    head = (
        "| Ticker | Bkt | Close | 1d | 5d | vs XBI 20d | %off 52wH | 1y pctile | RSI | %B | Runway | Veto | Tier |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    body = []
    for r in rows:
        if r.get("tier") == "NO_DATA":
            body.append(f"| {r['symbol']} | {r.get('bucket','')} | - | - | - | - | - | - | - | - | - | - | NO_DATA |")
            continue
        p, t = r["price"], r["technicals"]
        rw = r.get("runway")
        nveto = len(r.get("hard_vetoes") or [])
        body.append(
            f"| [{r['symbol']}]({(r.get('links') or {}).get('finviz','#')}) | {r.get('bucket','')} "
            f"| ${p['close']} | {_pp(p['chg_1d_pct'])} "
            f"| {_pp(p['chg_5d_pct'])} | {_rs(r)} | {_pp(p['pct_off_52w_high'])} | {p['percentile_1y']} "
            f"| {t['rsi14']} | {t['bollinger_pct_b']} | {str(rw['quarters']) + 'q' if rw else 'n/a'} "
            f"| {('**' + str(nveto) + '**') if nveto else '-'} "
            f"| {'**' + r['tier'] + '**' if r['tier'] in ('SETUP', 'ACT') else r['tier']} |"
        )
    return head + "\n".join(body)


def _pp(v):
    return "-" if v is None else f"{v:+.1f}%"


def _rs(r):
    rs = (r.get("relative_strength") or {}).get("20d")
    return "-" if not rs else f"{rs['excess_pct']:+.1f}pp"


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute signals and veto flags")
    ap.add_argument("--snapshot", default=str(DATA / "latest.json"))
    ap.add_argument("--watchlist", default=str(ROOT / "watchlist.toml"))
    ap.add_argument("--out", default=str(DATA / "signals.json"))
    # Screening a candidate list must not consume the live watchlist's alert
    # state, or a genuine tier change would be silently marked as already seen.
    ap.add_argument("--state", default=str(STATE / "alerts.json"))
    args = ap.parse_args()

    snap = localconfig.require_data(Path(args.snapshot))
    cfg = tomllib.loads(Path(args.watchlist).read_text())
    settings = cfg.get("settings", {})

    # The watchlist is authoritative for anything a human edits. The snapshot
    # only carries a copy from whenever it was last fetched, so reading zones
    # from it would mean an edited entry zone did nothing until the next fetch --
    # a silent no-op, which is the worst kind.
    overlay = {t["symbol"].upper(): t for t in cfg.get("ticker", [])}
    for sym, rec in snap["tickers"].items():
        t = overlay.get(sym)
        if not t:
            continue
        for field in ("entry_low", "entry_high", "tier", "thesis", "invalidation"):
            if field in t:
                rec[field] = t[field]

    bench = (snap.get("benchmarks") or {}).get(BENCHMARK) or {}
    bench_bars = bench.get("bars") or []
    if not bench_bars:
        print(f"[signals] WARNING: no {BENCHMARK} benchmark in snapshot -- "
              f"relative strength unavailable", file=sys.stderr)
    catalysts = load_catalysts(ROOT / "catalysts.toml")
    regime = market_regime(bench_bars)
    if regime:
        print(f"[signals] {BENCHMARK} regime: {regime['label']} "
              f"({regime['pct_vs_sma200']:+.1f}% vs 200DMA, {regime['chg_60d_pct']:+.1f}% over 60d)",
              file=sys.stderr)
    resolved = resolved_catalysts(ROOT / "catalysts.toml")
    last_alerts = load_last_alerts(DATA / "history.sqlite")
    rows = [analyse(rec, settings, bench_bars, catalysts, regime, resolved, last_alerts)
            for rec in snap["tickers"].values()]
    order = {"ACT": 0, "SETUP": 1, "WATCH": 2, "NONE": 3, "NO_DATA": 4}
    rows.sort(key=lambda r: (order.get(r["tier"], 9), r["symbol"]))

    # Tier changes, not tier states, are what deserve a phone buzz. A name that
    # has been oversold for three weeks should not ping every single evening.
    STATE.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state)
    prev = json.loads(state_path.read_text()) if state_path.exists() else {}
    changes, exit_alerts = [], []
    for r in rows:
        pr = prev.get(r["symbol"]) or {}
        was = pr.get("tier")
        r["previous_tier"] = was
        r["tier_changed"] = was != r["tier"]
        if r["tier_changed"] and r["tier"] in ("SETUP", "ACT"):
            changes.append(r)
        # Exit signals matter most when they are new. A veto that has been
        # standing for a week is context; one that appeared today is news.
        high = sorted(f["kind"] for f in r.get("exit_flags", []) if f["severity"] == "high")
        r["_exit_key"] = ",".join(high)
        if high and r["_exit_key"] != pr.get("exit_key", ""):
            exit_alerts.append(r)

    # If several correlated names fire at once, sizing each at its bucket cap
    # would commit more than the whole book. Nothing else computes this, and in
    # a sector drawdown these names all trigger together.
    actionable = [r for r in rows if r["tier"] in ("ACT", "SETUP")]
    requested = sum(r.get("max_position_pct", 0) for r in actionable)
    reserve = float(settings.get("min_cash_reserve_pct", 15))
    exposure = {
        "actionable_names": len(actionable),
        "symbols": [r["symbol"] for r in actionable],
        "requested_pct_if_all_taken": round(requested, 1),
        "max_investable_pct": round(100 - reserve, 1),
        "over_committed": bool(requested > 100 - reserve),
        "scale_factor_needed": (round((100 - reserve) / requested, 2)
                                if requested > 100 - reserve else 1.0),
    }
    if exposure["over_committed"]:
        print(f"[signals] WARNING: {len(actionable)} actionable names would request "
              f"{requested:.0f}% of capital against a {100-reserve:.0f}% ceiling -- "
              f"scale positions by {exposure['scale_factor_needed']}", file=sys.stderr)

    out = {
        "regime": regime,
        "exposure": exposure,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "session_date": snap.get("local_date"),
        "data_status": snap.get("status"),
        "settings": settings,
        "signals": rows,
        "notify": [
            {
                "symbol": r["symbol"], "tier": r["tier"], "previous_tier": r["previous_tier"],
                "close": r["price"]["close"], "reason": r["reasons"][0] if r["reasons"] else "",
            }
            for r in changes
        ],
        "notify_exits": [
            {
                "symbol": r["symbol"], "close": r["price"]["close"],
                "flags": [f["kind"] for f in r["exit_flags"] if f["severity"] == "high"],
                "detail": next((f["detail"] for f in r["exit_flags"]
                                if f["severity"] == "high"), ""),
            }
            for r in exit_alerts
        ],
        "table_markdown": render_table(rows),
    }
    # Record what was actually raised so score_alerts.py can grade it later.
    # Only real alerts are logged, not every day a name happens to sit at SETUP:
    # the question being answered is "did the alerts I sent work?"
    if changes and Path(args.state).name == "alerts.json":
        try:
            con = sqlite3.connect(DATA / "history.sqlite")
            # Older databases predate the context column; add it on the fly.
            cols = {r[1] for r in con.execute("PRAGMA table_info(alerts)")}
            if "context" not in cols:
                con.execute("ALTER TABLE alerts ADD COLUMN context TEXT")
            con.executemany(
                "INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        out["session_date"], r["symbol"], r["tier"], r["previous_tier"],
                        r["price"]["close"], r["technicals"]["rsi14"],
                        r["technicals"]["bollinger_pct_b"],
                        int(bool(r.get("capitulation_volume"))), len(r.get("hard_vetoes") or []),
                        ((r.get("relative_strength") or {}).get("20d") or {}).get("excess_pct"),
                        r.get("bucket", ""), "live",
                        "; ".join(r.get("reasons", []))[:500],
                        # Captured so the scorer can later answer questions the
                        # current data cannot: does insider buying into a veto
                        # predict recovery? Does regime change the odds?
                        json.dumps({
                            "conviction": (r.get("conviction") or {}).get("score"),
                            "conviction_label": (r.get("conviction") or {}).get("label"),
                            "regime": (out.get("regime") or {}).get("label"),
                            "insider_cluster": bool((r.get("insiders") or {}).get("cluster_buy")),
                            "insider_net_selling": bool((r.get("insiders") or {}).get("net_selling")),
                            "short_pct_float": (r.get("float") or {}).get("short_pct_of_float"),
                            "illiquid": bool((r.get("tradability") or {}).get("illiquid")),
                            "days_to_catalyst": (r.get("catalysts") or [{}])[0].get("days_until"),
                        }),
                    )
                    for r in changes
                ],
            )
            con.commit()
            con.close()
            print(f"[signals] logged {len(changes)} alert(s) for scoring", file=sys.stderr)
        except Exception as e:
            print(f"[signals] WARNING: could not log alerts: {e}", file=sys.stderr)

    Path(args.out).write_text(json.dumps(out, indent=2))
    state_path.write_text(json.dumps(
        {r["symbol"]: {"tier": r["tier"], "date": out["session_date"],
                       "exit_key": r.get("_exit_key", "")}
         for r in rows}, indent=2
    ))

    print(out["table_markdown"])
    print(f"\n[signals] {len(out['notify'])} new alert(s), "
          f"{len(out['notify_exits'])} new exit signal(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
