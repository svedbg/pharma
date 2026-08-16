"""The veto layer and the data guards protecting it.

These cover the failures that produced genuinely wrong output during
development: liquidity understated tenfold, float ratios of 15,000%, a
combined-tag double count, and an ATM programme treated as a priced offering.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from signals import (
    RUNWAY_VETO_QUARTERS,
    conviction,
    evaluate_filings,
    exit_signals,
    financial_vetoes,
    float_metrics,
    load_catalysts,
    move_profile,
    resolved_catalysts,
    runway,
    tradability,
)

# A fixed session to measure against, so these tests describe the veto windows
# rather than the day they happen to run on. Every age below is relative to it,
# which is exactly the relationship the code now enforces.
SESSION = date(2026, 8, 15)


def _recent(days_ago: int) -> str:
    """A filing date `days_ago` before the session under test."""
    return (SESSION - timedelta(days=days_ago)).isoformat()


# ------------------------------------------------------------------ filings


def test_priced_offering_is_a_hard_veto():
    hard, soft = evaluate_filings([
        {"form": "424B5", "filed": _recent(2), "items": "",
         "offering_type": "priced", "url": "u"}], SESSION)
    assert len(hard) == 1 and not soft


def test_atm_programme_is_only_a_soft_flag():
    """An at-the-market programme is registered capacity, not a discounted
    takedown. Treating them alike produced the most frequent false veto."""
    hard, soft = evaluate_filings([
        {"form": "424B5", "filed": _recent(2), "items": "",
         "offering_type": "atm", "url": "u"}], SESSION)
    assert not hard
    assert len(soft) == 1 and "at-the-market" in soft[0]["reason"]


def test_unclassified_offering_stays_a_hard_veto():
    # Unknown must fail safe toward caution, not toward permission.
    hard, _ = evaluate_filings([
        {"form": "424B5", "filed": _recent(2), "items": "", "url": "u"}], SESSION)
    assert len(hard) == 1


def test_a_resale_prospectus_is_a_soft_flag_and_not_a_dilution_veto():
    """424B7 starts with 424B and was matching the hard loop as well as its own
    soft one, so every resale registration carried a priced-takedown veto next
    to a soft flag explaining it was only a resale -- advice the reader could
    never act on, since the veto beside it had already blocked the name.

    A 424B7 registers shares existing holders already own. Whatever dilution
    preceded it arrives on its own hard veto as 8-K item 3.02.
    """
    hard, soft = evaluate_filings(
        [{"form": "424B7", "filed": _recent(2), "items": "", "url": "u"}], SESSION)
    assert hard == []
    assert [s["form"] for s in soft] == ["424B7"]
    assert "resale" in soft[0]["reason"]


def test_a_priced_supplement_is_still_a_hard_veto_alongside_it():
    """The narrowing above must not reach 424B3/424B5."""
    hard, _ = evaluate_filings(
        [{"form": "424B5", "filed": _recent(2), "items": "", "url": "u"}], SESSION)
    assert [h["form"] for h in hard] == ["424B5"]


def test_listing_deficiency_is_a_hard_veto():
    hard, _ = evaluate_filings([
        {"form": "8-K", "filed": _recent(5), "items": "3.01", "url": "u"}], SESSION)
    assert any("listing" in h["reason"] for h in hard)


def test_stale_filings_fall_out_of_the_window():
    hard, soft = evaluate_filings([
        {"form": "424B5", "filed": _recent(400), "items": "", "url": "u"}], SESSION)
    assert not hard and not soft


def test_filing_age_is_measured_from_the_session_not_the_wall_clock():
    """The same filing is inside or outside its veto window depending only on
    which session is being analysed. Reading the clock instead meant re-running
    signals.py against an existing snapshot -- the documented way to pick up a
    watchlist edit without waiting for a fetch -- silently aged every window by
    however long ago that fetch was."""
    filing = [{"form": "424B5", "filed": "2026-08-13", "items": "",
               "offering_type": "priced", "url": "u"}]

    # Two days after the filing: inside the 10-day prospectus window.
    hard, _ = evaluate_filings(filing, date(2026, 8, 15))
    assert len(hard) == 1
    assert hard[0]["days_ago"] == 2

    # Thirty days after: the same filing, out of the window.
    hard_later, _ = evaluate_filings(filing, date(2026, 9, 12))
    assert not hard_later


def test_an_unreadable_filing_date_is_ancient_but_an_unreadable_asof_raises():
    """The guard in _days_ago covers one of those and must not cover the other.

    An unreadable *filing* date has a safe answer: treat it as ancient, so it
    cannot hold a veto open on a date nobody can read. An unreadable *asof* is a
    programming error, and swallowing it would age every filing to 10**6 days
    and drop every veto in the run -- a fail-open on the one layer that must
    never fail open.
    """
    filing = [{"form": "424B5", "filed": "not-a-date", "items": "",
               "offering_type": "priced", "url": "u"}]
    hard, soft = evaluate_filings(filing, SESSION)
    assert not hard and not soft

    with pytest.raises(TypeError):
        evaluate_filings([{"form": "424B5", "filed": "2026-08-13", "items": "",
                           "offering_type": "priced", "url": "u"}], None)


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


def test_cash_flow_positive_company_has_no_quarters_but_is_not_unknown():
    """A company generating cash has no burn rate, so no runway figure -- but it
    is the strongest financing position on the list, not a missing number.

    Returning None conflated the two, and `runway_ok` reads None as ignorance:
    the safest balance sheet here needed a catalyst inside 90 days to reach ACT
    while a company with three quarters of cash did not. Trap 5 in the same
    direction: absence of data is not distress, and good news is not absence.
    """
    rw = runway(_fin(50_000_000, 0, quarterly_burn=+10_000_000))
    assert rw is not None
    assert rw["cash_flow_positive"] is True
    # No number may be invented for any of these.
    assert rw["quarters"] is None
    assert rw["months"] is None
    assert rw["estimated_exhaustion"] is None


def test_a_cash_generative_name_can_reach_act_without_a_catalyst():
    """The end-to-end consequence of the above: the ACT financing backdrop."""
    rw = runway(_fin(50_000_000, 0, quarterly_burn=+10_000_000))
    assert not rw["stale"] and not rw.get("superseded_by")
    assert rw["cash_flow_positive"] or rw["quarters"] >= 3, \
        "this is the expression runway_ok evaluates"


def test_a_cash_generative_name_fires_no_financing_veto():
    """It must also not fall through into the short-runway branch, which used to
    be unreachable only because runway() returned None."""
    out = {"recent_events": [],
           "runway": runway(_fin(50_000_000, 0, +10_000_000, age=40))}
    hard, soft = [], []
    financial_vetoes(out, {"filings": []}, hard, soft, {}, SESSION)
    assert not any(h["form"] == "cash runway" for h in hard), hard


def test_stale_balance_sheet_is_flagged_not_trusted():
    rw = runway(_fin(10_000_000, 0, -10_000_000, age=400))
    assert rw["stale"] is True


# -------------------------------------------------- runway as a hard veto


def _veto_run(filings, cash=5_000_000, burn=-10_000_000, as_of="2026-03-31",
              settings=None):
    """Run financial_vetoes over one short-runway name and return (hard, soft)."""
    out = {"recent_events": [], "runway": runway(_fin(cash, 0, burn, as_of=as_of, age=40))}
    hard, soft = [], []
    financial_vetoes(out, {"filings": filings}, hard, soft, settings or {}, SESSION)
    return hard, soft, out["runway"]


def test_short_runway_is_a_hard_veto_when_the_balance_sheet_is_current():
    hard, _soft, rw = _veto_run(filings=[])
    assert rw["quarters"] < RUNWAY_VETO_QUARTERS
    assert any(h["form"] == "cash runway" for h in hard)


def test_the_runway_veto_bar_is_configurable_like_the_one_that_clears_a_name():
    """It was a bare 1.5 inside financial_vetoes while its counterpart,
    min_runway_quarters_for_act, sat in watchlist.toml [settings].

    The looser rule -- the bar for *clearing* a name -- was the one you could
    see and tune; the one that blocks a name outright was neither. They have to
    move together to stay ordered, so they have to be settable the same way.
    """
    # 0.5 quarters of liquidity: vetoed by default, and not at a lower bar.
    hard, _soft, rw = _veto_run(filings=[], settings={"runway_veto_quarters": 0.25})
    assert rw["quarters"] < RUNWAY_VETO_QUARTERS, "test setup"
    assert not any(h["form"] == "cash runway" for h in hard), hard

    hard, _soft, _rw = _veto_run(filings=[], settings={"runway_veto_quarters": 4.0})
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


# ------------------------------------------------------- catalyst clock


def _catalyst_file(tmp_path, when: str):
    p = tmp_path / "catalysts.toml"
    p.write_text('[[catalyst]]\nsymbol = "X"\n'
                 f'date = "{when}"\nkind = "PDUFA"\n'
                 'confidence = "confirmed"\ndescription = "action date"\n')
    return p


def test_a_catalyst_on_the_session_date_is_still_upcoming(tmp_path):
    """days_until 0, not a resolved binary. The two lists are exclusive and the
    boundary belongs to the upcoming one -- on the morning of a PDUFA the desk
    should be counting down, not writing the thesis off."""
    path = _catalyst_file(tmp_path, "2026-08-15")
    upcoming = load_catalysts(path, date(2026, 8, 15))
    assert upcoming["X"][0]["days_until"] == 0
    assert not resolved_catalysts(path, date(2026, 8, 15))


def test_a_catalyst_resolves_relative_to_the_session(tmp_path):
    path = _catalyst_file(tmp_path, "2026-08-15")
    # Three days later it has resolved and is no longer on the clock.
    assert resolved_catalysts(path, date(2026, 8, 18))["X"][0]["days_ago"] == 3
    assert "X" not in load_catalysts(path, date(2026, 8, 18))
    # Past the 21-day re-underwrite window it drops out of both.
    assert not resolved_catalysts(path, date(2026, 9, 30))


def test_an_unpadded_past_date_does_not_become_a_permanent_countdown(tmp_path):
    """Every filter on this file is a string comparison, and '2026-8-01' sorts
    above '2026-08-16' because '8' > '0'.

    So a catalyst that had already happened was never filtered out as history,
    never picked up by resolved_catalysts, and printed forever as "CATALYST IN
    -15 DAYS ... size for the outcome, not the chart" -- the strongest sizing
    instruction in the report, on a binary that already resolved. strptime
    accepts the unpadded form, so this is a real date typed slightly wrong,
    not a broken one; normalising it is what makes lexical order date order.
    """
    path = _catalyst_file(tmp_path, "2026-8-01")
    asof = date(2026, 8, 16)
    assert "X" not in load_catalysts(path, asof), "a past catalyst is not upcoming"
    assert resolved_catalysts(path, asof)["X"][0]["days_ago"] == 15


def test_an_unpadded_future_date_is_normalised_rather_than_dropped(tmp_path):
    """The same date typed the same way, but still ahead: it has to survive, and
    it has to come back in canonical form so later sorts agree with it."""
    got = load_catalysts(_catalyst_file(tmp_path, "2026-9-01"), date(2026, 8, 16))
    assert got["X"][0]["date"] == "2026-09-01"
    assert got["X"][0]["days_until"] == 16


def test_an_unquoted_toml_date_does_not_take_the_run_down(tmp_path):
    """`date = 2026-09-15` without quotes is a TOML date literal, so tomllib
    hands back a datetime.date and every `d < today` below raised TypeError --
    one missing pair of quotes in a hand-edited file killed the whole run."""
    p = tmp_path / "catalysts.toml"
    p.write_text('[[catalyst]]\nsymbol = "X"\ndate = 2026-09-15\nkind = "PDUFA"\n')
    got = load_catalysts(p, date(2026, 8, 16))
    assert got["X"][0]["date"] == "2026-09-15"
    assert got["X"][0]["days_until"] == 30


def test_an_unreadable_catalyst_date_is_dropped_with_a_warning(tmp_path, capsys):
    """Not silently, and not by inventing a date: an unsourced or unparseable
    catalyst must not gate sizing in either direction."""
    p = tmp_path / "catalysts.toml"
    p.write_text('[[catalyst]]\nsymbol = "X"\ndate = "H2 2026"\nkind = "PDUFA"\n')
    assert load_catalysts(p, date(2026, 8, 16)) == {}
    assert "unreadable date" in capsys.readouterr().err


def test_conviction_still_counts_a_superseded_balance_sheet_against_the_name():
    """The demotion above must not make the shortfall invisible to the checklist."""
    out = {"runway": {"quarters": 0.6, "stale": False, "superseded_by": {"form": "10-Q"}},
           "relative_strength": {}, "short": {}, "tradability": {}, "technicals": {},
           "insiders": {}}
    assert any("superseded" in m for m in conviction(out, [], False)["against"])


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
    out = tradability(bars)
    assert out["very_illiquid"] is True
    assert out["comfortable_position_usd"] == pytest.approx(1500, rel=0.1)


def test_tradability_passes_a_liquid_name():
    bars = [{"close": 50.0, "volume": 1_000_000} for _ in range(20)]
    out = tradability(bars)
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
    good = conviction(out, hard=[], catalyst_soon=True)
    assert good["label"] in ("strong", "moderate") and good["score"] > 0

    vetoed = conviction({**out, "capitulation_volume": False},
                        hard=[{"reason": "priced offering"}, {"reason": "delisting"}],
                        catalyst_soon=False)
    assert vetoed["score"] < good["score"]


def test_being_funded_by_operations_counts_in_its_favour():
    """The checklist scored cash-flow-positive names as having no liquidity
    evidence at all, because runway() handed it None."""
    out = {"runway": runway(_fin(50_000_000, 0, quarterly_burn=+10_000_000)),
           "relative_strength": {}, "short": {}, "tradability": {}, "technicals": {},
           "insiders": {}}
    assert any("operations" in p for p in conviction(out, [], False)["supporting"])
