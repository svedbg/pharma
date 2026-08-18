"""Characterisation tests for analyse().

analyse() is the function every signal flows through and, at 360 lines, was the
riskiest thing in the codebase to change. These pin its observable contract so
it can be restructured safely: they assert the shape and the decision rules, not
the internals.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta

import pytest

import publish
import signals

SETTINGS = {"max_position_pct": 28, "max_position_pct_lottery": 5,
            "min_cash_reserve_pct": 15, "min_runway_quarters_for_act": 3}
# _bars() lays its fixtures out from 2026-01-01, so this sits just past the
# last of them. analyse() requires a session rather than defaulting to today:
# a test whose answer depends on the day it runs is a flake waiting for a
# threshold to drift underneath it.
SESSION = date(2026, 6, 1)


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
    out = signals.analyse(_rec([10.0] * 5), SETTINGS, asof=SESSION)
    assert out["tier"] == "NO_DATA"
    assert "insufficient" in out["reasons"][0]


def test_output_carries_the_keys_the_report_depends_on():
    """The report and the email renderer index these directly; a rename here
    silently empties sections downstream."""
    out = signals.analyse(_rec([10.0 + (i % 9) * 0.3 for i in range(120)]), SETTINGS, asof=SESSION)
    for key in ("symbol", "bucket", "tier", "reasons", "price", "technicals",
                "max_position_pct", "hard_vetoes", "soft_flags", "recent_events",
                "conviction", "exit_flags", "links", "links_md", "tradability"):
        assert key in out, f"missing key: {key}"
    for key in ("close", "date", "chg_1d_pct", "percentile_1y", "pct_off_52w_high"):
        assert key in out["price"]
    for key in ("rsi14", "bollinger_pct_b", "volume_vs_20d_avg"):
        assert key in out["technicals"]


def test_lottery_bucket_gets_the_smaller_ceiling():
    out = signals.analyse(_rec([10.0] * 40 + [10.5] * 40, tier="lottery"), SETTINGS, asof=SESSION)
    assert out["max_position_pct"] == 5
    assert signals.analyse(_rec([10.0] * 80, tier="A"), SETTINGS, asof=SESSION)["max_position_pct"] == 28


def test_a_hard_veto_blocks_setup():
    """An oversold name carrying a veto must degrade to WATCH, never SETUP."""
    falling = [100 - i * 1.5 for i in range(60)]
    veto = [{"form": "424B5", "filed": "2026-01-01", "items": "",
             "offering_type": "priced", "url": "u"}]
    clean = signals.analyse(_rec(falling), SETTINGS, asof=SESSION)
    vetoed = signals.analyse(_rec(falling, filings=veto), SETTINGS, asof=SESSION)
    assert vetoed["tier"] != "ACT"
    assert len(vetoed["hard_vetoes"]) >= len(clean["hard_vetoes"])


def test_act_requires_a_declared_zone():
    """Without entry_high the ACT tier is structurally unreachable, and the
    reasons must say so rather than failing silently."""
    falling = [100 - i * 1.5 for i in range(60)]
    out = signals.analyse(_rec(falling), SETTINGS, asof=SESSION)
    assert out["tier"] != "ACT"
    if out["tier"] == "SETUP":
        assert any("no entry zone" in r for r in out["reasons"])


def test_stale_zone_is_flagged_as_a_soft_flag():
    prices = [10.0] * 60 + [20.0] * 20          # price far above its zone
    out = signals.analyse(_rec(prices, entry_high=10.0, entry_low=8.0), SETTINGS, asof=SESSION)
    assert out["zone_stale"] is True
    flag = next(f for f in out["soft_flags"] if f["form"] == "stale entry zone")
    # Above the zone, in_zone is False and ACT genuinely cannot fire.
    assert "ACT cannot fire" in flag["reason"]


def test_a_zone_stale_to_the_downside_does_not_claim_to_block_act():
    """`zone_stale` asserted "ACT cannot fire for this name" in both directions,
    but it only gates the upward one.

    Below the zone `in_zone` is True and nothing in the ACT condition consults
    `zone_stale`, so the name reaches ACT carrying a flag that flatly denies it
    -- and downward is the direction this desk actually hunts. Whether a name
    that cheap is broken is the veto layer's question, not the zone's, so the
    behaviour is right and only the sentence was wrong.
    """
    prices = [100.0 * (0.985**i) for i in range(90)]
    bars = _bars(prices)
    bars[-1]["volume"] = 4_000_000               # capitulation, so ACT is reachable
    rec = _rec(prices, entry_high=round(prices[-1] / 0.70, 4), entry_low=1.0)
    rec["bars"] = bars
    rec["financials"] = {"liquidity": {"total_usd": 9e8, "as_of": "2026-03-31",
                                       "age_days": 20},
                         "burn": {"quarterly_usd": -3e7}}
    out = signals.analyse(rec, SETTINGS, asof=date(2026, 4, 1))

    assert out["zone_stale"] is True and out["zone_drift_pct"] < 0
    assert out["tier"] == "ACT", "test setup: this is the case the flag denied"
    flag = next(f for f in out["soft_flags"] if f["form"] == "stale entry zone")
    assert "ACT cannot fire" not in flag["reason"], flag["reason"]
    assert "ACT can still fire" in flag["reason"]


def _act_ready(entry_high_ratio=0.70):
    """A record that reaches ACT: oversold, in zone, funded, volume-confirmed.

    Shared by the regime tests below so the only thing varying between them is
    the regime and the conviction score.
    """
    prices = [100.0 * (0.985**i) for i in range(90)]
    bars = _bars(prices)
    bars[-1]["volume"] = 4_000_000                # capitulation
    rec = _rec(prices, entry_high=round(prices[-1] / entry_high_ratio, 4), entry_low=1.0)
    rec["bars"] = bars
    rec["financials"] = {"liquidity": {"total_usd": 9e8, "as_of": "2026-03-31",
                                       "age_days": 20},
                         "burn": {"quarterly_usd": -3e7}}
    rec["insiders"] = {"distinct_buyers": 3, "buy_value_usd": 900_000,
                       "sell_value_usd": 0, "net_value_usd": 900_000,
                       "lookback_days": 120, "buys": []}
    return rec


def test_a_falling_sector_raises_the_bar_to_act_rather_than_blocking():
    """One of the two conditions CLAUDE.md's tier list did not describe.

    In a downtrend the same setup is a worse bet, so ACT demands a `strong`
    conviction score -- the signal stays visible at SETUP rather than
    disappearing. Nothing tested this, on a gate that decides whether the desk's
    only actionable tier fires at all.
    """
    rec = _act_ready()
    moderate = signals.analyse(rec, SETTINGS, regime={"label": "downtrend"},
                               asof=date(2026, 4, 1))
    assert moderate["conviction"]["label"] != "strong", "test setup"
    assert moderate["tier"] == "SETUP", "a downtrend must hold this back from ACT"
    assert any("falling sector" in r for r in moderate["reasons"]), moderate["reasons"]

    # The identical name in a rising sector is actionable, so the regime is what
    # made the difference and not some other half of the ACT condition.
    for label in ("uptrend", "mixed"):
        out = signals.analyse(_act_ready(), SETTINGS, regime={"label": label},
                              asof=date(2026, 4, 1))
        assert out["tier"] == "ACT", label


def test_a_strong_conviction_still_reaches_act_in_a_downtrend():
    """The bar rises, it does not close. A downtrend that blocked ACT outright
    would make the whole tier unreachable for months at a time."""
    cats = {"TEST": [{"date": "2026-05-15", "days_until": 44, "kind": "PDUFA",
                      "description": "decision"}]}
    out = signals.analyse(_act_ready(), SETTINGS, catalysts=cats,
                          regime={"label": "downtrend"}, asof=date(2026, 4, 1))
    assert out["conviction"]["label"] == "strong", out["conviction"]
    assert out["tier"] == "ACT"


def test_market_regime_reads_the_benchmark_against_its_own_200_day_average():
    """`regime` gates ACT and had no test at all.

    A downtrend is simply "below the 200-day"; an uptrend additionally requires
    that average to be *rising*, so a dead-cat bounce back above a still-falling
    line reads as mixed rather than as a recovery.
    """
    rising = [{"adjclose": 100.0 + i * 0.2} for i in range(260)]
    assert signals.market_regime(rising)["label"] == "uptrend"

    falling = [{"adjclose": 200.0 - i * 0.2} for i in range(260)]
    down = signals.market_regime(falling)
    assert down["label"] == "downtrend"
    assert down["pct_vs_sma200"] < 0
    assert down["benchmark"] == signals.BENCHMARK

    # Above a 200-day average that is still falling: not yet an uptrend.
    bounce = [{"adjclose": 200.0 - i * 0.2} for i in range(250)]
    bounce += [{"adjclose": 200.0} for _ in range(10)]
    assert signals.market_regime(bounce)["label"] == "mixed"

    # Too little history is unknown, never a label to act on.
    assert signals.market_regime([{"adjclose": 100.0}] * 50) is None


def test_analyse_refuses_to_guess_which_session_it_is_analysing():
    """Every other function here that measures an age requires `asof`; this one
    defaulted to today, and it is the function every signal flows through.

    Its own docstring conceded the default "is what makes this function's
    determinism conditional". Nothing in the output records which clock produced
    a `days_ago`, so an ad-hoc caller did not get a slightly inconvenient
    answer -- it got a different one on Tuesday than on Monday, silently.
    """
    with pytest.raises(TypeError):
        signals.analyse(_rec([10.0] * 80), SETTINGS)


def test_conviction_is_always_present_and_labelled():
    out = signals.analyse(_rec([10.0 + (i % 5) * 0.2 for i in range(120)]), SETTINGS, asof=SESSION)
    assert out["conviction"]["label"] in ("strong", "moderate", "weak", "avoid")
    assert isinstance(out["conviction"]["supporting"], list)
    assert isinstance(out["conviction"]["against"], list)


def test_deterministic_for_the_same_input():
    """Stage 2 must be pure arithmetic: identical input, identical output."""
    rec = _rec([10.0 + (i % 11) * 0.4 for i in range(150)])
    a = json.dumps(signals.analyse(rec, SETTINGS, asof=SESSION), sort_keys=True, default=str)
    b = json.dumps(signals.analyse(rec, SETTINGS, asof=SESSION), sort_keys=True, default=str)
    assert a == b


def test_a_sourced_catalyst_satisfies_the_act_backdrop():
    """ACT accepts either a funded balance sheet or a near catalyst. That test
    used to consult only ClinicalTrials.gov primary-completion dates -- sponsor
    estimates that slip constantly -- while catalysts.toml, where every entry is
    dated and carries a source because the daily run may not invent one, was
    ignored. The trustworthy calendar was the one that could not open the gate.
    """
    prices = [100.0 * (0.985**i) for i in range(90)]
    vols = [1_000_000] * 89 + [4_000_000]          # capitulation volume on the last bar
    bars = _bars(prices)
    for b, v in zip(bars, vols, strict=True):
        b["volume"] = v
    rec = _rec(prices, entry_high=round(prices[-1] * 1.2, 4), entry_low=1.0)
    rec["bars"] = bars

    # Pinned to a fixed session like the rest of the date-sensitive tests, so
    # this describes the ACT gate rather than the day the suite runs on.
    session = date(2026, 8, 15)
    soon = (session + timedelta(days=30)).isoformat()
    catalysts = {"TEST": [{"symbol": "TEST", "date": soon, "days_until": 30,
                           "kind": "PDUFA", "confidence": "confirmed",
                           "description": "action date", "source": "8-K"}]}

    withc = signals.analyse(rec, SETTINGS, catalysts=catalysts, asof=session)
    without = signals.analyse(rec, SETTINGS, asof=session)
    assert withc["capitulation_volume"] is True, "test setup: needs volume confirmation"
    assert withc["tier"] == "ACT"
    # Nothing else changed, so the catalyst is demonstrably what carried it.
    assert without["tier"] == "SETUP"


def test_invalidation_price_reaches_analyse_through_the_watchlist_overlay(monkeypatch, tmp_path):
    """A hand-set stop must reach exit_signals(), end to end.

    `invalidation_breached` is the highest-severity exit flag and the one that
    pushes to ntfy, but nothing ever put `invalidation_price` on a ticker record:
    fetch.py did not store it and the watchlist overlay did not copy it. 57 of 59
    names carried a stop and none of them could ever fire. The unit test on
    exit_signals() passed throughout, because it built the record by hand -- so
    this one drives main() over the real overlay instead.

    The snapshot below deliberately omits the field, which is the case that
    matters: the overlay exists precisely so a hand edit takes effect without
    waiting for the next fetch.
    """
    monkeypatch.setattr(signals, "ROOT", tmp_path)
    monkeypatch.setattr(signals, "DATA", tmp_path)
    monkeypatch.setattr(signals, "STATE", tmp_path)

    prices = [10.0 - i * 0.05 for i in range(60)]
    snapshot = {
        "local_date": "2026-08-15",
        "status": "ok",
        "benchmarks": {},
        "tickers": {"TEST": {"symbol": "TEST", "tier": "A", "filings": [],
                             "financials": {}, "trials": [],
                             "bars": _bars(prices)}},
    }
    snap_path = tmp_path / "latest.json"
    snap_path.write_text(json.dumps(snapshot))

    last = prices[-1]
    watchlist = tmp_path / "watchlist.toml"
    watchlist.write_text(
        "[settings]\nmax_position_pct = 28\n\n"
        '[[ticker]]\nsymbol = "TEST"\ntier = "A"\n'
        f"invalidation_price = {last + 1.0}\n"
        'invalidation = "thesis dead below here"\n'
    )

    out_path = tmp_path / "signals.json"
    monkeypatch.setattr("sys.argv", [
        "signals.py", "--snapshot", str(snap_path), "--watchlist", str(watchlist),
        "--out", str(out_path), "--state", str(tmp_path / "test_state.json"),
    ])
    assert signals.main() == 0

    result = json.loads(out_path.read_text())
    row = result["signals"][0]
    assert any(f["kind"] == "invalidation_breached" and f["severity"] == "high"
               for f in row["exit_flags"]), row["exit_flags"]
    # And it must reach the notification payload, not just the JSON.
    assert result["notify_exits"], "a breached stop has to leave the file"
    assert "invalidation_breached" in result["notify_exits"][0]["flags"]


def _run_main(monkeypatch, tmp_path, snapshot: dict,
              state_name: str = "candidate_alerts.json") -> dict:
    """Drive main() over one snapshot and return the signals it wrote.

    `state_name` is what tells signals.py whether the run is live: anything but
    alerts.json is a screening pass, which gates the alert log and the archive
    summary. It defaults to a non-live name so these tests never write into the
    shared artefacts, even though DATA is redirected at tmp_path anyway.
    """
    monkeypatch.setattr(signals, "ROOT", tmp_path)
    monkeypatch.setattr(signals, "DATA", tmp_path)
    monkeypatch.setattr(signals, "STATE", tmp_path)
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(snapshot))
    watchlist = tmp_path / "watchlist.toml"
    watchlist.write_text('[settings]\n\n[[ticker]]\nsymbol = "TEST"\n')
    out_path = tmp_path / "signals.json"
    monkeypatch.setattr("sys.argv", [
        "signals.py", "--snapshot", str(snap_path), "--watchlist", str(watchlist),
        "--out", str(out_path), "--state", str(tmp_path / state_name),
    ])
    assert signals.main() == 0
    return json.loads(out_path.read_text())


def test_a_screening_run_leaves_no_trace_in_the_live_archive(monkeypatch, tmp_path):
    """Screening borrows this module against a candidate file. CLAUDE.md already
    required --state so it cannot consume the live alert state, but the per-day
    summary the local archive indexes from was written unconditionally, so a
    screen silently overwrote the real day's entry."""
    prices = [10.0 + (i % 7) * 0.2 for i in range(60)]
    _run_main(monkeypatch, tmp_path, {
        "local_date": "2026-08-15", "status": "ok", "benchmarks": {},
        "tickers": {"TEST": {"symbol": "TEST", "tier": "B", "filings": [],
                             "financials": {}, "trials": [], "bars": _bars(prices)}},
    }, state_name="screen_alerts.json")
    assert not (tmp_path / "summaries").exists(), \
        "a screening run must not write the live archive's per-day summary"


def _summary_snapshot(filings):
    """One name, calm prices, with whatever filings the caller wants -- enough to
    change the archive summary's `vetoed` list without changing anything else."""
    prices = [10.0 + (i % 7) * 0.2 for i in range(60)]
    return {
        "local_date": "2026-08-14", "status": "ok", "benchmarks": {},
        "tickers": {"TEST": {"symbol": "TEST", "tier": "B", "financials": {},
                             "trials": [], "bars": _bars(prices),
                             "filings": filings}},
    }


def test_a_second_run_on_the_same_session_displaces_the_summary_it_replaces(
        monkeypatch, tmp_path, capsys):
    """The session is the newest bar, not the run date, so two runs on different
    days land on the same summary whenever one wins the race with the provider
    and the next loses it.

    That happened: Friday's run wrote the 2026-08-14 pair, Monday's run analysed
    the same Friday bar and overwrote both halves. reports/ and data/ are
    gitignored, so the displaced version was recoverable only from the email it
    had already been sent in. Keep the canonical name for the newest, because
    publish.py pairs report and summary by filename, and keep the old one.
    """
    _run_main(monkeypatch, tmp_path, _summary_snapshot([]),
              state_name="alerts.json")
    first = json.loads((tmp_path / "summaries" / "2026-08-14.json").read_text())
    assert first["vetoed"] == []

    _run_main(monkeypatch, tmp_path, _summary_snapshot(
        [{"form": "424B5", "filed": "2026-08-13", "items": "",
          "offering_type": "priced", "report_date": None, "url": "u"}],
    ), state_name="alerts.json")

    live = json.loads((tmp_path / "summaries" / "2026-08-14.json").read_text())
    assert live["vetoed"] == ["TEST"], "the canonical name must hold the newest run"

    kept = sorted((tmp_path / "summaries" / "superseded").glob("*.json"))
    assert len(kept) == 1, kept
    assert re.fullmatch(r"2026-08-14\.written-\d{8}T\d{6}\.json", kept[0].name), \
        kept[0].name
    assert json.loads(kept[0].read_text()) == first, \
        "the displaced summary must survive verbatim"
    assert "superseded/" in capsys.readouterr().err


def test_re_running_an_unchanged_session_leaves_no_superseded_copy(
        monkeypatch, tmp_path):
    """superseded/ means "a record that said something else". Re-running the same
    snapshot -- which this project recommends, to pick up a watchlist edit --
    must not fill it with copies of the live file, or the versions that do differ
    are buried among them."""
    _run_main(monkeypatch, tmp_path, _summary_snapshot([]),
              state_name="alerts.json")
    _run_main(monkeypatch, tmp_path, _summary_snapshot([]),
              state_name="alerts.json")
    assert not (tmp_path / "summaries" / "superseded").exists()


def test_the_archive_indexes_one_page_per_session_not_the_displaced_versions(
        monkeypatch, tmp_path):
    """The displaced reports live under reports/, so the archive builder has to
    ignore them -- twice over, since they sit in a subdirectory *and* carry a
    stem that is not a bare date. Otherwise every collision would add a second
    page for the same day, which is the archive disagreeing with itself about
    what happened."""
    reports = tmp_path / "reports"
    (reports / "superseded").mkdir(parents=True)
    (reports / "2026-08-14.md").write_text("# live\n\nthe newest analysis\n")
    (reports / "superseded" / "2026-08-14.written-20260814T233915.md").write_text(
        "# displaced\n\nFriday's analysis\n")
    monkeypatch.setattr(publish, "REPORTS", reports)
    monkeypatch.setattr(publish, "SUMMARIES", tmp_path / "summaries")
    monkeypatch.setattr(publish, "SITE", tmp_path / "site")

    assert publish.build() == 0
    pages = sorted(p.name for p in (tmp_path / "site").glob("*.html"))
    assert pages == ["2026-08-14.html", "index.html"], pages
    assert "the newest analysis" in (tmp_path / "site" / "2026-08-14.html").read_text()


def test_main_ages_filings_from_the_snapshot_not_from_today(monkeypatch, tmp_path):
    """The end-to-end proof that the session date, not the wall clock, sets the
    veto windows.

    The filing below is two days old as of the snapshot's own date and would be
    months old measured from now. Under the wall clock it fell out of the 10-day
    prospectus window and the veto silently disappeared -- which is what happened
    whenever signals.py ran on a different day from the fetch: re-running it to
    pick up a watchlist edit, pointing --snapshot at an archived file, or a run
    that straddled midnight.
    """
    prices = [10.0 + (i % 7) * 0.2 for i in range(60)]
    result = _run_main(monkeypatch, tmp_path, {
        "local_date": "2026-06-01", "status": "ok", "benchmarks": {},
        "tickers": {"TEST": {
            "symbol": "TEST", "tier": "A", "financials": {}, "trials": [],
            "bars": _bars(prices),
            "filings": [{"form": "424B5", "filed": "2026-05-30", "items": "",
                         "offering_type": "priced", "report_date": None, "url": "u"}],
        }},
    })
    vetoes = result["signals"][0]["hard_vetoes"]
    assert [v["form"] for v in vetoes] == ["424B5"], vetoes
    assert vetoes[0]["days_ago"] == 2


def test_main_says_so_when_a_snapshot_has_no_session_date(monkeypatch, tmp_path, capsys):
    """Falling back to today is sometimes the only option, but doing it quietly
    would reintroduce the very drift the session date removes."""
    prices = [10.0 + (i % 7) * 0.2 for i in range(60)]
    _run_main(monkeypatch, tmp_path, {
        "status": "ok", "benchmarks": {},
        "tickers": {"TEST": {"symbol": "TEST", "tier": "A", "filings": [],
                             "financials": {}, "trials": [], "bars": _bars(prices)}},
    })
    assert "no usable local_date" in capsys.readouterr().err


def _snapshot(local_date, prices):
    snap = {"status": "ok", "benchmarks": {},
            "tickers": {"TEST": {"symbol": "TEST", "tier": "A", "filings": [],
                                 "financials": {}, "trials": [], "bars": _bars(prices)}}}
    if local_date is not None:
        snap["local_date"] = local_date
    return snap


@pytest.mark.parametrize("local_date", ["not-a-date", "15/08/2026", "2026-08-32", ""])
def test_an_unreadable_session_date_never_reaches_the_output(
        monkeypatch, tmp_path, local_date):
    """A date the code cannot parse must not be passed along as if it could.

    `session_date` was taken straight from the snapshot while `asof` was the
    validated value, so an unreadable-but-truthy date flowed into signals.json,
    into a summary filename, and into the alerts table's primary key. From there
    it is unreadable in both directions: score_alerts.py compares it against bar
    dates, where a malformed string sorts above every ISO date and matches
    nothing, so the alert silently vanishes from the only number that says
    whether the desk works.
    """
    result = _run_main(monkeypatch, tmp_path,
                       _snapshot(local_date, [10.0 + (i % 7) * 0.2 for i in range(60)]))
    assert result["session_date"] is None
    assert not (tmp_path / "summaries").exists(), "no session, no date-keyed file"


def test_a_readable_session_date_is_passed_through_unchanged(monkeypatch, tmp_path):
    """The guard above must not disturb the ordinary case."""
    result = _run_main(monkeypatch, tmp_path,
                       _snapshot("2026-08-15", [10.0 + (i % 7) * 0.2 for i in range(60)]))
    assert result["session_date"] == "2026-08-15"


def _oversold_snapshot(local_date):
    """A snapshot whose single name is deep enough in the hole to raise SETUP,
    so main() has a real tier change to record."""
    prices = [20.0] * 30 + [20.0 - 0.4 * i for i in range(35)]
    snap = {"status": "ok", "benchmarks": {},
            "tickers": {"TEST": {"symbol": "TEST", "tier": "A", "filings": [],
                                 "financials": {}, "trials": [],
                                 "bars": _bars(prices)}}}
    if local_date is not None:
        snap["local_date"] = local_date
    return snap


def _alerts_db(tmp_path):
    """The table fetch.py creates, which signals.py logs into. Same DDL as
    scripts/fetch.py; without it signals.py warns and moves on, so a test that
    skipped this would prove nothing about what was recorded."""
    con = sqlite3.connect(tmp_path / "history.sqlite")
    con.execute("""CREATE TABLE IF NOT EXISTS alerts (
        session_date TEXT, ticker TEXT, tier TEXT, previous_tier TEXT,
        close REAL, rsi REAL, pctb REAL, capitulation INTEGER,
        vetoes INTEGER, excess_20d REAL, bucket TEXT, source TEXT, reason TEXT,
        context TEXT, PRIMARY KEY (session_date, ticker, source))""")
    con.commit()
    con.close()


def _live_main(monkeypatch, tmp_path, snapshot) -> dict:
    """Drive main() as a LIVE run -- state file named alerts.json, no
    --screening. The session-date tests above all run non-live, which is why
    nothing caught what happens to the live alert state below."""
    _alerts_db(tmp_path)
    monkeypatch.setattr(signals, "ROOT", tmp_path)
    monkeypatch.setattr(signals, "DATA", tmp_path)
    monkeypatch.setattr(signals, "STATE", tmp_path)
    (tmp_path / "snap.json").write_text(json.dumps(snapshot))
    (tmp_path / "watchlist.toml").write_text('[settings]\n\n[[ticker]]\nsymbol = "TEST"\n')
    monkeypatch.setattr("sys.argv", [
        "signals.py", "--snapshot", str(tmp_path / "snap.json"),
        "--watchlist", str(tmp_path / "watchlist.toml"),
        "--out", str(tmp_path / "signals.json"),
        "--state", str(tmp_path / "alerts.json"),
    ])
    assert signals.main() == 0
    return json.loads((tmp_path / "signals.json").read_text())


def test_a_run_with_no_session_does_not_consume_the_alert_it_cannot_record(
        monkeypatch, tmp_path):
    """The state file is what makes an alert fire once, so writing it commits to
    having recorded the transition.

    A run with no session date records nothing -- the alert log is keyed by
    session and skips it -- but the state write still advanced every tier, so
    the next run saw no change and never logged it either, while the
    notification had already gone out. Refusing to write the unreadable key and
    then consuming the transition anyway turned an ungradeable row into no row
    at all, which is a strictly worse outcome for the one number that says
    whether the desk works.
    """
    first = _live_main(monkeypatch, tmp_path, _oversold_snapshot(None))
    assert first["signals"][0]["tier"] in ("SETUP", "ACT"), "test setup"
    assert first["notify"], "the phone was still buzzed on this run"
    assert not (tmp_path / "alerts.json").exists(), \
        "a run that could not record what it raised must not consume it"
    assert sqlite3.connect(tmp_path / "history.sqlite").execute(
        "SELECT COUNT(*) FROM alerts").fetchone()[0] == 0, \
        "and it must still not write a row nobody can key on"

    # The next run, with a readable session, must still see this as new.
    second = _live_main(monkeypatch, tmp_path, _oversold_snapshot("2026-08-15"))
    assert second["notify"], "the deferred alert has to be raised by the next good run"
    logged = sqlite3.connect(tmp_path / "history.sqlite").execute(
        "SELECT session_date, ticker FROM alerts").fetchall()
    assert logged == [("2026-08-15", "TEST")], logged


def test_a_normal_live_run_still_advances_the_state(monkeypatch, tmp_path):
    """The guard above must not stop an ordinary run from deduplicating."""
    first = _live_main(monkeypatch, tmp_path, _oversold_snapshot("2026-08-15"))
    assert first["notify"]
    second = _live_main(monkeypatch, tmp_path, _oversold_snapshot("2026-08-15"))
    assert not second["notify"], "an unchanged tier must not buzz twice"


def test_screening_is_explicit_and_not_only_inferred_from_a_filename(
        monkeypatch, tmp_path):
    """Liveness was inferred from the state file's basename alone, so a
    screening pass that forgot --state was silently live and overwrote the real
    day's alert log and archive summary."""
    _alerts_db(tmp_path)
    monkeypatch.setattr(signals, "ROOT", tmp_path)
    monkeypatch.setattr(signals, "DATA", tmp_path)
    monkeypatch.setattr(signals, "STATE", tmp_path)
    (tmp_path / "snap.json").write_text(json.dumps(_oversold_snapshot("2026-08-15")))
    (tmp_path / "watchlist.toml").write_text('[settings]\n\n[[ticker]]\nsymbol = "TEST"\n')
    monkeypatch.setattr("sys.argv", [
        "signals.py", "--snapshot", str(tmp_path / "snap.json"),
        "--watchlist", str(tmp_path / "watchlist.toml"),
        "--out", str(tmp_path / "signals.json"),
        # The live-looking path, which on its own used to be enough.
        "--state", str(tmp_path / "alerts.json"), "--screening",
    ])
    assert signals.main() == 0
    assert not (tmp_path / "summaries").exists()
    assert sqlite3.connect(tmp_path / "history.sqlite").execute(
        "SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_two_catalysts_on_one_date_do_not_take_the_run_down(monkeypatch, tmp_path):
    """The summary's next-catalyst sort compared whole tuples, so a tie on both
    days_until and symbol fell through to comparing the catalyst dicts. Dicts
    are unorderable: that raised TypeError out of main() and cost the day's
    report. One ticker with a PDUFA and an AdCom on the same date is ordinary,
    and the daily run appends to this file itself."""
    monkeypatch.setattr(signals, "ROOT", tmp_path)
    monkeypatch.setattr(signals, "DATA", tmp_path)
    monkeypatch.setattr(signals, "STATE", tmp_path)
    (tmp_path / "catalysts.toml").write_text(
        '[[catalyst]]\nsymbol = "TEST"\ndate = "2026-09-15"\nkind = "PDUFA"\n'
        '[[catalyst]]\nsymbol = "TEST"\ndate = "2026-09-15"\nkind = "AdCom"\n')
    (tmp_path / "snap.json").write_text(json.dumps(_oversold_snapshot("2026-08-15")))
    (tmp_path / "watchlist.toml").write_text('[settings]\n\n[[ticker]]\nsymbol = "TEST"\n')
    monkeypatch.setattr("sys.argv", [
        "signals.py", "--snapshot", str(tmp_path / "snap.json"),
        "--watchlist", str(tmp_path / "watchlist.toml"),
        "--out", str(tmp_path / "signals.json"),
        "--state", str(tmp_path / "alerts.json"),
    ])
    assert signals.main() == 0
    summary = json.loads((tmp_path / "summaries" / "2026-08-15.json").read_text())
    assert summary["next_catalyst"]["symbol"] == "TEST"
    assert summary["next_catalyst"]["days_until"] == 31


def test_a_valid_but_unpadded_session_date_is_normalised(monkeypatch, tmp_path):
    """strptime accepts '2026-8-15', so it is a real date rather than a broken
    one -- but passing the raw field through meant it named a summary file that
    the archive, which looks these up by canonical YYYY-MM-DD, could never find.
    Deriving the output from the parsed value normalises it."""
    result = _run_main(monkeypatch, tmp_path,
                       _snapshot("2026-8-15", [10.0 + (i % 7) * 0.2 for i in range(60)]))
    assert result["session_date"] == "2026-08-15"


# ------------------------------------------------------- the drill-down view


def test_the_drill_down_shows_the_levels_a_decision_uses(monkeypatch, tmp_path, capsys):
    """detail.py is what the analysis pass reads for a name it looks at closely.

    It printed neither entry zone, nor invalidation level, nor exit flags, nor
    conviction -- the two numbers ACT and the exit flags actually turn on, and
    the two summaries of why. fetch.py carries invalidation_price into the
    snapshot with a comment saying it was added so this file could show it, and
    this file still did not. Driven end to end rather than unit-tested, for the
    same reason the overlay test above is: every stage of that chain looked fine
    on its own while the field never arrived.
    """
    import detail

    prices = [10.0 - i * 0.05 for i in range(60)]
    last = prices[-1]
    snapshot = {
        "local_date": "2026-08-15", "status": "ok", "benchmarks": {},
        "tickers": {"TEST": {"symbol": "TEST", "tier": "A", "company": "Test Bio",
                             "filings": [], "financials": {}, "trials": [],
                             "entry_low": 5.0, "entry_high": 9.0,
                             "invalidation_price": last + 1.0,
                             "invalidation": "thesis dead below here",
                             "bars": _bars(prices)}},
    }
    (tmp_path / "latest.json").write_text(json.dumps(snapshot))

    monkeypatch.setattr(signals, "ROOT", tmp_path)
    monkeypatch.setattr(signals, "DATA", tmp_path)
    monkeypatch.setattr(signals, "STATE", tmp_path)
    watchlist = tmp_path / "watchlist.toml"
    watchlist.write_text('[settings]\n\n[[ticker]]\nsymbol = "TEST"\n')
    monkeypatch.setattr("sys.argv", [
        "signals.py", "--snapshot", str(tmp_path / "latest.json"),
        "--watchlist", str(watchlist), "--out", str(tmp_path / "signals.json"),
        "--state", str(tmp_path / "candidate_alerts.json"),
    ])
    assert signals.main() == 0
    capsys.readouterr()

    monkeypatch.setattr(detail, "DATA", tmp_path)
    monkeypatch.setattr("sys.argv", ["detail.py", "TEST"])
    assert detail.main() == 0
    out = capsys.readouterr().out

    assert "zone $5.0 - $9.0" in out
    assert "IN ZONE" in out
    assert f"invalidation ${last + 1.0}" in out
    assert "conviction:" in out
    assert "EXIT (high) invalidation_breached" in out
    # The truncated copy analyse() appends to `reasons` must not print alongside
    # the full one.
    assert out.count("invalidation_breached") == 1


# ------------------------------------------------------------------ exposure


def test_exposure_adds_up_what_every_actionable_name_would_ask_for(
        monkeypatch, tmp_path):
    """Nothing else computes this, and nothing tested it.

    Several correlated names fire together in a drawdown -- that is the normal
    case, not the edge case -- and each sized at its bucket cap can commit more
    than the whole book. SETUP is counted alongside ACT deliberately: this is
    the worst case, not a buy list, and `tiers_counted` publishes that so the
    report cannot mistake it for a committed figure.
    """
    prices = [100.0 * (0.985**i) for i in range(90)]
    monkeypatch.setattr(signals, "ROOT", tmp_path)
    monkeypatch.setattr(signals, "DATA", tmp_path)
    monkeypatch.setattr(signals, "STATE", tmp_path)

    names = ["AAA", "BBB", "CCC", "DDD"]
    snapshot = {
        "local_date": "2026-04-01", "status": "ok", "benchmarks": {},
        "tickers": {n: {"symbol": n, "tier": "A", "filings": [], "financials": {},
                        "trials": [], "bars": _bars(prices)} for n in names},
    }
    (tmp_path / "snap.json").write_text(json.dumps(snapshot))
    watchlist = tmp_path / "watchlist.toml"
    watchlist.write_text(
        "[settings]\nmax_position_pct = 28\nmin_cash_reserve_pct = 15\n\n"
        + "".join(f'[[ticker]]\nsymbol = "{n}"\ntier = "A"\n\n' for n in names))
    out_path = tmp_path / "signals.json"
    monkeypatch.setattr("sys.argv", [
        "signals.py", "--snapshot", str(tmp_path / "snap.json"),
        "--watchlist", str(watchlist), "--out", str(out_path),
        "--state", str(tmp_path / "candidate_alerts.json"),
    ])
    assert signals.main() == 0

    result = json.loads(out_path.read_text())
    exp = result["exposure"]
    assert exp["tiers_counted"] == ["ACT", "SETUP"], \
        "the report has to be able to tell this is a worst case, not a buy list"
    assert exp["actionable_names"] == 4
    assert sorted(exp["symbols"]) == names
    assert exp["requested_pct_if_all_taken"] == pytest.approx(112.0)
    assert exp["max_investable_pct"] == pytest.approx(85.0)   # 100 less the reserve
    assert exp["over_committed"] is True
    assert exp["scale_factor_needed"] == pytest.approx(0.76, abs=0.01)


def test_exposure_within_the_ceiling_needs_no_scaling(monkeypatch, tmp_path):
    """scale_factor_needed is 1.0, not a fraction, when nothing is over-committed
    -- the report multiplies by it unconditionally."""
    prices = [100.0 * (0.985**i) for i in range(90)]
    monkeypatch.setattr(signals, "ROOT", tmp_path)
    monkeypatch.setattr(signals, "DATA", tmp_path)
    monkeypatch.setattr(signals, "STATE", tmp_path)

    snapshot = {
        "local_date": "2026-04-01", "status": "ok", "benchmarks": {},
        "tickers": {"AAA": {"symbol": "AAA", "tier": "A", "filings": [],
                            "financials": {}, "trials": [], "bars": _bars(prices)}},
    }
    (tmp_path / "snap.json").write_text(json.dumps(snapshot))
    watchlist = tmp_path / "watchlist.toml"
    watchlist.write_text("[settings]\nmax_position_pct = 28\nmin_cash_reserve_pct = 15\n\n"
                         '[[ticker]]\nsymbol = "AAA"\ntier = "A"\n')
    out_path = tmp_path / "signals.json"
    monkeypatch.setattr("sys.argv", [
        "signals.py", "--snapshot", str(tmp_path / "snap.json"),
        "--watchlist", str(watchlist), "--out", str(out_path),
        "--state", str(tmp_path / "candidate_alerts.json"),
    ])
    assert signals.main() == 0

    exp = json.loads(out_path.read_text())["exposure"]
    assert exp["over_committed"] is False
    assert exp["scale_factor_needed"] == 1.0
