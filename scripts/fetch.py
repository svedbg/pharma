#!/usr/bin/env python3
"""Deterministic data collection for the daily biotech desk.

Everything here is a fact pulled from a primary source. No model, no inference,
no estimation. If a source fails, the failure is recorded and the run is marked
degraded -- a missing price must never reach the report looking like a real one.

Sources (all free, no API keys):
  prices    Nasdaq historical API, Yahoo chart API as fallback
  filings   SEC EDGAR submissions API -- includes 8-K item codes
  financials SEC EDGAR XBRL companyconcept API
  trials    ClinicalTrials.gov API v2

Stdlib only, so nothing can rot out from under a cron job.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localconfig  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "history.sqlite"

# SEC requires a declarative User-Agent with contact info and asks for <10 req/s.
# SEC mandates a real contact address in the User-Agent and throttles generic
# agents. It is read from local settings, never hardcoded, so the repository
# carries no personal address.
SEC_UA = f"pharma-desk/1.0 (personal research; {localconfig.sec_contact()})"
WORKERS = 6


class RateLimiter:
    """Process-wide minimum spacing between requests to one provider.

    Tickers are fetched in parallel, so per-call sleeps would not bound the
    aggregate request rate -- the limit has to be shared across threads.
    """

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.last + self.interval - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self.last = now


SEC_LIMITER = RateLimiter(7.0)      # SEC asks for fewer than 10 req/s
PRICE_LIMITER = RateLimiter(1.5)    # Yahoo 429s aggressively; Nasdaq is calmer
TRIALS_LIMITER = RateLimiter(4.0)
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 8-K item codes that materially change a thesis. Used by the veto layer in
# signals.py -- a cheap price sitting on top of one of these is not a dip.
ITEM_MEANINGS = {
    "1.01": "entry into material definitive agreement",
    "1.02": "termination of material definitive agreement",
    "2.02": "results of operations / earnings",
    "2.03": "creation of a direct financial obligation",
    "3.01": "NASDAQ/NYSE listing rule deficiency or delisting notice",
    "3.02": "unregistered sale of equity securities (dilution)",
    "3.03": "material modification to rights of security holders",
    "4.01": "change in certifying accountant",
    "4.02": "non-reliance on previously issued financials",
    "5.02": "departure/appointment of directors or officers",
    "5.03": "amendment to articles / reverse split mechanics",
    "7.01": "regulation FD disclosure",
    "8.01": "other events (often trial data or regulatory updates)",
}

# Cash alone badly understates a biotech's war chest -- most of the money sits in
# marketable securities. Worse, companies silently stop reporting a tag when they
# change presentation (Viridian's CashAndCashEquivalents last appeared in 2019
# while $816M sat in AFS securities), so tags are selected by recency, never by a
# fixed priority, and anything stale is refused rather than used.
CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]
# These are alternative presentations of the same short-term portfolio, not
# additive line items, so the largest is taken rather than the sum.
INVEST_TAGS = [
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "AvailableForSaleSecuritiesCurrent",
    "OtherShortTermInvestments",
    # Added after EWTX reported $388M under the un-suffixed tags and was read as
    # holding only $72M of cash -- a 6x understatement that put its runway just
    # above the financing veto. The tag family is wider than it looks; when a
    # name shows implausibly low liquidity, check companyfacts for the tag it
    # actually uses before trusting the number.
    "MarketableSecurities",
    "AvailableForSaleSecuritiesDebtMaturitiesWithinOneYearFairValue",
    "ShortTermInvestmentsFairValueDisclosure",
]
BURN_TAGS = [("us-gaap", "NetCashProvidedByUsedInOperatingActivities")]
SHARE_TAGS = [
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
]
# Aggregate market value of shares held by non-affiliates, from the 10-K cover.
# Annual and therefore stale, but it is the only free, authoritative float
# figure -- and float, not shares outstanding, is what shorts have to buy back.
FLOAT_TAGS = [("dei", "EntityPublicFloat")]

# A balance older than this cannot support a runway estimate.
MAX_BALANCE_AGE_DAYS = 200

# Sector benchmarks. XBI is equal-weighted and dominated by small/mid-cap
# biotech, so it is the honest comparison for this watchlist; IBB is
# cap-weighted and behaves more like large-cap pharma. Without one of these,
# a sector-wide drawdown lights up all 60+ names at once and looks like signal.
BENCHMARKS = {"XBI": "SPDR S&P Biotech ETF", "IBB": "iShares Biotechnology ETF"}


class FetchError(Exception):
    pass


def http_get(url: str, ua: str = BROWSER_UA, tries: int = 3, timeout: int = 30) -> bytes:
    """GET with backoff. Raises FetchError rather than returning something falsy."""
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(
            url, headers={"User-Agent": ua, "Accept": "application/json,text/csv,*/*"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            # 404 means "this company never reported this tag" -- not retryable.
            if e.code in (404, 403):
                raise FetchError(f"{last} for {url}") from e
        except Exception as e:  # noqa: BLE001 - network layer, anything can happen
            last = f"{type(e).__name__}: {e}"
        if attempt < tries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"{last} for {url}")


def sec_get(url: str) -> dict:
    SEC_LIMITER.wait()
    return json.loads(http_get(url, ua=SEC_UA))


# --------------------------------------------------------------------------- db


def db_connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS bars (
            ticker TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, adjclose REAL, volume INTEGER,
            PRIMARY KEY (ticker, date)
        );
        CREATE TABLE IF NOT EXISTS filings (
            ticker TEXT, accession TEXT, form TEXT, filed TEXT, report_date TEXT,
            items TEXT, doc_desc TEXT, url TEXT, first_seen TEXT,
            PRIMARY KEY (ticker, accession)
        );
        CREATE TABLE IF NOT EXISTS facts (
            ticker TEXT, tag TEXT, period_start TEXT, period_end TEXT,
            val REAL, unit TEXT, form TEXT, filed TEXT,
            PRIMARY KEY (ticker, tag, period_end, form, period_start)
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_at TEXT PRIMARY KEY, session_date TEXT, status TEXT, note TEXT
        );
        -- Every alert the desk actually raised, so it can be scored later.
        -- Without this the system is unfalsifiable: it emits tiers nobody grades.
        CREATE TABLE IF NOT EXISTS alerts (
            session_date TEXT, ticker TEXT, tier TEXT, previous_tier TEXT,
            close REAL, rsi REAL, pctb REAL, capitulation INTEGER,
            vetoes INTEGER, excess_20d REAL, bucket TEXT,
            source TEXT,          -- 'live' or 'backfill'
            reason TEXT,
            context TEXT,         -- JSON: conviction, regime, insider and float state
            PRIMARY KEY (session_date, ticker, source)
        );
        """
    )
    return con


# ----------------------------------------------------------------------- prices


def fetch_bars(symbol: str, lookback_days: int, asset_class: str = "stocks") -> tuple[list[dict], dict, str]:
    """Daily OHLCV, trying each provider in turn. Returns (bars, meta, source).

    Two independent providers on purpose: Yahoo aggressively rate-limits by IP
    and will happily 429 an entire run, while Nasdaq has no such behaviour but
    carries no split metadata. Neither is reliable enough alone.
    """
    errs = []
    for name, fn in (("nasdaq", fetch_bars_nasdaq), ("yahoo", fetch_bars_yahoo)):
        try:
            bars, meta = (fn(symbol, lookback_days, asset_class)
                          if fn is fetch_bars_nasdaq else fn(symbol, lookback_days))
            if bars:
                return bars, meta, name
            errs.append(f"{name}: no bars")
        except FetchError as e:
            errs.append(f"{name}: {e}")
    raise FetchError(f"all price sources failed for {symbol} -- {'; '.join(errs)}")


def fetch_bars_nasdaq(symbol: str, lookback_days: int, asset_class: str = "stocks") -> tuple[list[dict], dict]:
    """Daily OHLCV from Nasdaq. Values arrive as '$4.21' / '16,749,500' strings.

    ETFs need assetclass=etf; passing 'stocks' for XBI returns a 400.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    url = (
        f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}/historical"
        f"?assetclass={asset_class}&fromdate={start}&todate={end}&limit=9999"
    )
    PRICE_LIMITER.wait()
    payload = json.loads(http_get(url))
    if (payload.get("status") or {}).get("rCode") != 200:
        raise FetchError(f"nasdaq rCode {(payload.get('status') or {}).get('rCode')}")

    rows = ((payload.get("data") or {}).get("tradesTable") or {}).get("rows") or []
    bars = []
    for r in rows:
        close = _money(r.get("close"))
        if close is None:
            continue
        try:
            d = datetime.strptime(r["date"], "%m/%d/%Y").strftime("%Y-%m-%d")
        except (KeyError, ValueError):
            continue
        bars.append(
            {
                "date": d,
                "open": _money(r.get("open")),
                "high": _money(r.get("high")),
                "low": _money(r.get("low")),
                "close": close,
                "adjclose": close,  # Nasdaq restates history across splits
                "volume": _money(r.get("volume")),
            }
        )
    bars.sort(key=lambda b: b["date"])
    if not bars:
        raise FetchError("nasdaq returned no usable rows")
    return bars, {"currency": "USD", "exchange": "Nasdaq", "splits": []}


def _money(v):
    """'$4.21' / '16,749,500' / 'N/A' -> float or None."""
    if v is None:
        return None
    s = str(v).replace("$", "").replace(",", "").strip()
    if not s or s.upper() in ("N/A", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_bars_yahoo(symbol: str, lookback_days: int) -> tuple[list[dict], dict]:
    """Daily OHLCV from Yahoo.

    adjclose is preferred for indicator math: these names do reverse splits, and
    unadjusted history would make a 1-for-20 split look like a 95% crash.
    """
    rng = "2y" if lookback_days <= 730 else "5y"
    last_err = None
    for host in ("query1", "query2"):
        url = (
            f"https://{host}.finance.yahoo.com/v8/finance/chart/"
            f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d&events=div%2Csplit"
        )
        try:
            PRICE_LIMITER.wait()
            payload = json.loads(http_get(url))
        except FetchError as e:
            last_err = str(e)
            continue

        chart = payload.get("chart") or {}
        if chart.get("error"):
            last_err = json.dumps(chart["error"])[:200]
            continue
        results = chart.get("result") or []
        if not results:
            last_err = "empty result set"
            continue

        res = results[0]
        meta = res.get("meta", {})
        stamps = res.get("timestamp") or []
        quote = (res.get("indicators", {}).get("quote") or [{}])[0]
        adj_block = res.get("indicators", {}).get("adjclose") or [{}]
        adj = adj_block[0].get("adjclose") if adj_block else None

        bars = []
        for i, ts in enumerate(stamps):
            close = _at(quote.get("close"), i)
            if close is None:
                continue  # halted / no print that day
            bars.append(
                {
                    "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "open": _at(quote.get("open"), i),
                    "high": _at(quote.get("high"), i),
                    "low": _at(quote.get("low"), i),
                    "close": close,
                    # Yahoo can null out an individual adjclose; fall back to the
                    # raw close so the series never develops holes.
                    "adjclose": (_at(adj, i) if adj else None) or close,
                    "volume": _at(quote.get("volume"), i),
                }
            )
        if not bars:
            last_err = "no usable bars"
            continue

        split_events = ((res.get("events") or {}).get("splits") or {})
        meta_out = {
            "currency": meta.get("currency"),
            "exchange": meta.get("fullExchangeName"),
            "regular_market_price": meta.get("regularMarketPrice"),
            "previous_close": meta.get("chartPreviousClose"),
            "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
            "splits": [
                {
                    "date": datetime.fromtimestamp(v["date"], tz=timezone.utc).strftime("%Y-%m-%d"),
                    "ratio": v.get("splitRatio"),
                }
                for v in split_events.values()
            ],
        }
        return bars, meta_out

    raise FetchError(f"price fetch failed for {symbol}: {last_err}")


def _at(seq, i):
    if not seq or i >= len(seq):
        return None
    return seq[i]


# ---------------------------------------------------------------------- filings


def load_cik_map(force: bool = False) -> dict[str, dict]:
    """Ticker -> {cik, title}. Cached for a week; the source file is ~1MB.

    The title doubles as the ClinicalTrials.gov sponsor query, which is what
    makes a 50-name watchlist practical -- nobody is hand-entering sponsors.
    """
    cache = DATA / "cik_map.json"
    if cache.exists() and not force:
        age = time.time() - cache.stat().st_mtime
        if age < 7 * 86400:
            cached = json.loads(cache.read_text())
            if cached and isinstance(next(iter(cached.values())), dict):
                return cached  # ignore the older ticker->str format

    raw = json.loads(http_get("https://www.sec.gov/files/company_tickers.json", ua=SEC_UA))
    mapping = {
        row["ticker"].upper(): {"cik": str(row["cik_str"]).zfill(10), "title": row["title"]}
        for row in raw.values()
    }
    cache.write_text(json.dumps(mapping))
    return mapping


def sponsor_from_title(title: str) -> str:
    """'OUTLOOK THERAPEUTICS, INC.' -> 'Outlook Therapeutics' for the trials search."""
    if not title:
        return ""
    s = title.split("/")[0].strip()  # drop state suffixes like '/DE'
    for suffix in (", INC.", ", INC", " INC.", " INC", ", CORP.", ", CORP", " CORP.",
                   " CORP", " CORPORATION", ", LTD.", " LTD.", " LTD", " PLC", " CO.",
                   ", L.P.", " HOLDINGS"):
        if s.upper().endswith(suffix):
            s = s[: -len(suffix)]
    return s.strip().rstrip(",").title()


def fetch_filings(cik: str, since: date) -> list[dict]:
    data = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = data.get("filings", {}).get("recent", {})
    out = []
    n = len(recent.get("accessionNumber", []))
    for i in range(n):
        filed = recent["filingDate"][i]
        if datetime.strptime(filed, "%Y-%m-%d").date() < since:
            continue
        acc = recent["accessionNumber"][i]
        acc_nodash = acc.replace("-", "")
        doc = recent["primaryDocument"][i]
        items = recent["items"][i] or ""
        out.append(
            {
                "accession": acc,
                "form": recent["form"][i],
                "filed": filed,
                "report_date": recent["reportDate"][i] or None,
                "items": items,
                "item_meanings": [
                    f"{c.strip()}: {ITEM_MEANINGS[c.strip()]}"
                    for c in items.split(",")
                    if c.strip() in ITEM_MEANINGS
                ],
                "doc_desc": recent["primaryDocDescription"][i] or "",
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{doc}",
            }
        )
    return out


def fetch_concept(cik: str, taxonomy: str, tag: str) -> list[dict]:
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
    try:
        data = sec_get(url)
    except FetchError:
        return []  # company simply does not report this tag
    rows = []
    for unit, entries in (data.get("units") or {}).items():
        for e in entries:
            rows.append(
                {
                    "tag": tag,
                    "unit": unit,
                    "start": e.get("start"),
                    "end": e.get("end"),
                    "val": e.get("val"),
                    "form": e.get("form"),
                    "filed": e.get("filed"),
                }
            )
    rows.sort(key=lambda r: (r["end"] or "", r["filed"] or ""))
    return rows


# Name fragments that identify a short-term investment balance, and fragments
# that rule one out. Used only by the discovery sweep below.
INVEST_NAME_HINTS = (
    "shortterminvestment", "marketablesecurit", "availableforsalesecurit",
    "investmentsfairvalue", "debtsecuritiesavailableforsale",
)
INVEST_NAME_BLOCKERS = (
    "noncurrent", "longterm", "antidilutive", "paymentsto", "proceedsfrom",
    "gainloss", "income", "expense", "amortization", "accretion", "restricted",
    "classofwarrant", "sharebased", "unrealized", "realized", "maturitiesafter",
    "heldtomaturity", "equitymethod", "affiliates", "percent", "numberof",
)


def discover_investments(cik: str, as_of: str) -> dict | None:
    """Find whatever tag this filer actually uses for short-term investments.

    A fixed tag list is guaranteed to keep failing: Edgewise reported $388.9M
    under `MarketableSecurities`, which was absent from the list, and the desk
    read it as holding $72M of cash and nearly fired a financing veto on a
    company with thirteen quarters of runway. Rather than chase tag names
    forever, this sweeps the company's full fact set once -- but only for names
    that already look suspicious, since companyfacts is a multi-megabyte
    document.

    Only *instant* facts qualify. XBRL duration facts carry a `start`; balances
    do not, which cleanly excludes cash-flow items such as
    PaymentsToAcquireMarketableSecurities that would otherwise match by name.
    """
    try:
        data = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    except FetchError:
        return None

    best = None
    for tag, body in ((data.get("facts") or {}).get("us-gaap") or {}).items():
        low = tag.lower()
        if not any(h in low for h in INVEST_NAME_HINTS):
            continue
        if any(b in low for b in INVEST_NAME_BLOCKERS):
            continue
        for unit, rows in (body.get("units") or {}).items():
            if unit != "USD":
                continue
            for r in rows:
                if r.get("start") or r.get("end") != as_of:
                    continue  # duration fact, or the wrong balance date
                val = r.get("val")
                if val and val > 0 and (best is None or val > best["value_usd"]):
                    # Some filers report a single combined line
                    # (CashCashEquivalentsAndShortTermInvestments) that already
                    # contains the cash. Adding it to separately-fetched cash
                    # double-counts and inflates runway -- which would suppress
                    # a genuine financing veto, the most costly direction to be
                    # wrong in.
                    combined = "cash" in low
                    best = {"tag": tag, "value_usd": val, "as_of": as_of,
                            "form": r.get("form"), "discovered": True,
                            "combined": combined}
    return best


def _latest_per_tag(cik: str, tags: list[str], taxonomy: str = "us-gaap") -> dict:
    """{tag: newest observation} for each tag the company actually reports."""
    found = {}
    for tag in tags:
        rows = [r for r in fetch_concept(cik, taxonomy, tag) if r["val"] is not None and r["end"]]
        if rows:
            rows.sort(key=lambda r: (r["end"], r["filed"] or ""))
            found[tag] = rows[-1]
    return found


def fetch_financials(cik: str) -> dict:
    """Latest liquidity, operating burn and share count.

    Burn is normalised to a per-day rate because XBRL cash-flow values are often
    cumulative year-to-date -- a raw 9-month figure read as quarterly would
    overstate the burn by 3x and produce a fake runway crisis.
    """
    out: dict = {"liquidity": None, "burn": None, "shares": None, "share_history": [],
                 "public_float": None}

    cash_rows = _latest_per_tag(cik, CASH_TAGS)
    invest_rows = _latest_per_tag(cik, INVEST_TAGS)
    if cash_rows or invest_rows:
        # Anchor on the most recent balance-sheet date any tag reports, then use
        # only figures as of that same date -- mixing dates invents money.
        as_of = max(r["end"] for r in list(cash_rows.values()) + list(invest_rows.values()))
        cash_at = {t: r for t, r in cash_rows.items() if r["end"] == as_of}
        inv_at = {t: r for t, r in invest_rows.items() if r["end"] == as_of}

        cash_val, cash_tag = None, None
        for tag in CASH_TAGS:  # plain cash preferred over the restricted-inclusive tag
            if tag in cash_at:
                cash_val, cash_tag = cash_at[tag]["val"], tag
                break
        inv_tag = max(inv_at, key=lambda t: inv_at[t]["val"]) if inv_at else None
        inv_val = inv_at[inv_tag]["val"] if inv_tag else None

        age = (date.today() - datetime.strptime(as_of, "%Y-%m-%d").date()).days
        src = cash_at.get(cash_tag) or inv_at.get(inv_tag)

        # Implausibly low liquidity is far more often a missing tag than a
        # company about to run out of money. A clinical-stage biotech holding
        # only cash and no securities is the signature of the EWTX bug, so the
        # discovery sweep runs before that number is allowed to fire a veto.
        discovered = None
        if not inv_val:
            discovered = discover_investments(cik, as_of)
            if discovered:
                inv_tag = discovered["tag"]
                if discovered.get("combined"):
                    # Value is cash + investments already; back out the cash so
                    # the components stay honest and the total is not doubled.
                    inv_val = max(discovered["value_usd"] - (cash_val or 0), 0)
                else:
                    inv_val = discovered["value_usd"]

        out["liquidity"] = {
            # Recorded either way: if the sweep ran and found nothing, the
            # cash-only figure is trustworthy and a veto on it is justified.
            "investments_discovery_ran": bool(not inv_at),
            "investments_discovered": bool(discovered),
            "total_usd": (cash_val or 0) + (inv_val or 0),
            "cash_usd": cash_val,
            "cash_tag": cash_tag,
            "investments_usd": inv_val,
            "investments_tag": inv_tag,
            "as_of": as_of,
            "age_days": age,
            "stale": age > MAX_BALANCE_AGE_DAYS,
            "form": src.get("form") if src else None,
            "filed": src.get("filed") if src else None,
        }

    for tax, tag in BURN_TAGS:
        rows = [
            r for r in fetch_concept(cik, tax, tag)
            if r["val"] is not None and r["start"] and r["end"]
        ]
        if rows:
            latest = rows[-1]
            # A single period can be dominated by a one-off. Mirum's H1 2026
            # showed -$273M of operating cash flow -- an in-licensing payment,
            # not a burn rate -- which implied a 3.2-quarter runway for a
            # company with $176M/quarter of product sales. The median per-day
            # rate across recent periods is used instead, with the latest kept
            # alongside so the divergence is visible rather than hidden.
            recent = []
            for r in rows[-6:]:
                try:
                    d0 = datetime.strptime(r["start"], "%Y-%m-%d").date()
                    d1 = datetime.strptime(r["end"], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                days = max((d1 - d0).days, 1)
                recent.append(r["val"] / days)
            recent.sort()
            d0 = datetime.strptime(latest["start"], "%Y-%m-%d").date()
            d1 = datetime.strptime(latest["end"], "%Y-%m-%d").date()
            days = max((d1 - d0).days, 1)
            per_day = latest["val"] / days
            age = (date.today() - d1).days
            median_per_day = recent[len(recent) // 2] if recent else per_day
            out["burn"] = {
                "tag": tag,
                "period": f"{latest['start']}..{latest['end']}",
                "period_days": days,
                "value_usd": latest["val"],
                "per_day_usd": median_per_day,
                "latest_per_day_usd": per_day,
                "periods_used": len(recent),
                # Flag when the newest period disagrees sharply with the median:
                # that is the signature of a one-off, and the report should say so.
                "one_off_suspected": bool(
                    recent and per_day and abs(per_day) > abs(median_per_day) * 2.0
                ),
                "quarterly_usd": median_per_day * 91.0,
                "latest_quarterly_usd": per_day * 91.0,
                "age_days": age,
                "stale": age > MAX_BALANCE_AGE_DAYS,
                "form": latest["form"],
                "filed": latest["filed"],
            }
            break

    for tax, tag in FLOAT_TAGS:
        rows = [r for r in fetch_concept(cik, tax, tag) if r["val"] is not None and r["end"]]
        if rows:
            latest = rows[-1]
            out["public_float"] = {
                "value_usd": latest["val"],
                "as_of": latest["end"],
                "age_days": (date.today() - datetime.strptime(latest["end"], "%Y-%m-%d").date()).days,
                "form": latest["form"],
            }
            break

    for tax, tag in SHARE_TAGS:
        rows = [r for r in fetch_concept(cik, tax, tag) if r["val"] is not None]
        if rows:
            out["shares"] = {
                "tag": tag,
                "value": rows[-1]["val"],
                "as_of": rows[-1]["end"],
                "filed": rows[-1]["filed"],
            }
            # Trailing share count is the dilution detector: quiet ATM selling
            # shows up here months before anyone writes about it.
            seen = {}
            for r in rows:
                if r["filed"]:
                    seen[r["filed"]] = r["val"]
            out["share_history"] = [
                {"filed": k, "shares": v} for k, v in sorted(seen.items())
            ][-12:]
            break

    return out


# ----------------------------------------------------------------------- trials


def classify_offerings(filings: list[dict], lookback_days: int = 12) -> int:
    """Tag recent 424B filings as an ATM programme or a priced takedown.

    These are materially different and the form type cannot tell them apart. A
    priced takedown sells stock at a discount today; an ATM merely registers
    capacity to dribble stock out over time. COGT's 424B5 was a $400M ATM and
    the veto layer wrongly treated it as active dilution, which took a human
    reading the document to overturn. Doing it mechanically removes the most
    frequent false veto.

    Only the first stretch of the document is read -- the offering type is
    always stated in the opening summary, and these filings run to megabytes.
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    tagged = 0
    for f in filings:
        if not (f.get("form") or "").upper().startswith("424B"):
            continue
        if (f.get("filed") or "") < cutoff:
            continue
        try:
            SEC_LIMITER.wait()
            body = http_get(f["url"], ua=SEC_UA, tries=2, timeout=40)[:400_000]
            text = re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace")).lower()
            text = re.sub(r"\s+", " ", text)
        except FetchError:
            f["offering_type"] = "unknown"
            continue

        atm_hits = sum(kw in text for kw in (
            "at the market offering", "at-the-market offering",
            "rule 415(a)(4)", "sales agreement", "atm program", "atm programme",
        ))
        priced_hits = sum(kw in text for kw in (
            "underwriting agreement", "public offering price", "per share and",
            "underwriters have agreed", "placement agent", "we are offering",
        ))
        if atm_hits and atm_hits >= priced_hits:
            f["offering_type"] = "atm"
        elif priced_hits:
            f["offering_type"] = "priced"
        else:
            f["offering_type"] = "unknown"
        tagged += 1
    return tagged


def fetch_insider_trades(cik: str, filings: list[dict], lookback_days: int = 120,
                         max_forms: int = 20) -> dict:
    """Parse Form 4s into open-market buys and sells.

    The transaction code is everything. Only **P** is an open-market purchase --
    an insider spending their own money, which is the part with signal. Codes A
    (grant), M (option exercise) and F (tax withholding) all show up as
    "acquired" and mean nothing directional; counting them is the classic way to
    manufacture fake insider-buying. ARDX's CEO, for example, filed an M
    (exercise at $0.99) immediately followed by an S (sale at $5.06) -- naively
    that reads as a purchase and is the opposite of one.
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    forms = [f for f in filings if f.get("form") == "4" and (f.get("filed") or "") >= cutoff]
    forms = forms[:max_forms]

    buys, sells, errors = [], [], 0
    for f in forms:
        # primaryDocument points at the XSL-rendered wrapper; the raw XML sits
        # in the same directory one level up from the xslF345XNN/ segment.
        url = f["url"]
        parts = url.rsplit("/", 2)
        raw = f"{parts[0]}/{parts[2]}" if len(parts) == 3 and parts[1].startswith("xsl") else url
        try:
            SEC_LIMITER.wait()
            xml = http_get(raw, ua=SEC_UA, tries=2)
            doc = ET.fromstring(xml)
        except (FetchError, ET.ParseError):
            errors += 1
            continue

        owner = doc.findtext("reportingOwner/reportingOwnerId/rptOwnerName") or "?"
        rel = doc.find("reportingOwner/reportingOwnerRelationship")
        title = ""
        if rel is not None:
            bits = []
            if rel.findtext("isDirector") in ("1", "true"):
                bits.append("director")
            if rel.findtext("isOfficer") in ("1", "true"):
                bits.append(rel.findtext("officerTitle") or "officer")
            if rel.findtext("isTenPercentOwner") in ("1", "true"):
                bits.append("10% owner")
            title = ", ".join(bits)

        for tx in doc.findall("nonDerivativeTable/nonDerivativeTransaction"):
            code = tx.findtext("transactionCoding/transactionCode")
            if code not in ("P", "S"):
                continue
            try:
                shares = float(tx.findtext("transactionAmounts/transactionShares/value") or 0)
                price = float(
                    tx.findtext("transactionAmounts/transactionPricePerShare/value") or 0
                )
            except ValueError:
                continue
            rec = {
                "owner": owner, "title": title,
                "date": tx.findtext("transactionDate/value"),
                "shares": shares, "price": price, "value_usd": shares * price,
                "filed": f["filed"], "url": f["url"],
            }
            (buys if code == "P" else sells).append(rec)

    net = sum(b["value_usd"] for b in buys) - sum(s["value_usd"] for s in sells)
    return {
        "lookback_days": lookback_days,
        "forms_parsed": len(forms),
        "parse_errors": errors,
        "buys": sorted(buys, key=lambda r: r["date"] or "", reverse=True)[:15],
        "sells": sorted(sells, key=lambda r: r["date"] or "", reverse=True)[:15],
        "buy_value_usd": sum(b["value_usd"] for b in buys),
        "sell_value_usd": sum(s["value_usd"] for s in sells),
        "net_value_usd": net,
        "distinct_buyers": len({b["owner"] for b in buys}),
    }


def fetch_short_interest(symbol: str) -> list[dict]:
    """Bi-monthly settled short interest and days-to-cover from Nasdaq."""
    url = (f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(symbol)}"
           f"/short-interest?assetclass=stocks")
    try:
        PRICE_LIMITER.wait()
        payload = json.loads(http_get(url))
    except FetchError:
        return []
    rows = ((payload.get("data") or {}).get("shortInterestTable") or {}).get("rows") or []
    out = []
    for r in rows[:12]:
        try:
            d = datetime.strptime(r["settlementDate"], "%m/%d/%Y").strftime("%Y-%m-%d")
        except (KeyError, ValueError):
            continue
        out.append({
            "settlement_date": d,
            "short_interest": _money(r.get("interest")),
            "avg_daily_volume": _money(r.get("avgDailyShareVolume")),
            "days_to_cover": _money(r.get("daysToCover")),
        })
    out.sort(key=lambda r: r["settlement_date"])
    return out


def fetch_regsho(symbols: set[str], sessions: int = 10) -> dict:
    """Daily short-volume ratio per symbol from FINRA Reg SHO.

    One file covers every symbol, so this is a handful of requests for the whole
    watchlist rather than one per name.
    """
    out: dict = {s: [] for s in symbols}
    day, found = date.today(), 0
    for _ in range(sessions * 2 + 6):  # walk back past weekends and holidays
        if found >= sessions:
            break
        day -= timedelta(days=1)
        if day.weekday() >= 5:
            continue
        url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day:%Y%m%d}.txt"
        try:
            body = http_get(url, tries=1, timeout=25).decode("utf-8", "replace")
        except FetchError:
            continue
        found += 1
        for line in body.splitlines()[1:]:
            parts = line.split("|")
            if len(parts) < 5 or parts[1] not in symbols:
                continue
            try:
                short_v, total_v = float(parts[2]), float(parts[4])
            except ValueError:
                continue
            if total_v > 0:
                out[parts[1]].append({
                    "date": f"{day:%Y-%m-%d}",
                    "short_volume": short_v,
                    "total_volume": total_v,
                    "short_pct": round(100.0 * short_v / total_v, 2),
                })
    for s in out:
        out[s].sort(key=lambda r: r["date"])
    return out


def fetch_trials(sponsor: str) -> list[dict]:
    if not sponsor:
        return []
    params = urllib.parse.urlencode(
        {
            "query.spons": sponsor,
            "pageSize": "40",
            "fields": "NCTId,BriefTitle,OverallStatus,Phase,PrimaryCompletionDate,"
            "StudyFirstPostDate,LastUpdatePostDate,EnrollmentCount,Condition",
        }
    )
    try:
        TRIALS_LIMITER.wait()
        data = json.loads(http_get(f"https://clinicaltrials.gov/api/v2/studies?{params}"))
    except FetchError:
        return []

    out = []
    for s in data.get("studies", []):
        p = s.get("protocolSection", {})
        ident = p.get("identificationModule", {})
        status = p.get("statusModule", {})
        design = p.get("designModule", {})
        out.append(
            {
                "nct_id": ident.get("nctId"),
                "title": ident.get("briefTitle"),
                "status": status.get("overallStatus"),
                "phase": ",".join(design.get("phases", []) or []),
                "primary_completion": (status.get("primaryCompletionDateStruct") or {}).get("date"),
                "last_update": (status.get("lastUpdatePostDateStruct") or {}).get("date"),
                "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
                "conditions": (p.get("conditionsModule") or {}).get("conditions", []),
            }
        )
    # Active studies with the nearest primary completion are the catalyst clock.
    active = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING"}
    out.sort(key=lambda t: (t["status"] not in active, t["primary_completion"] or "9999"))
    return out[:20]


# ------------------------------------------------------------------- persistence


def persist(con: sqlite3.Connection, ticker: str, bars, filings, fin) -> list[dict]:
    """Write to sqlite. Returns filings not seen in any previous run."""
    con.executemany(
        "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)",
        [
            (ticker, b["date"], b["open"], b["high"], b["low"], b["close"], b["adjclose"], b["volume"])
            for b in bars
        ],
    )

    now = datetime.now(timezone.utc).isoformat()
    known = {
        r[0] for r in con.execute("SELECT accession FROM filings WHERE ticker=?", (ticker,))
    }
    fresh = [f for f in filings if f["accession"] not in known]
    con.executemany(
        "INSERT OR IGNORE INTO filings VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                ticker, f["accession"], f["form"], f["filed"], f["report_date"],
                f["items"], f["doc_desc"], f["url"], now,
            )
            for f in filings
        ],
    )

    rows = []
    liq = fin.get("liquidity")
    if liq:
        rows.append((ticker, "Liquidity", "", liq["as_of"], liq["total_usd"], "USD",
                     liq.get("form") or "", liq.get("filed") or ""))
    burn = fin.get("burn")
    if burn:
        start, _, end = burn["period"].partition("..")
        rows.append((ticker, burn["tag"], start, end, burn["value_usd"], "USD",
                     burn.get("form") or "", burn.get("filed") or ""))
    sh = fin.get("shares")
    if sh:
        rows.append((ticker, sh["tag"], "", sh["as_of"], sh["value"], "shares",
                     "", sh.get("filed") or ""))
    con.executemany("INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return fresh


def build_record(entry: dict, cik_map: dict, lookback: int, since: date):
    """All network work for one ticker. Thread-safe; touches no database."""
    sym = entry["symbol"].upper()
    rec: dict = {
        "symbol": sym,
        "tier": entry.get("tier", ""),
        "sponsor": entry.get("sponsor", ""),
        "thesis": entry.get("thesis", ""),
        "entry_low": entry.get("entry_low", 0) or 0,
        "entry_high": entry.get("entry_high", 0) or 0,
        "invalidation": entry.get("invalidation", ""),
        "errors": [],
    }

    bars = []
    try:
        bars, meta, source = fetch_bars(sym, lookback)
        rec["meta"] = meta
        rec["price_source"] = source
        rec["bars"] = bars[-320:]
    except FetchError as e:
        rec["errors"].append(f"prices: {e}")

    listing = cik_map.get(sym) or {}
    cik = listing.get("cik")
    rec["company"] = listing.get("title", "")
    # An explicit sponsor in the watchlist wins; otherwise derive it from the
    # SEC company name so a large watchlist needs no manual upkeep.
    sponsor = entry.get("sponsor") or sponsor_from_title(rec["company"])
    rec["sponsor"] = sponsor

    filings, fin = [], {}
    if not cik:
        rec["errors"].append("no CIK in SEC ticker map (foreign issuer or delisted?)")
    else:
        rec["cik"] = cik
        try:
            filings = fetch_filings(cik, since)
            rec["filings"] = filings[:60]
            classify_offerings(rec["filings"])
        except FetchError as e:
            rec["errors"].append(f"filings: {e}")
        try:
            fin = fetch_financials(cik)
            rec["financials"] = fin
        except FetchError as e:
            rec["errors"].append(f"financials: {e}")

    rec["trials"] = fetch_trials(sponsor)
    if cik and filings:
        try:
            rec["insiders"] = fetch_insider_trades(cik, filings)
        except Exception as e:  # noqa: BLE001 - never let one bad Form 4 kill a name
            rec["errors"].append(f"insiders: {type(e).__name__}: {e}")
    rec["short_interest"] = fetch_short_interest(sym)
    return rec, bars, filings, fin


# -------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch market + regulatory data for the watchlist")
    ap.add_argument("--watchlist", default=str(ROOT / "watchlist.toml"))
    ap.add_argument("--out", default=str(DATA / "latest.json"))
    ap.add_argument("--refresh-ciks", action="store_true")
    args = ap.parse_args()

    cfg = tomllib.loads(Path(args.watchlist).read_text())
    settings = cfg.get("settings", {})
    lookback = int(settings.get("lookback_days", 730))
    tickers = cfg.get("ticker", [])
    if not tickers:
        print("watchlist has no [[ticker]] entries", file=sys.stderr)
        return 2

    cik_map = load_cik_map(force=args.refresh_ciks)
    con = db_connect()
    since = date.today() - timedelta(days=400)

    snapshot: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "local_date": date.today().isoformat(),
        "settings": settings,
        "tickers": {},
        "errors": [],
    }

    # Sector benchmarks first: every name is scored relative to these, so a
    # failure here degrades the run rather than silently dropping the context.
    snapshot["benchmarks"] = {}
    for bsym, bname in BENCHMARKS.items():
        try:
            bbars, _, bsrc = fetch_bars(bsym, lookback, asset_class="etf")
            snapshot["benchmarks"][bsym] = {
                "symbol": bsym, "name": bname, "source": bsrc, "bars": bbars[-320:],
            }
            con.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)",
                [(bsym, b["date"], b["open"], b["high"], b["low"], b["close"],
                  b["adjclose"], b["volume"]) for b in bbars],
            )
            con.commit()
            print(f"[fetch] benchmark {bsym}: {len(bbars)} bars", file=sys.stderr)
        except FetchError as e:
            snapshot["errors"].append(f"benchmark {bsym}: {e}")
            print(f"[fetch] benchmark {bsym} FAILED: {e}", file=sys.stderr)

    # Network fan-out runs in threads (rate limiters keep us inside each
    # provider's limits); sqlite writes stay on the main thread afterwards.
    results: dict = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(build_record, entry, cik_map, lookback, since): entry["symbol"].upper()
            for entry in tickers
        }
        done = 0
        for fut in as_completed(futures):
            sym = futures[fut]
            done += 1
            try:
                results[sym] = fut.result()
            except Exception as e:  # noqa: BLE001 - one bad ticker must not kill the run
                results[sym] = ({"symbol": sym, "errors": [f"unhandled: {type(e).__name__}: {e}"]}, [], [], {})
            print(f"[fetch] {done}/{len(futures)} {sym}", file=sys.stderr)

    # One FINRA file covers every symbol, so this runs once for the whole list.
    try:
        regsho = fetch_regsho({e["symbol"].upper() for e in tickers})
        print(f"[fetch] reg sho: {sum(1 for v in regsho.values() if v)} symbols with short volume",
              file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        regsho = {}
        snapshot["errors"].append(f"regsho: {e}")

    # Persist in watchlist order so output is stable run to run.
    for entry in tickers:
        sym = entry["symbol"].upper()
        rec, bars, filings, fin = results[sym]
        rec["new_filings_since_last_run"] = (
            persist(con, sym, bars, filings, fin) if (bars or filings) else []
        )
        rec["short_volume"] = regsho.get(sym, [])
        snapshot["tickers"][sym] = rec
        snapshot["errors"].extend(f"{sym} {m}" for m in rec.get("errors", []))

    hard_fail = [s for s, r in snapshot["tickers"].items() if not r.get("bars")]
    snapshot["status"] = "degraded" if snapshot["errors"] else "ok"
    snapshot["tickers_without_prices"] = hard_fail

    Path(args.out).write_text(json.dumps(snapshot, indent=2))
    con.execute(
        "INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(),
            date.today().isoformat(),
            snapshot["status"],
            "; ".join(snapshot["errors"])[:500],
        ),
    )
    con.commit()
    con.close()

    print(
        f"[fetch] wrote {args.out} status={snapshot['status']} "
        f"tickers={len(snapshot['tickers'])} errors={len(snapshot['errors'])}",
        file=sys.stderr,
    )
    # Total price failure is fatal: the report must not run on no data.
    return 1 if len(hard_fail) == len(snapshot["tickers"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
