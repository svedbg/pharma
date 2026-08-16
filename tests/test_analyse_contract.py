"""Characterisation tests for analyse().

analyse() is the function every signal flows through and, at 360 lines, was the
riskiest thing in the codebase to change. These pin its observable contract so
it can be restructured safely: they assert the shape and the decision rules, not
the internals.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import signals

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
    out = signals.analyse(_rec([10.0] * 5), SETTINGS)
    assert out["tier"] == "NO_DATA"
    assert "insufficient" in out["reasons"][0]


def test_output_carries_the_keys_the_report_depends_on():
    """The report and the email renderer index these directly; a rename here
    silently empties sections downstream."""
    out = signals.analyse(_rec([10.0 + (i % 9) * 0.3 for i in range(120)]), SETTINGS)
    for key in ("symbol", "bucket", "tier", "reasons", "price", "technicals",
                "max_position_pct", "hard_vetoes", "soft_flags", "recent_events",
                "conviction", "exit_flags", "links", "links_md", "tradability"):
        assert key in out, f"missing key: {key}"
    for key in ("close", "date", "chg_1d_pct", "percentile_1y", "pct_off_52w_high"):
        assert key in out["price"]
    for key in ("rsi14", "bollinger_pct_b", "volume_vs_20d_avg"):
        assert key in out["technicals"]


def test_lottery_bucket_gets_the_smaller_ceiling():
    out = signals.analyse(_rec([10.0] * 40 + [10.5] * 40, tier="lottery"), SETTINGS)
    assert out["max_position_pct"] == 5
    assert signals.analyse(_rec([10.0] * 80, tier="A"), SETTINGS)["max_position_pct"] == 28


def test_a_hard_veto_blocks_setup():
    """An oversold name carrying a veto must degrade to WATCH, never SETUP."""
    falling = [100 - i * 1.5 for i in range(60)]
    veto = [{"form": "424B5", "filed": "2026-01-01", "items": "",
             "offering_type": "priced", "url": "u"}]
    clean = signals.analyse(_rec(falling), SETTINGS)
    vetoed = signals.analyse(_rec(falling, filings=veto), SETTINGS)
    assert vetoed["tier"] != "ACT"
    assert len(vetoed["hard_vetoes"]) >= len(clean["hard_vetoes"])


def test_act_requires_a_declared_zone():
    """Without entry_high the ACT tier is structurally unreachable, and the
    reasons must say so rather than failing silently."""
    falling = [100 - i * 1.5 for i in range(60)]
    out = signals.analyse(_rec(falling), SETTINGS)
    assert out["tier"] != "ACT"
    if out["tier"] == "SETUP":
        assert any("no entry zone" in r for r in out["reasons"])


def test_stale_zone_is_flagged_as_a_soft_flag():
    prices = [10.0] * 60 + [20.0] * 20          # price far above its zone
    out = signals.analyse(_rec(prices, entry_high=10.0, entry_low=8.0), SETTINGS)
    assert out["zone_stale"] is True
    assert any("stale entry zone" in f["form"] for f in out["soft_flags"])


def test_conviction_is_always_present_and_labelled():
    out = signals.analyse(_rec([10.0 + (i % 5) * 0.2 for i in range(120)]), SETTINGS)
    assert out["conviction"]["label"] in ("strong", "moderate", "weak", "avoid")
    assert isinstance(out["conviction"]["supporting"], list)
    assert isinstance(out["conviction"]["against"], list)


def test_deterministic_for_the_same_input():
    """Stage 2 must be pure arithmetic: identical input, identical output."""
    rec = _rec([10.0 + (i % 11) * 0.4 for i in range(150)])
    a = json.dumps(signals.analyse(rec, SETTINGS), sort_keys=True, default=str)
    b = json.dumps(signals.analyse(rec, SETTINGS), sort_keys=True, default=str)
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
