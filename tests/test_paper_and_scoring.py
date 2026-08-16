"""The arithmetic that decides whether the project worked.

paper.py, propose_zones.py and score_alerts.py were the last untested modules,
and they are the ones that compute the verdict and the ACT gate. A silent error
in any of them produces a confident, wrong conclusion about the whole strategy —
which is worse than no conclusion at all.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import paper
import propose_zones as pz

# ------------------------------------------------------------------ fixtures


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A throwaway database with a name and a benchmark that both rise 10%."""
    path = tmp_path / "history.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE bars (ticker TEXT, date TEXT, open REAL, high REAL, low REAL,
                           close REAL, adjclose REAL, volume INTEGER,
                           PRIMARY KEY (ticker, date));
    """)
    days = [f"2026-01-{d:02d}" for d in range(1, 21)]
    for i, d in enumerate(days):
        con.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                    ("XYZ", d, 10, 10, 10, 10 + i * 0.5, 10 + i * 0.5, 100_000))
        con.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                    ("XBI", d, 100, 100, 100, 100 + i * 1.0, 100 + i * 1.0, 100_000))
    con.commit()
    con.close()
    monkeypatch.setattr(paper, "DB", path)
    return path


def _args(**kw):
    base = {"ticker": "XYZ", "entry": None, "size": None, "stop": None,
            "horizon": "bounce", "thesis": "", "date": None, "price": None, "note": ""}
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------- paper log


def test_backdated_entry_uses_that_days_price(db, capsys):
    """Defaulting to the latest close silently records a trade at a price that
    was never available on the open date, making every later number wrong."""
    con = paper.con()
    paper.cmd_open(con, _args(date="2026-01-05"))
    row = con.execute("SELECT entry FROM paper_trades").fetchone()
    con.close()
    assert row[0] == pytest.approx(12.0)  # 10 + 4*0.5, not the final 19.5


def test_entry_falls_back_to_the_prior_session_on_a_non_trading_day(db):
    con = paper.con()
    paper.cmd_open(con, _args(date="2026-01-25"))   # beyond the last bar
    row = con.execute("SELECT entry FROM paper_trades").fetchone()
    con.close()
    assert row[0] == pytest.approx(19.5)


def test_duplicate_open_is_refused(db, capsys):
    con = paper.con()
    assert paper.cmd_open(con, _args()) == 0
    assert paper.cmd_open(con, _args()) == 1
    assert con.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1
    con.close()


def test_closing_scores_against_the_benchmark(db, capsys):
    """XYZ rises 10 -> 19.5 (+95%) while XBI rises 100 -> 119 (+19%). The trade
    won in absolute terms and also beat the benchmark; both must be reported."""
    con = paper.con()
    paper.cmd_open(con, _args(date="2026-01-01"))
    paper.cmd_close(con, _args(date="2026-01-20"))
    con.close()
    out = capsys.readouterr().out
    assert "+95.0% absolute" in out
    assert "vs XBI" in out
    assert "+76" in out  # 95 - 19


def test_a_gain_smaller_than_the_benchmark_is_reported_as_a_loss(db, capsys):
    """The central discipline: +9% while the sector rose 19% is a losing
    decision, and absolute P&L alone would say the opposite."""
    con = paper.con()
    paper.cmd_open(con, _args(date="2026-01-01"))
    paper.cmd_close(con, _args(date="2026-01-20", price=10.9))  # +9%
    con.close()
    out = capsys.readouterr().out
    assert "+9.0% absolute" in out
    assert "-10" in out          # roughly -10pp against the benchmark


def test_closing_without_an_open_position_fails(db):
    con = paper.con()
    assert paper.cmd_close(con, _args()) == 1
    con.close()


def test_closing_with_no_available_price_says_so(db, capsys):
    """cmd_open already refuses politely when it cannot find a price; cmd_close
    fell through to `px / entry` with px=None and raised TypeError instead. The
    position it was trying to close stayed open, so the failure also lost data."""
    con = paper.con()
    paper.cmd_open(con, _args(ticker="GONE", entry=5.0))
    capsys.readouterr()
    # GONE has no bars at all, and no --price was passed.
    assert paper.cmd_close(con, _args(ticker="GONE")) == 1
    assert "pass --price" in capsys.readouterr().err
    still_open = con.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE ticker='GONE' AND closed IS NULL").fetchone()
    con.close()
    assert still_open[0] == 1, "a refused close must leave the position untouched"


def test_report_states_the_verdict_against_the_benchmark(db, capsys):
    con = paper.con()
    paper.cmd_open(con, _args(date="2026-01-01"))
    paper.cmd_close(con, _args(date="2026-01-20", price=10.9))
    capsys.readouterr()
    paper.cmd_report(con, _args())
    con.close()
    out = capsys.readouterr().out
    assert "NOT beating XBI" in out
    assert "too few to conclude" in out   # refuses to over-read one trade


def test_status_marks_a_breached_stop(db, capsys):
    con = paper.con()
    paper.cmd_open(con, _args(date="2026-01-01", stop=25.0))  # above the last close
    capsys.readouterr()
    paper.cmd_status(con, _args())
    con.close()
    assert "STOP HIT" in capsys.readouterr().out


# --------------------------------------------------------------- entry zones


def _bars(prices):
    return [{"date": f"2026-01-{i+1:02d}", "adjclose": p, "close": p,
             "open": p, "high": p, "low": p, "volume": 1000}
            for i, p in enumerate(prices)]


def test_zone_uses_only_post_collapse_bars():
    """Pre-break prices describe a company that no longer exists. Averaging them
    in places a buy zone far above where the stock now trades — arithmetic
    catching a falling knife."""
    prices = [50.0] * 40 + [5.0] + [5.0 + i * 0.05 for i in range(40)]
    out = pz.propose({"symbol": "X", "bars": _bars(prices)})
    assert "post-collapse" in out["note"]
    assert out["entry_high"] < 10, "zone must ignore the pre-collapse regime"


def test_no_zone_when_the_collapse_is_too_recent():
    prices = [50.0] * 40 + [5.0, 5.1, 5.2]
    out = pz.propose({"symbol": "X", "bars": _bars(prices)})
    assert out["entry_high"] == 0
    assert "too soon" in out["status"]


def test_uptrending_name_anchors_to_a_pullback_not_an_old_percentile():
    """A pure percentile returns the price before the name re-rated, which it
    will never revisit if the thesis is working."""
    prices = [float(1 + i * 0.2) for i in range(120)]     # 1 -> ~25
    out = pz.propose({"symbol": "X", "bars": _bars(prices)})
    assert "pullback" in out["note"]
    # Must sit near the recent regime, not down at the early single digits.
    assert out["entry_high"] > prices[-1] * 0.5


def test_invalidation_sits_below_the_window_low():
    # Kept gentle on purpose: a >25% single-session drop would (correctly)
    # trigger the collapse path and refuse a zone entirely.
    prices = [10.0 + (i % 5) * 0.2 for i in range(120)]
    out = pz.propose({"symbol": "X", "bars": _bars(prices)})
    assert 0 < out["invalidation_price"] < min(prices[-120:])


def test_entry_low_is_below_entry_high():
    prices = [10.0 + (i % 7) * 0.2 for i in range(120)]
    out = pz.propose({"symbol": "X", "bars": _bars(prices)})
    assert 0 < out["entry_low"] < out["entry_high"]


def test_zone_is_skipped_without_enough_history():
    out = pz.propose({"symbol": "X", "bars": _bars([10.0, 10.1, 10.2])})
    assert out["entry_high"] == 0


def test_applying_zones_rewrites_only_the_intended_keys(tmp_path):
    wl = tmp_path / "watchlist.toml"
    wl.write_text('[settings]\na = 1\n\n[[ticker]]\nsymbol = "X"\ntier = "A"\n'
                  'thesis = "keep me"\nentry_low = 0\nentry_high = 0\n'
                  'invalidation_price = 0\ninvalidation = "keep me too"\n')
    zones = {"X": {"entry_low": 1.5, "entry_high": 2.0, "invalidation_price": 1.0}}
    pz.apply_to_watchlist(wl, zones)
    text = wl.read_text()
    assert "entry_high = 2.0" in text
    assert "invalidation_price = 1.0" in text
    # Comments, thesis and unrelated fields must survive untouched.
    assert 'thesis = "keep me"' in text
    assert 'invalidation = "keep me too"' in text
    import tomllib
    assert tomllib.loads(text)["ticker"][0]["entry_low"] == 1.5


def test_a_zone_cannot_leak_into_a_later_section(tmp_path):
    """The rewrite tracks sections by hand, and the last ticker used to stay
    'current' through everything after it. A key of the same name in a section
    below the ticker blocks would be silently overwritten with that ticker's
    zone -- and these keys sit next to position-size settings."""
    import tomllib
    wl = tmp_path / "watchlist.toml"
    wl.write_text('[[ticker]]\nsymbol = "X"\nentry_low = 0\nentry_high = 0\n\n'
                  '[defaults]\nentry_low = 99\nentry_high = 99\n')
    pz.apply_to_watchlist(wl, {"X": {"entry_low": 1.5, "entry_high": 2.0,
                                     "invalidation_price": 1.0}})
    parsed = tomllib.loads(wl.read_text())
    assert parsed["ticker"][0]["entry_high"] == 2.0
    assert parsed["defaults"] == {"entry_low": 99, "entry_high": 99}


# ----------------------------------------------------------------- heartbeat


def test_heartbeat_counts_weekdays_not_calendar_days():
    """A Friday report checked on Monday is one weekday old, not three. Counting
    calendar days would raise a false alarm every Monday morning."""
    from datetime import date

    import heartbeat
    friday, monday = date(2026, 8, 14), date(2026, 8, 17)
    assert heartbeat.weekdays_between(friday, monday) == 1
    assert heartbeat.weekdays_between(friday, date(2026, 8, 21)) == 5


def test_heartbeat_reports_stale_when_no_report_exists(tmp_path, monkeypatch, capsys):
    import heartbeat
    monkeypatch.setattr(heartbeat, "REPORTS", tmp_path)
    last, path = heartbeat.latest_report()
    assert last is None and path is None


def test_heartbeat_finds_the_newest_dated_report(tmp_path, monkeypatch):
    from datetime import date

    import heartbeat
    monkeypatch.setattr(heartbeat, "REPORTS", tmp_path)
    for name in ("2026-08-10.md", "2026-08-14.md", "notes.md", "draft-2026.md"):
        (tmp_path / name).write_text("x")
    last, path = heartbeat.latest_report()
    assert last == date(2026, 8, 14)
    assert path.name == "2026-08-14.md"
