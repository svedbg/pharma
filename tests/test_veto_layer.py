"""The veto layer and the data guards protecting it.

These cover the failures that produced genuinely wrong output during
development: liquidity understated tenfold, float ratios of 15,000%, a
combined-tag double count, and an ATM programme treated as a priced offering.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from signals import (
    conviction,
    evaluate_filings,
    exit_signals,
    financial_vetoes,
    float_metrics,
    move_profile,
    runway,
    tradability,
)


def _recent(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


# ------------------------------------------------------------------ filings


def test_priced_offering_is_a_hard_veto():
    hard, soft = evaluate_filings([
        {"form": "424B5", "filed": _recent(2), "items": "",
         "offering_type": "priced", "url": "u"}])
    assert len(hard) == 1 and not soft


def test_atm_programme_is_only_a_soft_flag():
    """An at-the-market programme is registered capacity, not a discounted
    takedown. Treating them alike produced the most frequent false veto."""
    hard, soft = evaluate_filings([
        {"form": "424B5", "filed": _recent(2), "items": "",
         "offering_type": "atm", "url": "u"}])
    assert not hard
    assert len(soft) == 1 and "at-the-market" in soft[0]["reason"]


def test_unclassified_offering_stays_a_hard_veto():
    # Unknown must fail safe toward caution, not toward permission.
    hard, _ = evaluate_filings([
        {"form": "424B5", "filed": _recent(2), "items": "", "url": "u"}])
    assert len(hard) == 1


def test_listing_deficiency_is_a_hard_veto():
    hard, _ = evaluate_filings([
        {"form": "8-K", "filed": _recent(5), "items": "3.01", "url": "u"}])
    assert any("listing" in h["reason"] for h in hard)


def test_stale_filings_fall_out_of_the_window():
    hard, soft = evaluate_filings([
        {"form": "424B5", "filed": _recent(400), "items": "", "url": "u"}])
    assert not hard and not soft


# ------------------------------------------------------------------ runway


def _fin(cash, invest, quarterly_burn, as_of="2026-06-30", age=40):
    return {
        "liquidity": {"total_usd": (cash or 0) + (invest or 0), "cash_usd": cash,
                      "investments_usd": invest, "as_of": as_of, "age_days": age,
                      "stale": age > 200, "investments_discovery_ran": True,
                      "investments_discovered": bool(invest)},
        "burn": {"quarterly_usd": quarterly_burn, "stale": False},
    }


def test_runway_counts_investments_not_just_cash():
    """Counting only cash understated Edgewise's runway by 6x and nearly fired
    a financing veto on a company with thirteen quarters of money."""
    rw = runway(_fin(cash=72_000_000, invest=388_000_000, quarterly_burn=-35_000_000))
    assert rw["quarters"] == pytest.approx(13.1, abs=0.2)


def test_cash_flow_positive_company_has_no_runway_figure():
    # A company generating cash has no burn rate; reporting one is misleading.
    assert runway(_fin(50_000_000, 0, quarterly_burn=+10_000_000)) is None


def test_stale_balance_sheet_is_flagged_not_trusted():
    rw = runway(_fin(10_000_000, 0, -10_000_000, age=400))
    assert rw["stale"] is True


# -------------------------------------------------- runway as a hard veto


def _veto_run(filings, cash=5_000_000, burn=-10_000_000, as_of="2026-03-31"):
    """Run financial_vetoes over one short-runway name and return (hard, soft)."""
    out = {"recent_events": [], "runway": runway(_fin(cash, 0, burn, as_of=as_of, age=40))}
    hard, soft = [], []
    financial_vetoes(out, {"filings": filings}, hard, soft, {})
    return hard, soft, out["runway"]


def test_short_runway_is_a_hard_veto_when_the_balance_sheet_is_current():
    hard, _soft, rw = _veto_run(filings=[])
    assert rw["quarters"] < 1.5
    assert any(h["form"] == "cash runway" for h in hard)


def test_a_superseded_short_runway_does_not_fire_the_hard_veto():
    """The same figure cannot be too old to clear a name and fresh enough to
    condemn it. runway_ok already refuses superseded data, so letting it veto
    trusted it in the direction that hurts: OTLK carried a 0.59-quarter hard veto
    off a balance sheet its own later 8-K had replaced. It stays visible as a
    soft flag -- absence of current data is ignorance, not distress."""
    later = [{"form": "10-Q", "filed": "2026-08-10", "report_date": "2026-06-30",
              "items": "", "url": "u"}]
    hard, soft, rw = _veto_run(filings=later)
    assert rw["quarters"] < 1.5, "test setup: the runway must still be short"
    assert rw["superseded_by"]["form"] == "10-Q"
    assert not any(h["form"] == "cash runway" for h in hard)
    flag = next(f for f in soft if f["form"] == "short runway, superseded")
    # The number must survive the downgrade -- this is a demotion, not a deletion.
    assert "0.5" in flag["reason"] or "0.59" in flag["reason"]


def test_conviction_still_counts_a_superseded_balance_sheet_against_the_name():
    """The demotion above must not make the shortfall invisible to the checklist."""
    out = {"runway": {"quarters": 0.6, "stale": False, "superseded_by": {"form": "10-Q"}},
           "relative_strength": {}, "short": {}, "tradability": {}, "technicals": {},
           "insiders": {}}
    assert any("superseded" in m for m in conviction(out, [], [], False)["against"])


# ------------------------------------------------------------------- float


def _bars_at(price, date="2025-06-30"):
    return [{"date": date, "adjclose": price, "close": price, "volume": 1_000_000}]


def test_float_rejects_implausible_filer_data():
    """PTCT reports a $3.35M public float against 83M shares, which yielded
    '15,401% of float short'. Refusing to answer beats answering wrongly."""
    fin = {"public_float": {"value_usd": 3_350_589, "as_of": "2025-06-30", "age_days": 400},
           "shares": {"value": 83_000_000},
           "share_history": [{"filed": "2025-05-06", "shares": 79_000_000}]}
    out = float_metrics(fin, _bars_at(50.0), [{"short_interest": 10_000_000}])
    assert "unusable" in out
    assert "short_pct_of_float" not in out


def test_float_carries_forward_as_a_fraction_of_shares():
    """XFOR went 5.8M -> 99.1M shares in a year. A year-old absolute float
    against today's short interest is meaningless; the ratio survives."""
    fin = {"public_float": {"value_usd": 50_000_000, "as_of": "2025-06-30", "age_days": 400},
           "shares": {"value": 200_000_000},
           "share_history": [{"filed": "2025-05-01", "shares": 100_000_000}]}
    out = float_metrics(fin, _bars_at(1.0), [{"short_interest": 20_000_000}])
    # 50M float shares of 100M outstanding = 50%; applied to 200M today = 100M.
    assert out["float_fraction"] == pytest.approx(0.5, abs=0.01)
    assert out["float_shares_est"] == pytest.approx(100_000_000, rel=0.01)
    assert out["dilution_adjusted"] is True
    assert out["short_pct_of_float"] == pytest.approx(20.0, abs=0.5)


def test_float_rejects_short_interest_exceeding_float():
    fin = {"public_float": {"value_usd": 10_000_000, "as_of": "2025-06-30", "age_days": 400},
           "shares": {"value": 20_000_000},
           "share_history": [{"filed": "2025-05-01", "shares": 20_000_000}]}
    out = float_metrics(fin, _bars_at(1.0), [{"short_interest": 999_000_000}])
    assert "unusable" in out


# ------------------------------------------------------------- tradability


def test_tradability_flags_a_name_nobody_can_trade():
    bars = [{"close": 1.0, "volume": 15_000} for _ in range(20)]
    out = tradability(bars, {})
    assert out["very_illiquid"] is True
    assert out["comfortable_position_usd"] == pytest.approx(1500, rel=0.1)


def test_tradability_passes_a_liquid_name():
    bars = [{"close": 50.0, "volume": 1_000_000} for _ in range(20)]
    out = tradability(bars, {})
    assert out["illiquid"] is False


# --------------------------------------------------------------- move sigma


def test_big_move_is_measured_in_the_names_own_sigma():
    """A 19% day in a name that normally moves 8.7% is quieter than a 17% day
    in one that normally moves 4.2%. Ranking by percent gets that backwards."""
    calm = [{"date": f"2026-01-{i+1:02d}", "adjclose": 100 + (i % 2), "close": 100 + (i % 2),
             "open": 100, "high": 101, "low": 99, "volume": 1000} for i in range(25)]
    calm[-1] = {**calm[-1], "adjclose": 110.0, "close": 110.0}
    out = move_profile(calm, [])
    assert out["big_move"] is True
    assert out["sigma"] > 2


def test_a_normal_day_is_not_a_big_move():
    bars = [{"date": f"2026-01-{i+1:02d}", "adjclose": 100 + i * 0.1, "close": 100 + i * 0.1,
             "open": 100, "high": 101, "low": 99, "volume": 1000} for i in range(30)]
    out = move_profile(bars, [])
    assert out["big_move"] is False


# --------------------------------------------------------------- exit flags


def test_invalidation_breach_raises_a_high_severity_exit():
    rec = {"invalidation_price": 5.0, "invalidation": "thesis dead below 5"}
    flags = exit_signals(rec, {}, last=4.5, hard=[], resolved=[], last_alert=None)
    assert any(f["kind"] == "invalidation_breached" and f["severity"] == "high" for f in flags)


def test_no_exit_flag_above_the_invalidation_level():
    rec = {"invalidation_price": 5.0, "invalidation": ""}
    flags = exit_signals(rec, {}, last=5.5, hard=[], resolved=[], last_alert=None)
    assert not any(f["kind"] == "invalidation_breached" for f in flags)


def test_resolved_catalyst_prompts_a_re_underwrite():
    flags = exit_signals({}, {}, last=10.0, hard=[], last_alert=None,
                         resolved=[{"date": "2026-08-01", "kind": "PDUFA",
                                    "description": "action date", "days_ago": 3}])
    assert any(f["kind"] == "catalyst_resolved" and f["severity"] == "high" for f in flags)


# ---------------------------------------------------------------- conviction


def test_conviction_counts_evidence_on_both_sides():
    out = {"capitulation_volume": True,
           "insiders": {"cluster_buy": True, "distinct_buyers": 3, "buy_value_usd": 100_000},
           "runway": {"quarters": 8.0, "stale": False},
           "relative_strength": {}, "short": {}, "tradability": {}, "technicals": {"rsi14": 30}}
    good = conviction(out, hard=[], soft=[], catalyst_soon=True)
    assert good["label"] in ("strong", "moderate") and good["score"] > 0

    vetoed = conviction({**out, "capitulation_volume": False},
                        hard=[{"reason": "priced offering"}, {"reason": "delisting"}],
                        soft=[], catalyst_soon=False)
    assert vetoed["score"] < good["score"]
