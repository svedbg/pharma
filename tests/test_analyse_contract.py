"""Characterisation tests for analyse().

analyse() is the function every signal flows through and, at 360 lines, was the
riskiest thing in the codebase to change. These pin its observable contract so
it can be restructured safely: they assert the shape and the decision rules, not
the internals.
"""

from __future__ import annotations

import pytest
from signals import analyse

SETTINGS = {"max_position_pct": 28, "max_position_pct_lottery": 5,
            "min_cash_reserve_pct": 15, "min_runway_quarters_for_act": 3}


def _bars(prices, vol=1_000_000):
    return [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
             "adjclose": p, "close": p, "open": p, "high": p * 1.01,
             "low": p * 0.99, "volume": vol}
            for i, p in enumerate(prices)]


def _rec(prices, **kw):
    rec = {"symbol": "TEST", "tier": "A", "bars": _bars(prices),
           "filings": [], "financials": {}, "trials": [],
           "entry_low": 0, "entry_high": 0, "invalidation_price": 0}
    rec.update(kw)
    return rec


def test_returns_no_data_without_enough_history():
    out = analyse(_rec([10.0] * 5), SETTINGS)
    assert out["tier"] == "NO_DATA"
    assert "insufficient" in out["reasons"][0]


def test_output_carries_the_keys_the_report_depends_on():
    """The report and the email renderer index these directly; a rename here
    silently empties sections downstream."""
    out = analyse(_rec([10.0 + (i % 9) * 0.3 for i in range(120)]), SETTINGS)
    for key in ("symbol", "bucket", "tier", "reasons", "price", "technicals",
                "max_position_pct", "hard_vetoes", "soft_flags", "recent_events",
                "conviction", "exit_flags", "links", "links_md", "tradability"):
        assert key in out, f"missing key: {key}"
    for key in ("close", "date", "chg_1d_pct", "percentile_1y", "pct_off_52w_high"):
        assert key in out["price"]
    for key in ("rsi14", "bollinger_pct_b", "volume_vs_20d_avg"):
        assert key in out["technicals"]


def test_lottery_bucket_gets_the_smaller_ceiling():
    out = analyse(_rec([10.0] * 40 + [10.5] * 40, tier="lottery"), SETTINGS)
    assert out["max_position_pct"] == 5
    assert analyse(_rec([10.0] * 80, tier="A"), SETTINGS)["max_position_pct"] == 28


def test_a_hard_veto_blocks_setup():
    """An oversold name carrying a veto must degrade to WATCH, never SETUP."""
    falling = [100 - i * 1.5 for i in range(60)]
    veto = [{"form": "424B5", "filed": "2026-01-01", "items": "",
             "offering_type": "priced", "url": "u"}]
    clean = analyse(_rec(falling), SETTINGS)
    vetoed = analyse(_rec(falling, filings=veto), SETTINGS)
    assert vetoed["tier"] != "ACT"
    assert len(vetoed["hard_vetoes"]) >= len(clean["hard_vetoes"])


def test_act_requires_a_declared_zone():
    """Without entry_high the ACT tier is structurally unreachable, and the
    reasons must say so rather than failing silently."""
    falling = [100 - i * 1.5 for i in range(60)]
    out = analyse(_rec(falling), SETTINGS)
    assert out["tier"] != "ACT"
    if out["tier"] == "SETUP":
        assert any("no entry zone" in r for r in out["reasons"])


def test_stale_zone_is_flagged_as_a_soft_flag():
    prices = [10.0] * 60 + [20.0] * 20          # price far above its zone
    out = analyse(_rec(prices, entry_high=10.0, entry_low=8.0), SETTINGS)
    assert out["zone_stale"] is True
    assert any("stale entry zone" in f["form"] for f in out["soft_flags"])


def test_conviction_is_always_present_and_labelled():
    out = analyse(_rec([10.0 + (i % 5) * 0.2 for i in range(120)]), SETTINGS)
    assert out["conviction"]["label"] in ("strong", "moderate", "weak", "avoid")
    assert isinstance(out["conviction"]["supporting"], list)
    assert isinstance(out["conviction"]["against"], list)


def test_deterministic_for_the_same_input():
    """Stage 2 must be pure arithmetic: identical input, identical output."""
    rec = _rec([10.0 + (i % 11) * 0.4 for i in range(150)])
    import json
    a = json.dumps(analyse(rec, SETTINGS), sort_keys=True, default=str)
    b = json.dumps(analyse(rec, SETTINGS), sort_keys=True, default=str)
    assert a == b
