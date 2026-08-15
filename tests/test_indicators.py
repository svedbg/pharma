"""Indicator arithmetic.

Every test here corresponds to a bug that actually occurred while building this,
or to an invariant the veto layer depends on. They are regression protection,
not decoration.
"""

from __future__ import annotations

import math

import pytest

from signals import (
    bollinger_pct_b,
    crash_scan,
    pct_change,
    percentile_rank,
    rsi,
    sma,
    stdev,
)


def test_sma_needs_a_full_window():
    assert sma([1, 2, 3], 5) is None
    assert sma([1, 2, 3, 4, 5], 5) == 3.0


def test_rsi_is_100_when_only_gains():
    assert rsi(list(range(1, 40))) == 100.0


def test_rsi_is_low_on_a_persistent_decline():
    # A monotonic decline must land at the floor, not somewhere mid-range.
    assert rsi([100 - i for i in range(40)]) < 1.0


def test_rsi_seeds_from_the_start_not_the_end():
    """The original implementation seeded Wilder's average from the LAST n
    changes and then smoothed over the whole series again, which double-counted
    recent moves. A series that falls hard then recovers must not read as
    oversold once recovered."""
    falling = [100 - i * 2 for i in range(20)]
    recovering = [falling[-1] + i * 3 for i in range(20)]
    value = rsi(falling + recovering)
    assert value > 50, f"a recovered series should not read oversold, got {value}"


def test_rsi_returns_none_without_enough_history():
    assert rsi([1, 2, 3]) is None


def test_bollinger_pct_b_bounds():
    flat = [10.0] * 25
    pctb, upper, lower = bollinger_pct_b(flat)
    # Zero variance has no meaningful band rather than a divide-by-zero.
    assert pctb is None

    rising = [float(i) for i in range(25)]
    pctb, upper, lower = bollinger_pct_b(rising)
    assert upper > lower
    assert pctb is not None and pctb > 0.5


def test_pct_change_handles_zero_and_short_series():
    assert pct_change([0.0, 5.0], 1) is None
    assert pct_change([10.0], 1) is None
    assert pct_change([10.0, 11.0], 1) == pytest.approx(10.0)


def test_percentile_rank_endpoints():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert percentile_rank(xs, 0.5) == 0.0
    assert percentile_rank(xs, 4.0) == 100.0
    assert percentile_rank([], 1.0) is None


def test_stdev_of_constant_series_is_zero():
    assert stdev([5.0] * 10) == 0.0
    assert stdev([1.0]) is None


# --------------------------------------------------------------- crash_scan


def _bars(prices, start_day=1):
    return [
        {"date": f"2026-01-{start_day + i:02d}", "adjclose": p, "close": p, "volume": 1000}
        for i, p in enumerate(prices)
    ]


def test_crash_scan_flags_a_collapse_on_the_right_date():
    """bars and closes must stay index-aligned. Filtering one list and not the
    other pinned collapses to the wrong session -- a silent, serious error."""
    prices = [10.0] * 5 + [3.0] + [3.1, 3.2]
    bars = _bars(prices)
    closes = [b["adjclose"] for b in bars]
    events = crash_scan(bars, closes, filings=[], lookback=10)
    collapses = [e for e in events if e["kind"] == "collapse"]
    assert len(collapses) == 1
    assert collapses[0]["date"] == bars[5]["date"], "collapse pinned to the wrong session"
    assert collapses[0]["change_pct"] == pytest.approx(-70.0, abs=0.1)


def test_crash_scan_ignores_ordinary_moves():
    bars = _bars([10.0, 10.5, 10.2, 10.4, 10.1, 10.3])
    closes = [b["adjclose"] for b in bars]
    assert crash_scan(bars, closes, filings=[], lookback=10) == []


def test_crash_scan_attaches_the_likely_causal_filing():
    prices = [10.0] * 5 + [3.0]
    bars = _bars(prices)
    closes = [b["adjclose"] for b in bars]
    filings = [{"form": "8-K", "filed": bars[5]["date"], "items": "8.01",
                "item_meanings": ["8.01: other events"], "url": "http://example/8k"}]
    events = crash_scan(bars, closes, filings, lookback=10)
    assert events[0]["likely_cause_filings"][0]["form"] == "8-K"


def test_crash_scan_only_looks_back_the_requested_window():
    # An old collapse outside the window must not resurface as today's news.
    prices = [10.0, 2.0] + [2.0 + i * 0.01 for i in range(30)]
    bars = _bars(prices)
    closes = [b["adjclose"] for b in bars]
    assert crash_scan(bars, closes, filings=[], lookback=5) == []


def test_indicators_never_return_nan():
    """A NaN silently poisons every downstream comparison, and a NaN RSI would
    make `rsi < 35` false rather than raising."""
    series = [1.0, 1.0, 1.0] + [float(i) for i in range(1, 40)]
    for value in (rsi(series), bollinger_pct_b(series)[0], sma(series, 20)):
        assert value is None or not math.isnan(value)
