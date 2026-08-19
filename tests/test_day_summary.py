"""The per-day summary record written for the local site archive.

Regression coverage for the 2026-08-18 failure: PCRX carried two distinct
catalysts guided to the same date (2026-10-01), the sort tied on
(days_until, symbol) and fell through to comparing the catalyst dicts,
and the whole run died on the TypeError.
"""

from __future__ import annotations

from signals import soonest_catalyst


def _row(symbol, catalysts):
    return {"symbol": symbol, "catalysts": catalysts}


def test_two_catalysts_same_day_same_symbol_do_not_crash():
    rows = [_row("PCRX", [
        {"days_until": 44, "date": "2026-10-01", "kind": "readout"},
        {"days_until": 44, "date": "2026-10-01", "kind": "readout"},
    ])]
    out = soonest_catalyst(rows)
    assert out == {"symbol": "PCRX", "date": "2026-10-01",
                   "days_until": 44, "kind": "readout"}


def test_soonest_catalyst_wins_across_names():
    rows = [
        _row("PCRX", [{"days_until": 44, "date": "2026-10-01", "kind": "readout"}]),
        _row("CAPR", [{"days_until": 3, "date": "2026-08-22", "kind": "PDUFA"}]),
    ]
    assert soonest_catalyst(rows) == {"symbol": "CAPR", "date": "2026-08-22",
                                   "days_until": 3, "kind": "PDUFA"}


def test_no_catalysts_anywhere_returns_none():
    assert soonest_catalyst([_row("AARD", []), _row("OTLK", None)]) is None
