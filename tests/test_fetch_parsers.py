"""Parsers in fetch.py, exercised offline against recorded API responses.

fetch.py was previously untestable because every path touched the network.
tests/fixtures/ holds the shapes instead: the Nasdaq and FINRA samples are real
responses captured once, and the Form 4 documents are minimal hand-authored
files that reproduce the real structure. Small and readable beats a 5KB filing —
a fixture nobody can read is a fixture nobody maintains.

The Form 4 case matters most: transaction codes are the difference between
"an insider bought" and "an insider exercised options and sold", which are
opposite signals.
"""

from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import fetch

FIXTURES = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------- Form 4 codes


def _parse_form4(path: Path):
    """Mirror fetch_insider_trades' extraction over a local document."""
    doc = ET.fromstring(path.read_text())
    out = []
    for tx in doc.findall("nonDerivativeTable/nonDerivativeTransaction"):
        out.append({
            "code": tx.findtext("transactionCoding/transactionCode"),
            "shares": float(tx.findtext("transactionAmounts/transactionShares/value") or 0),
            "price": float(
                tx.findtext("transactionAmounts/transactionPricePerShare/value") or 0),
            "acquired": tx.findtext(
                "transactionAmounts/transactionAcquiredDisposedCode/value"),
        })
    return doc, out


def test_option_exercise_is_not_an_insider_purchase():
    """The recorded filing is a CEO exercising at $0.99 and selling at $5.06.
    Both legs report as 'acquired'/'disposed', and counting the A leg as a buy
    would invert the signal entirely. Only code P is an open-market purchase."""
    _doc, txs = _parse_form4(FIXTURES / "form4_exercise_and_sell.xml")
    codes = {t["code"] for t in txs}
    assert "M" in codes and "S" in codes
    assert "P" not in codes, "this filing contains no open-market purchase"

    buys = [t for t in txs if t["code"] == "P"]
    sells = [t for t in txs if t["code"] == "S"]
    assert buys == []
    assert sells and sells[0]["price"] > 5.0


def test_open_market_purchase_is_distinguished_from_a_grant():
    """The same filing carries a P (purchase, real money) and an A (grant,
    compensation). Both are 'acquired'; only P is signal."""
    _, txs = _parse_form4(FIXTURES / "form4_open_market_buy.xml")
    buys = [t for t in txs if t["code"] == "P"]
    grants = [t for t in txs if t["code"] == "A"]
    assert len(buys) == 1 and len(grants) == 1
    assert buys[0]["shares"] * buys[0]["price"] == pytest.approx(49_987_200)
    assert grants[0]["price"] == 0, "a grant has no purchase price"


def test_reporting_owner_and_role_are_extracted():
    doc, _ = _parse_form4(FIXTURES / "form4_exercise_and_sell.xml")
    assert doc.findtext("reportingOwner/reportingOwnerId/rptOwnerName")
    rel = doc.find("reportingOwner/reportingOwnerRelationship")
    assert rel is not None
    assert rel.findtext("isOfficer") in ("1", "true")


def test_raw_xml_url_is_derived_from_the_xsl_wrapper():
    """primaryDocument points at an XSL-rendered wrapper; the machine-readable
    document sits one level up. Getting this wrong yields HTML, not XML."""
    url = ("https://www.sec.gov/Archives/edgar/data/1437402/000132140226000016/"
           "xslF345X06/wk-form4_1784318944.xml")
    parts = url.rsplit("/", 2)
    raw = f"{parts[0]}/{parts[2]}" if parts[1].startswith("xsl") else url
    assert raw.endswith("/000132140226000016/wk-form4_1784318944.xml")
    assert "xsl" not in raw


# ---------------------------------------------------------------- Nasdaq


def test_nasdaq_money_strings_are_parsed():
    """Values arrive as '$4.21' and '16,749,500'; naive float() raises."""
    assert fetch._money("$4.21") == pytest.approx(4.21)
    assert fetch._money("16,749,500") == pytest.approx(16_749_500)
    assert fetch._money("") is None
    assert fetch._money("N/A") is None
    assert fetch._money(None) is None
    assert fetch._money("--") is None


def test_nasdaq_rows_become_ascending_bars():
    payload = json.loads((FIXTURES / "nasdaq_historical.json").read_text())
    rows = ((payload.get("data") or {}).get("tradesTable") or {}).get("rows") or []
    assert rows, "fixture should contain rows"

    from datetime import datetime
    bars = []
    for r in rows:
        close = fetch._money(r.get("close"))
        if close is None:
            continue
        bars.append({
            "date": datetime.strptime(r["date"], "%m/%d/%Y").strftime("%Y-%m-%d"),
            "close": close, "volume": fetch._money(r.get("volume")),
        })
    bars.sort(key=lambda b: b["date"])

    # Nasdaq returns newest-first; the pipeline requires oldest-first.
    assert bars[0]["date"] < bars[-1]["date"]
    assert all(b["close"] > 0 for b in bars)
    assert all(len(b["date"]) == 10 for b in bars)


# ----------------------------------------------------------------- FINRA


def test_regsho_lines_parse_into_short_volume_ratios():
    text = (FIXTURES / "regsho_sample.txt").read_text()
    symbols = {"ARDX", "CAPR", "A"}
    found = {}
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 5 or parts[1] not in symbols:
            continue
        short_v, total_v = float(parts[2]), float(parts[4])
        if total_v > 0:
            found[parts[1]] = round(100.0 * short_v / total_v, 2)

    assert found, "fixture should contain at least one watched symbol"
    for sym, pct in found.items():
        assert 0 < pct <= 100, f"{sym} short ratio out of range: {pct}"


# ------------------------------------------------- offering classification


def _classify(text: str) -> str:
    """The keyword logic from classify_offerings, over plain text."""
    text = text.lower()
    atm = sum(kw in text for kw in (
        "at the market offering", "at-the-market offering", "rule 415(a)(4)",
        "sales agreement", "atm program", "atm programme"))
    priced = sum(kw in text for kw in (
        "underwriting agreement", "public offering price", "per share and",
        "underwriters have agreed", "placement agent", "we are offering"))
    if atm and atm >= priced:
        return "atm"
    return "priced" if priced else "unknown"


def test_at_the_market_programme_is_recognised():
    assert _classify(
        "We may offer and sell shares having an aggregate offering price of up to "
        "$400,000,000 from time to time through our sales agent under a Sales "
        "Agreement, in what is deemed an 'at the market offering' as defined in "
        "Rule 415(a)(4) under the Securities Act."
    ) == "atm"


def test_priced_takedown_is_recognised():
    assert _classify(
        "We are offering 10,000,000 shares of our common stock. The public offering "
        "price is $1.05 per share. The underwriters have agreed to purchase the "
        "shares under an Underwriting Agreement dated today."
    ) == "priced"


def test_ambiguous_document_is_left_unknown():
    """Unknown must fail safe toward caution: signals.py keeps the hard veto."""
    assert _classify("This prospectus supplement relates to certain securities.") == "unknown"


# ------------------------------------------------- investment tag discovery


def _facts(tags: dict) -> dict:
    """A companyfacts document carrying the given {tag: [rows]}."""
    return {"facts": {"us-gaap": {t: {"units": {"USD": rows}} for t, rows in tags.items()}}}


def test_a_failed_sweep_is_not_reported_as_a_completed_one(monkeypatch):
    """"The sweep found no securities" and "the sweep never ran" are opposite
    facts, and a bare None conflated them. The caller turns the first into
    cash_only_verified, which is printed inside a hard veto as "confirmed by a
    full XBRL tag sweep" -- a sentence a timed-out request must not be able to
    buy."""
    def boom(url):
        raise fetch.FetchError("HTTP 503")

    monkeypatch.setattr(fetch, "sec_get", boom)
    ran, best = fetch.discover_investments("0000000001", "2026-06-30")
    assert ran is False and best is None


def test_a_completed_sweep_finding_nothing_says_so(monkeypatch):
    monkeypatch.setattr(fetch, "sec_get", lambda url: _facts({}))
    ran, best = fetch.discover_investments("0000000001", "2026-06-30")
    assert ran is True and best is None


def test_the_sweep_finds_an_uncurated_investment_tag(monkeypatch):
    """EWTX reported $388.9M under MarketableSecurities, absent from the curated
    list, and was read as holding $72M of cash."""
    monkeypatch.setattr(fetch, "sec_get", lambda url: _facts({
        "MarketableSecurities": [{"end": "2026-06-30", "val": 388_900_000, "form": "10-Q"}],
        # A duration fact that matches by name; it carries a start, so it is not
        # a balance and must be ignored.
        "PaymentsToAcquireMarketableSecurities": [
            {"start": "2026-01-01", "end": "2026-06-30", "val": 999_000_000, "form": "10-Q"}],
    }))
    ran, best = fetch.discover_investments("0000000001", "2026-06-30")
    assert ran is True
    assert best["tag"] == "MarketableSecurities"
    assert best["value_usd"] == 388_900_000
    assert best["combined"] is False


def test_a_combined_cash_and_investments_tag_is_marked_as_such(monkeypatch):
    """A combined line already contains the cash; adding it to separately
    fetched cash double-counted SION by $63M and would suppress a real veto."""
    monkeypatch.setattr(fetch, "sec_get", lambda url: _facts({
        "CashCashEquivalentsAndShortTermInvestments": [
            {"end": "2026-06-30", "val": 200_000_000, "form": "10-Q"}],
    }))
    _ran, best = fetch.discover_investments("0000000001", "2026-06-30")
    assert best["combined"] is True


# --------------------------------------------------------- misc invariants


def test_sponsor_name_is_cleaned_for_the_trials_search():
    assert fetch.sponsor_from_title("OUTLOOK THERAPEUTICS, INC.") == "Outlook Therapeutics"
    assert fetch.sponsor_from_title("CAPRICOR THERAPEUTICS, INC.") == "Capricor Therapeutics"
    assert fetch.sponsor_from_title("") == ""


def test_http_errors_that_cannot_succeed_are_not_retried(monkeypatch):
    """A 404 means the company never reported that tag. Retrying it three times
    across 60 names is a pointless multiplier on a rate-limited API."""
    import urllib.error

    calls = {"n": 0}

    def boom(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(fetch.urllib.request, "urlopen", boom)
    with pytest.raises(fetch.FetchError):
        fetch.http_get("https://example.org/x", tries=3)
    assert calls["n"] == 1, "404 must not be retried"


def test_a_non_json_200_is_a_fetch_error_not_a_decode_error(monkeypatch):
    """Every caller in this module guards FetchError and nothing else.

    A captive portal or a provider error page answers 200 with HTML, so a bare
    json.loads went straight past all of that guarding -- and in build_record
    the only thing left to catch it discarded the entire ticker record,
    including bars already fetched.
    """
    monkeypatch.setattr(fetch, "http_get",
                        lambda url, ua=None, **kw: b"<html><body>Sign in to the network")
    with pytest.raises(fetch.FetchError) as e:
        fetch.get_json("https://example.org/x")
    assert "non-JSON" in str(e.value)
    assert "Sign in" in str(e.value), "say what answered instead, or it is undebuggable"


def test_a_bad_price_response_falls_back_to_the_other_provider(monkeypatch):
    """Two providers exist because neither is reliable alone. A Nasdaq error
    page used to kill the record outright rather than failing over."""
    monkeypatch.setattr(fetch, "fetch_bars_nasdaq",
                        lambda *a, **k: (_ for _ in ()).throw(fetch.FetchError("non-JSON response")))
    monkeypatch.setattr(fetch, "fetch_bars_yahoo",
                        lambda *a, **k: ([{"date": "2026-08-14", "close": 1.0}], {"currency": "USD"}))
    bars, _meta, source = fetch.fetch_bars("X", 400)
    assert source == "yahoo" and bars


@pytest.fixture
def cik_cache(monkeypatch, tmp_path):
    """Point load_cik_map at a throwaway cache, with the SEC identity stubbed.

    `sec_ua()` is evaluated as an *argument* to get_json, so it runs before the
    call it is passed to. On a machine with no SEC_CONTACT_EMAIL that raises
    SystemExit first and these tests never reach the cache logic they are about
    -- which is why they passed locally and failed in CI, where no contact
    address is configured. That ordering is correct in production: without a
    contact address the SEC fetches cannot happen at all, so dying with the
    instructions beats limping on. It just is not what is under test here.
    """
    monkeypatch.setattr(fetch, "sec_ua", lambda: "pharma-desk/test (ci@example.org)")
    monkeypatch.setattr(fetch, "DATA", tmp_path)
    return tmp_path / "cik_map.json"


def _offline(*_a, **_k):
    raise fetch.FetchError("HTTP 503")


def test_a_stale_cik_map_beats_no_run_at_all(monkeypatch, cik_cache):
    """CIKs move at the pace of corporate actions, not of trading days.

    This is the first request the run makes, and its failure used to end the
    run before a single price was fetched. A rename that the stale map misses
    already presents as 'no CIK in SEC ticker map', which the record carries as
    an error -- a far smaller loss than the whole day.
    """
    cik_cache.write_text(json.dumps({"ARDX": {"cik": "0001437402", "title": "ARDELYX, INC."}}))
    old = time.time() - 30 * 86400
    os.utime(cik_cache, (old, old))                   # older than the 7-day window
    monkeypatch.setattr(fetch, "get_json", _offline)

    got = fetch.load_cik_map()
    assert got["ARDX"]["cik"] == "0001437402"


@pytest.mark.parametrize("junk", ['["not", "a", "map"]', '"a string"', "{}", "not json"])
def test_an_unusable_cached_map_is_no_map_at_all(monkeypatch, cik_cache, junk):
    """Shape-checked, not merely parsed. A file that is valid JSON but not a
    mapping makes .values() raise AttributeError, which no caller guards -- and
    this is the fallback for a failed fetch, so raising would take the run down
    by the very path that exists to keep it alive."""
    cik_cache.write_text(junk)
    monkeypatch.setattr(fetch, "get_json", _offline)
    with pytest.raises(fetch.FetchError):
        fetch.load_cik_map()


def test_no_cached_map_and_no_network_still_fails_loudly(monkeypatch, cik_cache):
    """The fallback is for a stale map, not for no map. Without one there is
    nothing to run on, and inventing a silent empty map would resolve no CIK
    for any name while looking like an ordinary quiet day."""
    assert not cik_cache.exists()
    monkeypatch.setattr(fetch, "get_json", _offline)
    with pytest.raises(fetch.FetchError):
        fetch.load_cik_map()


# ------------------------------------------------------- the session date


def _snap(ticker_dates, benchmark_dates=()):
    def rec(dates):
        return {"bars": [{"date": d, "adjclose": 1.0} for d in dates]}
    return {
        "tickers": {f"T{i}": rec(d) for i, d in enumerate(ticker_dates)},
        "benchmarks": {f"B{i}": rec(d) for i, d in enumerate(benchmark_dates)},
    }


def test_the_session_is_the_newest_bar_not_the_calendar_day():
    """`local_date` was `date.today()`, which is only the session when the day's
    bar already exists.

    It routinely does not: the scheduled run fires 18 minutes after the US
    close, any hand run before then is hours early, and a market holiday has no
    bar at all. When they diverge the alert is keyed a day ahead of the bar its
    own numbers came from, and score_alerts.py -- which resolves an alert to the
    first bar at or after its session date -- grades the entry from the *next*
    session at a price that has already moved. Backfilled alerts are keyed to
    their own bar, so the two halves of the scorecard stop being comparable,
    which is the one comparison it exists to make.
    """
    snap = _snap([["2026-08-13", "2026-08-14"]] * 3, [["2026-08-14"]])
    assert fetch.session_date(snap) == "2026-08-14"


def test_one_lagging_ticker_cannot_move_the_session_for_the_run():
    """Taken as the date most sources agree is their newest, so a single stale
    or halted series does not redate the whole run."""
    snap = _snap([["2026-08-15"]] * 20 + [["2026-07-01"]])
    assert fetch.session_date(snap) == "2026-08-15"


def test_one_ticker_with_a_bogus_future_bar_cannot_move_it_either():
    """The reason this is a mode and not a max()."""
    snap = _snap([["2026-08-15"]] * 20 + [["2027-01-01"]])
    assert fetch.session_date(snap) == "2026-08-15"


def test_a_snapshot_with_no_bars_has_no_session():
    """Not today's date as a consolation prize. signals.py already degrades
    honestly on a missing session -- it warns, and it writes nothing keyed by a
    date it cannot name."""
    assert fetch.session_date({"tickers": {}, "benchmarks": {}}) is None
    assert fetch.session_date({"tickers": {"T": {"bars": []}}}) is None


def test_a_half_published_session_resolves_to_the_newer_date():
    """Ties break forward: if half the list has today's bar and half is still on
    yesterday's, today's session has started to publish."""
    snap = _snap([["2026-08-14"]] * 5 + [["2026-08-15"]] * 5)
    assert fetch.session_date(snap) == "2026-08-15"
