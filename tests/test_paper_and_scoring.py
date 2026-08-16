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


def test_a_broken_delivery_channel_is_a_heartbeat_fault(tmp_path, monkeypatch):
    """A report on disk is not evidence that anything was sent.

    The heartbeat watched reports only, so a wrong SMTP password read as a
    completely healthy desk: the run wrote its file, this check passed, and the
    phone stayed silent for as long as nobody thought to wonder. notify.py now
    records what each channel did, and this is the only place that answer
    exists.
    """
    import json

    import heartbeat
    monkeypatch.setattr(heartbeat, "DELIVERY_LOG", tmp_path / "last_delivery.json")
    (tmp_path / "last_delivery.json").write_text(json.dumps({
        "at": "2026-08-15T23:20:00", "session_date": "2026-08-15",
        "channels": {"ntfy": True, "email": False},
        "configured": {"ntfy": True, "email": True}, "attempted": ["ntfy", "email"],
    }))
    fault = heartbeat.delivery_fault()
    assert fault and "email" in fault and "ntfy" not in fault


def test_a_clean_delivery_is_not_a_fault(tmp_path, monkeypatch):
    import json

    import heartbeat
    monkeypatch.setattr(heartbeat, "DELIVERY_LOG", tmp_path / "last_delivery.json")
    (tmp_path / "last_delivery.json").write_text(json.dumps({
        "at": "2026-08-15T23:20:00", "session_date": "2026-08-15",
        "channels": {"ntfy": None, "email": True},
        "configured": {"ntfy": False, "email": True}, "attempted": ["email"],
    }))
    assert heartbeat.delivery_fault() is None


def test_a_quiet_day_that_sent_nothing_is_not_a_fault(tmp_path, monkeypatch):
    """EMAIL_ALWAYS=0 with no alerts correctly sends nothing at all. Treating
    "attempted nothing" as broken would cry wolf on every quiet day -- and
    silence is this desk's normal output."""
    import json

    import heartbeat
    monkeypatch.setattr(heartbeat, "DELIVERY_LOG", tmp_path / "last_delivery.json")
    (tmp_path / "last_delivery.json").write_text(json.dumps({
        "at": "2026-08-15T23:20:00", "session_date": "2026-08-15",
        "channels": {"ntfy": None, "email": None},
        "configured": {"ntfy": True, "email": True}, "attempted": [],
    }))
    assert heartbeat.delivery_fault() is None


def test_an_unconfigured_channel_is_not_reported_as_a_broken_one(tmp_path, monkeypatch):
    """send_ntfy and send_email both return False when merely unconfigured,
    which is indistinguishable from a failure at the call site. Recording that
    straight through would report a broken ntfy to anyone running email only."""
    import notify
    monkeypatch.setattr(notify, "DELIVERY_LOG", tmp_path / "last_delivery.json")
    cfg = {"SMTP_HOST": "smtp.example.org", "EMAIL_TO": "a@example.org"}
    assert notify.configured_channels(cfg) == {"ntfy": False, "email": True}

    import heartbeat
    monkeypatch.setattr(heartbeat, "DELIVERY_LOG", tmp_path / "last_delivery.json")
    notify.record_delivery("2026-08-15", {"ntfy": None, "email": True},
                           notify.configured_channels(cfg))
    assert heartbeat.delivery_fault() is None


def test_a_missing_delivery_record_is_not_a_fault(tmp_path, monkeypatch):
    """It means notify.py has not run since this was added. Inventing an alarm
    out of that would fire once on every install."""
    import heartbeat
    monkeypatch.setattr(heartbeat, "DELIVERY_LOG", tmp_path / "nope.json")
    assert heartbeat.delivery_fault() is None


def test_a_desk_with_nowhere_to_send_is_a_fault(tmp_path, monkeypatch):
    """Sending nothing is fine; having nowhere to send is not. From the phone's
    point of view an unconfigured desk and a dead one are the same thing."""
    import json

    import heartbeat
    monkeypatch.setattr(heartbeat, "DELIVERY_LOG", tmp_path / "last_delivery.json")
    (tmp_path / "last_delivery.json").write_text(json.dumps({
        "at": "2026-08-15T23:20:00", "session_date": "2026-08-15",
        "channels": {"ntfy": None, "email": None},
        "configured": {"ntfy": False, "email": False}, "attempted": [],
    }))
    assert "no delivery channel configured" in (heartbeat.delivery_fault() or "")


def test_notify_records_what_each_channel_did(tmp_path, monkeypatch):
    """None means 'not configured', not 'failed' -- a quiet day that sends email
    only must not read as a broken ntfy."""
    import json

    import notify
    monkeypatch.setattr(notify, "DELIVERY_LOG", tmp_path / "last_delivery.json")
    notify.record_delivery("2026-08-15", {"ntfy": None, "email": True})
    rec = json.loads((tmp_path / "last_delivery.json").read_text())
    assert rec["ok"] is True and rec["attempted"] == ["email"]

    notify.record_delivery("2026-08-15", {"ntfy": False, "email": True})
    rec = json.loads((tmp_path / "last_delivery.json").read_text())
    assert rec["ok"] is False


# ------------------------------------------------------- alert grading


def test_an_alert_with_no_session_date_does_not_take_the_scorecard_down(
        db, capsys, monkeypatch):
    """signals.py used to write the snapshot's raw local_date into the alerts
    table, so a snapshot that carried none left NULL in the primary key. The
    scorer locates an alert by comparing bar dates against it, and `d >= None`
    raises -- taking the whole scorecard with it, and run_daily.sh swallows that
    failure as non-fatal, so the desk quietly stops grading itself.

    signals.py no longer writes such a row. Rows already in a database still
    have to be survivable.
    """
    import score_alerts

    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            session_date TEXT, ticker TEXT, tier TEXT, previous_tier TEXT,
            close REAL, rsi REAL, pctb REAL, capitulation INTEGER,
            vetoes INTEGER, excess_20d REAL, bucket TEXT, source TEXT,
            reason TEXT, context TEXT,
            PRIMARY KEY (session_date, ticker, source));
    """)
    rows = [
        (None, "XYZ", "SETUP", "NONE", 10.0, 30.0, 0.1, 0, 0, None, "A", "live", "", None),
        ("not-a-date", "XYZ", "SETUP", "NONE", 10.0, 30.0, 0.1, 0, 0, None, "A", "backfill", "", None),
        ("2026-01-02", "XYZ", "SETUP", "NONE", 10.0, 30.0, 0.1, 0, 0, None, "A", "live", "", None),
    ]
    con.executemany("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    assert score_alerts.usable_session("2026-01-02") is True
    assert score_alerts.usable_session(None) is False
    assert score_alerts.usable_session("not-a-date") is False

    monkeypatch.setattr(score_alerts, "DB", db)
    monkeypatch.setattr("sys.argv", ["score_alerts.py"])
    assert score_alerts.main() == 0
    out = capsys.readouterr().out
    # It graded the one real alert and said what it could not grade.
    assert "SKIPPED  2 alert(s) carry no usable session date" in out
