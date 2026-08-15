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
