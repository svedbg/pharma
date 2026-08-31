"""The pre-market delta: what is new, and what deserves to reach the phone.

`premarket_delta.py` is the deterministic half of the pre-market run. The report
around it is written by a model, but whether the phone buzzes at 07:30 ET is
arithmetic and has to stay arithmetic, so these pin the arithmetic.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import premarket_delta as pd

ROOT = Path(__file__).resolve().parent.parent


def _sig(session: str, *signals: dict) -> dict:
    return {"session_date": session, "generated_at": f"{session}T01:34:00",
            "signals": list(signals)}


def _name(symbol: str = "ARDX", **kw) -> dict:
    base = {
        "symbol": symbol,
        "company": f"{symbol} Inc",
        "tier": "NONE",
        "price": {"close": 4.03},
        "hard_vetoes": [],
        "soft_flags": [],
        "exit_flags": [],
        "catalysts": [],
        "new_filings_since_last_run": [],
    }
    base.update(kw)
    return base


ASOF = date(2026, 8, 20)


def test_a_standing_veto_is_not_reported_as_new_each_morning():
    """Vetoes are compared by (form, filed), never as whole dicts.

    `days_ago` and the rendered `reason` both move with the clock, so comparing
    dicts would mark every standing veto as new every single morning -- the
    report would be all noise and the phone would buzz daily for nothing, which
    is precisely how an alert channel gets muted.
    """
    veto = {"form": "424B5", "filed": "2026-08-18", "days_ago": 1,
            "reason": "prospectus supplement -- filed 1 day ago"}
    aged = {"form": "424B5", "filed": "2026-08-18", "days_ago": 2,
            "reason": "prospectus supplement -- filed 2 days ago"}

    out = pd.diff_symbol(ASOF, _name(hard_vetoes=[veto]), _name(hard_vetoes=[aged]))
    assert out is None, "an unchanged veto with a drifted age was reported as a change"


def test_a_genuinely_new_hard_veto_is_urgent():
    out = pd.diff_symbol(
        ASOF,
        _name(),
        _name(hard_vetoes=[{"form": "424B5", "filed": "2026-08-20", "days_ago": 0,
                            "reason": "prospectus supplement -- READ IT"}]),
    )
    assert out is not None
    assert len(out["new_hard_vetoes"]) == 1
    assert out["urgent_because"], "a new hard veto did not make the name urgent"


def test_a_veto_that_aged_out_is_reported_but_is_not_urgent():
    """A veto clearing is good news and belongs in the report. It is not a
    reason to look at a screen before the open."""
    out = pd.diff_symbol(
        ASOF,
        _name(hard_vetoes=[{"form": "424B5", "filed": "2026-08-05", "days_ago": 10,
                            "reason": "prospectus supplement"}]),
        _name(),
    )
    assert out is not None
    assert len(out["cleared_hard_vetoes"]) == 1
    assert out["urgent_because"] == []


@pytest.mark.parametrize("form,items,urgent", [
    # An 8-K carrying any item the pipeline already considers material. 8.01 is
    # where readouts, CRLs and FDA correspondence land, and the veto layer has
    # no opinion on it precisely because it is news, not a mechanical condition.
    ("8-K", "8.01", True),
    ("8-K", "2.02", True),
    ("8-K", "3.02", True),
    # An 8-K with no recognised item is not nothing, but it is not a reason to
    # wake anyone either.
    ("8-K", "", False),
    ("8-K", "9.99", False),
    # Dilution being priced.
    ("424B5", "", True),
    ("424B3", "", True),
    # 424B7 registers shares existing holders already own, so it is a resale and
    # not an issuance -- excluded here exactly as the veto layer excludes it, so
    # there is one answer to "is this dilution?" rather than two that can drift.
    ("424B7", "", False),
    # Insider transactions matter and the insider layer reads them every night.
    ("4", "", False),
    ("SC 13G/A", "", False),
    ("10-Q", "", False),
])
def test_which_new_filings_reach_the_phone(form, items, urgent):
    filing = {"form": form, "filed": "2026-08-20", "items": items,
              "item_meanings": [], "url": "https://example.invalid/f"}
    out = pd.diff_symbol(ASOF, _name(), _name(new_filings_since_last_run=[filing]))
    assert out is not None, "a new filing was not reported as a change at all"
    assert bool(out["urgent_because"]) is urgent, (
        f"{form} items={items!r} urgency was {bool(out['urgent_because'])}, "
        f"expected {urgent}")


def test_item_meanings_come_from_the_fetch_table_not_a_second_copy():
    """The urgency rule reads fetch.ITEM_MEANINGS, so the pre-market email
    cannot describe item 8.01 differently from the nightly report.

    Compared by value and pinned at the source, not by object identity: the
    suite ends up with more than one `fetch` module instance, so `is` fails for
    a reason that has nothing to do with whether there are two tables.
    """
    import fetch
    assert pd.ITEM_MEANINGS == fetch.ITEM_MEANINGS
    src = (ROOT / "scripts" / "premarket_delta.py").read_text()
    assert "from fetch import ITEM_MEANINGS" in src, \
        "premarket_delta.py no longer imports the table"
    assert "ITEM_MEANINGS = {" not in src, \
        "premarket_delta.py defines its own item table instead of importing one"
    why = pd._filing_is_urgent(
        {"form": "8-K", "filed": "2026-08-20", "items": "8.01"})
    assert why and fetch.ITEM_MEANINGS["8.01"] in why


def test_only_high_severity_exit_flags_push():
    """`high` is a thesis that has broken or a binary that has resolved.
    `medium` is a standing live veto, which was almost certainly in last night's
    report already -- reported, but not a nightly buzz."""
    high = pd.diff_symbol(ASOF, _name(), _name(exit_flags=[
        {"kind": "invalidation_breached", "severity": "high", "detail": "closed below"}]))
    medium = pd.diff_symbol(ASOF, _name(), _name(exit_flags=[
        {"kind": "veto_active", "severity": "medium", "detail": "1 hard veto"}]))

    assert high["urgent_because"], "a breached invalidation did not push"
    assert medium is not None and medium["urgent_because"] == [], \
        "a live-veto exit flag pushed"


def test_catalyst_proximity_is_measured_from_the_run_date_not_days_until():
    """`days_until` is measured from the snapshot's SESSION date, and the
    pre-market pass re-derives the previous session because no new bar exists
    yet.

    So a catalyst resolving this morning arrives from signals.py as
    `days_until: 1`. Reading that as "tomorrow" would put the strongest sizing
    instruction the desk emits one day out on the morning it actually matters.
    """
    today = {"symbol": "CAPR", "date": "2026-08-20", "kind": "pdufa",
             "confidence": "confirmed", "description": "PDUFA", "days_until": 1}
    out = pd.diff_symbol(ASOF, _name("CAPR"), _name("CAPR", catalysts=[today]))

    assert out is not None
    assert out["imminent_catalysts"][0]["days_from_today"] == 0, \
        "a catalyst dated today was not measured as today"
    assert any("TODAY" in w for w in out["urgent_because"])


def test_a_catalyst_that_already_passed_is_not_urgent():
    """Past binaries are the exit layer's business (`catalyst_resolved`), not a
    reason to act before the open."""
    passed = {"symbol": "CAPR", "date": "2026-08-14", "kind": "pdufa",
              "confidence": "confirmed", "description": "PDUFA", "days_until": -5}
    out = pd.diff_symbol(ASOF, _name("CAPR"), _name("CAPR", catalysts=[passed]))
    assert out is None or out["imminent_catalysts"] == []


def test_an_unparseable_catalyst_date_is_dropped_not_guessed_at():
    bad = {"symbol": "X", "date": "not-a-date", "kind": "pdufa",
           "confidence": "rumored", "description": "?"}
    out = pd.diff_symbol(ASOF, _name("X"), _name("X", catalysts=[bad]))
    assert out is None or out["imminent_catalysts"] == []


def test_a_quiet_morning_produces_no_changes_and_no_urgency():
    base = _sig("2026-08-19", _name("ARDX"), _name("CAPR"))
    cur = _sig("2026-08-19", _name("ARDX"), _name("CAPR"))
    delta = pd.build(ASOF, base, cur)

    assert delta["counts"]["changed"] == 0
    assert delta["urgent"] == []
    assert "Nothing has changed" in pd.render(delta)


def test_a_watchlist_edit_between_the_runs_is_named_rather_than_silent():
    """The pre-market pass covers the whole watchlist, so a reader counting
    names would come up short with nothing saying why."""
    base = _sig("2026-08-19", _name("ARDX"), _name("OTLK"))
    cur = _sig("2026-08-19", _name("ARDX"), _name("NEWCO"))
    delta = pd.build(ASOF, base, cur)

    assert delta["watchlist_dropped"] == ["OTLK"]
    assert delta["watchlist_added"] == ["NEWCO"]
    text = pd.render(delta)
    assert "OTLK" in text and "NEWCO" in text


def test_a_session_mismatch_refuses_instead_of_diffing_two_days(tmp_path, capsys):
    """The pre-market pass runs before any new bar exists, so it must re-derive
    the same session the nightly run analysed. If it did not, every difference
    below is an artefact of the mismatch rather than something that happened."""
    base = tmp_path / "base.json"
    cur = tmp_path / "cur.json"
    base.write_text(json.dumps(_sig("2026-08-19", _name())))
    cur.write_text(json.dumps(_sig("2026-08-20", _name())))

    import sys
    argv = sys.argv
    sys.argv = ["premarket_delta.py", "--baseline", str(base), "--current", str(cur),
                "--asof", "2026-08-20"]
    try:
        assert pd.main() == 1, "a session mismatch did not refuse"
    finally:
        sys.argv = argv
    assert "session mismatch" in capsys.readouterr().err


def test_the_urgent_block_leads_the_rendered_text():
    """Exits and urgency lead the notification ahead of anything else, the same
    ordering notify.py already uses: a thesis breaking outranks an idea."""
    base = _sig("2026-08-19", _name("ARDX"), _name("OTLK"))
    cur = _sig("2026-08-19",
               _name("ARDX", new_filings_since_last_run=[
                   {"form": "4", "filed": "2026-08-20", "items": "",
                    "item_meanings": []}]),
               _name("OTLK", hard_vetoes=[
                   {"form": "424B5", "filed": "2026-08-20", "days_ago": 0,
                    "reason": "priced takedown"}]))
    text = pd.render(pd.build(ASOF, base, cur))

    assert text.index("URGENT") < text.index("ALSO CHANGED")
    assert "OTLK" in text.split("ALSO CHANGED")[0]


# --- delivery ---------------------------------------------------------------

def _delta_file(tmp_path, urgent, changed=0, session="2026-08-19"):
    import json as _json
    p = tmp_path / "delta.json"
    p.write_text(_json.dumps({
        "asof": "2026-08-20", "current_session": session,
        "baseline_session": session,
        "urgent": urgent,
        "changes": urgent,
        "counts": {"names": 61, "changed": changed or len(urgent),
                   "urgent": len(urgent), "new_filings": 0, "new_hard_vetoes": 0},
    }))
    return p


class _Args:
    """A stand-in for notify.py's parsed arguments.

    Every default argparse supplies is set here first, so a flag added to
    notify.py reaches send_premarket() as its default rather than as a missing
    attribute -- the failure would be an AttributeError in the tests only, which
    says nothing about the flag it is actually about.
    """

    def __init__(self, **kw):
        self.no_email = False
        self.__dict__.update(kw)


def _cfg():
    return {"NTFY_TOPIC": "t", "SMTP_HOST": "h", "EMAIL_TO": "a@b.co",
            "SMTP_USER": "u", "SMTP_PASS": "p"}


def _capture(monkeypatch, notify):
    sent = {"ntfy": [], "email": []}
    monkeypatch.setattr(notify, "send_ntfy",
                        lambda cfg, title, body, priority="default", tags="pill":
                        sent["ntfy"].append((title, body, priority)) or True)
    monkeypatch.setattr(notify, "send_email",
                        lambda cfg, subject, body, html_body=None, attachment=None,
                        attachment_name=None:
                        sent["email"].append((subject, body, attachment_name)) or True)
    return sent


def test_an_urgent_morning_pushes_and_emails(tmp_path, monkeypatch):
    import notify
    sent = _capture(monkeypatch, notify)
    monkeypatch.setattr(notify, "PREMARKET_DELIVERY_LOG", tmp_path / "rec.json")
    delta = _delta_file(tmp_path, [{
        "symbol": "OTLK", "close": 0.71, "tier": "NONE",
        "urgent_because": ["424B5 prospectus supplement -- read it before the open"],
    }])
    report = tmp_path / "pm.md"
    report.write_text("# Pre-market\n")

    rc = notify.send_premarket(
        _cfg(), {"ntfy": True, "email": True},
        _Args(premarket=str(report), delta=str(delta),
              signals=str(tmp_path / "missing-signals.json")))

    assert rc == 0
    assert len(sent["ntfy"]) == 1, "an urgent morning did not push"
    assert sent["ntfy"][0][2] == "high"
    assert "OTLK" in sent["ntfy"][0][1]
    assert len(sent["email"]) == 1


def test_a_quiet_morning_emails_but_does_not_push(tmp_path, monkeypatch):
    """Notifications fire on changes and not on states. A daily pre-market buzz
    saying nothing happened is how the channel gets muted."""
    import notify
    sent = _capture(monkeypatch, notify)
    monkeypatch.setattr(notify, "PREMARKET_DELIVERY_LOG", tmp_path / "rec.json")
    delta = _delta_file(tmp_path, [], changed=0)
    report = tmp_path / "pm.md"
    report.write_text("# Pre-market\nNothing material.\n")

    rc = notify.send_premarket(
        _cfg(), {"ntfy": True, "email": True},
        _Args(premarket=str(report), delta=str(delta),
              signals=str(tmp_path / "missing-signals.json")))

    assert rc == 0
    assert sent["ntfy"] == [], "a quiet morning buzzed the phone"
    assert len(sent["email"]) == 1, "a quiet morning sent no email"


def test_a_missing_delta_refuses_rather_than_assuming_nothing_is_urgent(
        tmp_path, monkeypatch):
    """Defaulting to quiet would turn every future breakage of the delta stage
    into a permanently silent phone, which is indistinguishable from a calm
    market -- so it fails loudly and records the fault."""
    import notify
    sent = _capture(monkeypatch, notify)
    rec = tmp_path / "rec.json"
    monkeypatch.setattr(notify, "PREMARKET_DELIVERY_LOG", rec)

    rc = notify.send_premarket(
        _cfg(), {"ntfy": True, "email": True},
        _Args(premarket=str(tmp_path / "pm.md"),
              delta=str(tmp_path / "absent.json"),
              signals=str(tmp_path / "s.json")))

    assert rc == 1
    assert sent["ntfy"] == [] and sent["email"] == []
    assert rec.exists(), "the refusal was not recorded for the heartbeat"


def test_the_premarket_subject_is_distinguishable_from_the_nightly_one():
    """Both land in the same mailbox on the same calendar day and describe
    different things, so a reader filing by subject must be able to tell them
    apart without opening either."""
    import notify
    nightly = notify.email_subject("2 new setups", "2026-08-19")
    morning = notify.premarket_subject("1 urgent, 3 changed", "2026-08-20")
    assert nightly != morning
    assert "pre-market" in morning and "pre-market" not in nightly


def test_the_premarket_record_names_which_run_wrote_it(tmp_path, monkeypatch):
    """The heartbeat reports both records and has to be able to say which one
    could not deliver."""
    import json as _json

    import notify
    rec = tmp_path / "rec.json"
    monkeypatch.setattr(notify, "PREMARKET_DELIVERY_LOG", rec)
    notify.record_delivery("2026-08-19", {"ntfy": True, "email": True},
                           {"ntfy": True, "email": True}, path=rec)
    assert _json.loads(rec.read_text())["run"] == "premarket"


def test_the_heartbeat_reports_a_broken_premarket_channel(tmp_path, monkeypatch):
    """A nightly run that delivers fine must not hide a pre-market pass that
    cannot. One shared record did exactly that."""
    import json as _json

    import heartbeat
    good, bad = tmp_path / "daily.json", tmp_path / "pm.json"
    for path, ok in ((good, True), (bad, False)):
        path.write_text(_json.dumps({
            "at": "2026-08-20T14:34:00", "session_date": "2026-08-19",
            "channels": {"ntfy": ok, "email": ok},
            "configured": {"ntfy": True, "email": True},
        }))
    monkeypatch.setattr(heartbeat, "DELIVERY_LOGS",
                        (("nightly run", good), ("pre-market pass", bad)))

    fault = heartbeat.delivery_fault()
    assert fault and "pre-market pass" in fault
    assert "nightly run" not in fault


def test_the_attached_markdown_is_named_distinguishably(tmp_path, monkeypatch):
    """Both runs write a file called <date>.md, in different directories, and a
    mail client shows only the basename -- so on any given day the nightly
    report and the pre-market note would arrive as two identically named
    attachments, undoing the distinguishable subjects."""
    import notify
    sent = _capture(monkeypatch, notify)
    monkeypatch.setattr(notify, "PREMARKET_DELIVERY_LOG", tmp_path / "rec.json")
    delta = _delta_file(tmp_path, [], changed=2)
    report = tmp_path / "2026-08-20.md"
    report.write_text("# Pre-market\n")

    notify.send_premarket(
        _cfg(), {"ntfy": True, "email": True},
        _Args(premarket=str(report), delta=str(delta),
              signals=str(tmp_path / "s.json")))

    name = sent["email"][0][2]
    assert name and name != report.name, "the attachment kept the ambiguous name"
    assert "premarket" in name
