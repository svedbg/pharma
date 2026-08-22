#!/usr/bin/env python3
"""Delivery: ntfy phone push + email.

Credentials live in ~/.config/pharma/pharma.env, never in the repo (the older
notify.env is still read, so an existing install keeps working). Every channel
is optional and failures are non-fatal -- a broken SMTP password should not cost
you the report, which is on disk either way.

Default behaviour:
  ntfy   fires only on a NEW SETUP/ACT tier (set NTFY_ALWAYS=1 to get every run)
  email  sends the full report every run (set EMAIL_ALWAYS=0 for alerts only)
"""

from __future__ import annotations

import argparse
import json
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localconfig
from render_email import build_email_html

ROOT = Path(__file__).resolve().parent.parent
# What actually left the machine, so something other than this process can tell.
# Delivery is the one stage with no evidence of its own: the report is on disk
# whether or not it was sent, and heartbeat.py watches reports. A wrong SMTP
# password therefore produced a written report, a healthy heartbeat, and silence
# on the phone, indefinitely -- and the heartbeat's own alarm would have gone out
# through the same dead channel.
DELIVERY_LOG = ROOT / "data" / "last_delivery.json"
# The pre-market pass records separately. One shared file would let the two runs
# overwrite each other's verdict: the nightly run fails to deliver at 01:30, the
# pre-market pass succeeds at 14:30, and the heartbeat -- which reads whatever is
# there -- sees a healthy desk while last night's report never arrived. Two
# records, both checked, is the only arrangement where a permanently broken
# channel on either run stays visible.
PREMARKET_DELIVERY_LOG = ROOT / "data" / "last_delivery_premarket.json"


def load_config() -> dict:
    """Settings come from scripts/localconfig.py so there is one loader, not two."""
    return localconfig.load()


def configured_channels(cfg: dict) -> dict[str, bool]:
    """Which channels have enough settings to be worth attempting.

    Kept separate from the send result because `send_ntfy` and `send_email` both
    return False when they are merely unconfigured, which is indistinguishable
    from a failure at the call site. Recording that directly would report a
    broken ntfy to anyone who deliberately runs email only.
    """
    return {
        "ntfy": bool(cfg.get("NTFY_TOPIC")),
        "email": bool(cfg.get("SMTP_HOST") and cfg.get("EMAIL_TO")),
    }


def record_delivery(session_date: str, channels: dict[str, bool | None],
                    configured: dict[str, bool] | None = None,
                    path: Path | None = None) -> None:
    """Write what each channel did. None means 'not attempted', not 'failed'.

    Best-effort: a delivery that worked must not be reported as broken because
    the note about it could not be written.
    """
    dest = path or DELIVERY_LOG
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({
            "at": datetime.now().isoformat(timespec="seconds"),
            "session_date": session_date or None,
            "channels": channels,
            # What the machine is set up to do, as opposed to what this
            # particular run had occasion to do. A quiet day with EMAIL_ALWAYS=0
            # legitimately sends nothing, and that must not read as a fault --
            # having nothing configured at all must.
            "configured": configured if configured is not None else {},
            # The question the heartbeat asks: did everything that was supposed
            # to go out, go out?
            "ok": all(v for v in channels.values() if v is not None),
            "attempted": [k for k, v in channels.items() if v is not None],
            # Which run wrote this. The heartbeat reports both records and has
            # to be able to say which one could not deliver.
            "run": "premarket" if dest == PREMARKET_DELIVERY_LOG else "daily",
        }, indent=2))
    except OSError as e:
        print(f"[notify] could not record delivery state: {e}", file=sys.stderr)


def send_ntfy(cfg: dict, title: str, body: str, priority: str = "default", tags: str = "pill") -> bool:
    topic = cfg.get("NTFY_TOPIC")
    if not topic:
        return False
    server = cfg.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(
        f"{server}/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": title.encode("ascii", "replace").decode(),
            "Priority": priority,
            "Tags": tags,
        },
        method="POST",
    )
    token = cfg.get("NTFY_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[notify] ntfy failed: {e}", file=sys.stderr)
        return False


def send_email(cfg: dict, subject: str, body: str, html_body: str | None = None,
               attachment: Path | None = None,
               attachment_name: str | None = None) -> bool:
    host = cfg.get("SMTP_HOST")
    to = cfg.get("EMAIL_TO")
    if not host or not to:
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("EMAIL_FROM") or cfg.get("SMTP_USER") or to
    msg["To"] = to
    # Plain text stays as the fallback part: some clients, and every screen
    # reader, will use it. HTML is added as the preferred alternative.
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    if attachment and attachment.exists():
        # The name is overridable because both runs write a file called
        # <date>.md, in different directories. A mail client shows only the
        # basename, so the nightly report and the pre-market note would arrive
        # on the same day as two identically named attachments -- undoing the
        # whole point of giving them distinguishable subjects.
        msg.add_attachment(
            attachment.read_bytes(), maintype="text", subtype="markdown",
            filename=attachment_name or attachment.name,
        )

    port = int(cfg.get("SMTP_PORT", 587))
    user, password = cfg.get("SMTP_USER"), cfg.get("SMTP_PASS")
    # Google presents app passwords as "abcd efgh ijkl mnop"; pasted verbatim the
    # spaces are sent as part of the secret and auth fails with a 535 that looks
    # like a wrong password. Real passwords never legitimately contain spaces here.
    if password:
        password = password.replace(" ", "")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                if user:
                    s.login(user, password)
                s.send_message(msg)
        return True
    except Exception as e:
        print(f"[notify] email failed: {e}", file=sys.stderr)
        return False


def ntfy_title(summary: str, session_date: str) -> str:
    """Title for a push that arrives with no other context on the lock screen.

    It carries the date because the phone stamps the message with when it
    arrived, which is not the session it describes: the desk fires at 23:18, so
    a run that overruns midnight pushes yesterday's session onto today's
    notification, and a hand re-send is later still.
    """
    return f"{summary} - {session_date}" if session_date else summary


def email_subject(summary: str, session_date: str) -> str:
    """Subject line: name, then date, then what happened.

    Date near the front so a mailbox sorted by subject groups by day. Omitted
    entirely rather than left as a dangling separator when the run has none.
    """
    if not session_date:
        return f"[biotech desk] {summary}"
    return f"[biotech desk] {session_date} - {summary}"


def premarket_subject(summary: str, run_date: str) -> str:
    """Subject for the pre-market note.

    Kept visibly distinct from the nightly subject. Both land in the same
    mailbox on the same calendar day and describe different things -- the
    nightly one is an account of a closed session, this is an account of a
    morning -- so a reader filing by subject must be able to tell them apart
    without opening either. Stamped with the RUN date, because that is what this
    report is about; the session it is measured against is in the body.
    """
    return f"[biotech desk] pre-market {run_date} - {summary}"


def build_premarket_text(delta: dict) -> tuple[str, str, str]:
    """What changed this morning, as (summary, body, priority).

    Read from premarket_delta.py's output and nothing else. The body is the
    urgent block only: it becomes an ntfy push, which is read on a lock screen,
    and the full account is in the email. Priority is `high` when anything is
    urgent, because that is the whole reason a pre-market push exists.

    A morning with nothing urgent returns priority `default` and an empty-ish
    body; main() then sends no push at all. Per the desk's own rule that
    notifications fire on changes and not on states, a daily pre-market buzz
    saying "nothing happened" is how the channel gets muted.
    """
    urgent = delta.get("urgent") or []
    counts = delta.get("counts") or {}
    changed = counts.get("changed", 0)

    if not urgent and not changed:
        return ("nothing new since the close", "No filings, no new vetoes, no "
                "catalyst inside a day. Nothing has changed since the nightly run.",
                "default")

    bits = []
    if urgent:
        bits.append(f"{len(urgent)} urgent")
    if changed:
        bits.append(f"{changed} changed")
    if counts.get("new_filings"):
        bits.append(f"{counts['new_filings']} new filing"
                    f"{'s' if counts['new_filings'] > 1 else ''}")
    summary = ", ".join(bits)

    lines = []
    for ch in urgent:
        lines.append(f"{ch['symbol']} ${ch.get('close')} [{ch.get('tier') or 'NONE'}]")
        for why in ch.get("urgent_because") or []:
            lines.append(f"  {why}")
    body = "\n".join(lines) if lines else summary
    return summary, body, ("high" if urgent else "default")


def build_alert_text(sig: dict) -> tuple[str, str, str]:
    """What happened, as (summary, body, priority).

    The summary carries no date and no product name. It used to carry both,
    because it also served as the ntfy title, which has to stand alone on a
    phone -- and the email subject then prefixed its own, so every notification
    said the date twice and a quiet day went out as
    "[biotech desk] 2026-08-15 - Biotech desk 2026-08-15". One string cannot be
    both a standalone title and a subject suffix, so it is now neither: each
    channel stamps this one once, its own way, through the two helpers above.
    """
    alerts = sig.get("notify") or []
    exits = sig.get("notify_exits") or []
    if not alerts and not exits:
        return ("No new setups", "No new setups. Report written.", "default")

    lines = []
    # Exits lead: a thesis breaking is more urgent than a new idea appearing.
    for e in exits:
        lines.append(f"EXIT {e['symbol']} ${e.get('close')} - {', '.join(e['flags'])}")
    for a in alerts:
        arrow = f"{a.get('previous_tier') or 'NONE'}->{a['tier']}"
        lines.append(f"{a['symbol']} ${a['close']} [{arrow}] {a['reason']}")
    priority = "high" if (exits or any(a["tier"] == "ACT" for a in alerts)) else "default"
    bits = []
    if exits:
        bits.append(f"{len(exits)} EXIT")
    if alerts:
        bits.append(f"{len(alerts)} new setup{'s' if len(alerts) > 1 else ''}")
    return " + ".join(bits), "\n".join(lines), priority


def send_premarket(cfg: dict, configured: dict[str, bool], args) -> int:
    """Deliver the pre-market note: email always, ntfy only when urgent.

    The urgency decision is read out of premarket_delta.py's JSON, never
    inferred from the report text. The report is written by a model; whether the
    phone buzzes at 07:30 ET is arithmetic, and it stays arithmetic.

    A missing delta file is a hard stop rather than a silent "nothing urgent".
    Defaulting to quiet would turn every future breakage of the delta stage into
    a permanently silent phone, which is indistinguishable from a calm market --
    the exact failure the heartbeat exists to catch and the reason this run keeps
    its own delivery record.
    """
    report = Path(args.premarket)
    delta_path = Path(args.delta)
    if not delta_path.exists():
        print(f"[notify] no delta at {delta_path}; refusing to guess whether "
              f"anything is urgent", file=sys.stderr)
        record_delivery("", {"ntfy": None, "email": None}, configured,
                        path=PREMARKET_DELIVERY_LOG)
        return 1
    delta = json.loads(delta_path.read_text())

    summary, body, priority = build_premarket_text(delta)
    run_date = delta.get("asof") or ""
    session = delta.get("current_session") or ""
    channels: dict[str, bool | None] = {"ntfy": None, "email": None}

    # Push only on urgency. `high` is set by build_premarket_text() exactly when
    # delta["urgent"] is non-empty, so this is the same condition spelled once.
    if priority == "high" or cfg.get("NTFY_ALWAYS", "0") == "1":
        ok = send_ntfy(cfg, f"Pre-market {run_date} - {summary}", body,
                       priority=priority, tags="warning")
        if configured["ntfy"]:
            channels["ntfy"] = ok
        print(f"[notify] premarket ntfy sent={ok}", file=sys.stderr)

    if cfg.get("EMAIL_ALWAYS", "1") == "1" or priority == "high":
        report_md = report.read_text() if report.exists() else body
        html_body = None
        try:
            sig = json.loads(Path(args.signals).read_text())
            html_body = build_email_html(report_md, sig)
        except Exception as e:
            print(f"[notify] HTML render failed, sending plain text: {e}",
                  file=sys.stderr)
        ok = send_email(cfg, premarket_subject(summary, run_date), report_md,
                        html_body=html_body,
                        attachment=report if report.exists() else None,
                        attachment_name=f"premarket-{run_date or report.stem}.md")
        if configured["email"]:
            channels["email"] = ok
        print(f"[notify] premarket email sent={ok} (html={bool(html_body)})",
              file=sys.stderr)

    # Keyed by the session the delta was measured against, so the record says
    # which day's numbers this morning's note was compared with.
    record_delivery(session, channels, configured, path=PREMARKET_DELIVERY_LOG)
    failed = [name for name, ok in channels.items() if ok is False]
    if failed:
        print(f"[notify] WARNING: premarket {', '.join(failed)} did not deliver; "
              f"recorded in {PREMARKET_DELIVERY_LOG.name}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Send alerts for the daily desk run")
    ap.add_argument("--signals", default=str(ROOT / "data" / "signals.json"))
    ap.add_argument("--report", default=None, help="path to the day's markdown report")
    ap.add_argument("--failure", default=None, help="send a failure notice instead")
    ap.add_argument("--premarket", default=None, metavar="REPORT",
                    help="send the pre-market note at this path instead of the "
                         "nightly report")
    ap.add_argument("--delta", default=str(ROOT / "data" / "premarket" / "delta.json"),
                    help="premarket_delta.py output; decides what counts as urgent")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg:
        # Name the file the loader actually prefers. This used to point at
        # notify.env, which is only read for backward compatibility, so the one
        # message a new user sees named the wrong path.
        print(f"[notify] no config at {localconfig.CONFIG_FILES[0]}; nothing sent",
              file=sys.stderr)
        # Recorded, not just printed. heartbeat.delivery_fault() reports "no
        # delivery channel configured" from this file, and returning here
        # without writing it made that branch unreachable in the one case it
        # most obviously describes: a machine with no config at all. A missing
        # record is correctly not a fault, so the fault has to be written down.
        record_delivery("", {"ntfy": None, "email": None}, {"ntfy": False, "email": False})
        return 0

    configured = configured_channels(cfg)

    if args.failure:
        # Recorded like any other send. Delivery is the one stage with no
        # evidence of its own, and this was the send with none at all -- the
        # notification that goes out precisely when something has already gone
        # wrong, on a path where nothing afterwards would notice it never
        # arrived. Keyed to no session because a failed run has none.
        channels: dict[str, bool | None] = {"ntfy": None, "email": None}
        ok_push = send_ntfy(cfg, "Biotech desk FAILED", args.failure, priority="high", tags="warning")
        if configured["ntfy"]:
            channels["ntfy"] = ok_push
        ok_mail = send_email(cfg, "[biotech desk] run failed", args.failure)
        if configured["email"]:
            channels["email"] = ok_mail
        record_delivery("", channels, configured)
        print(f"[notify] ntfy sent={ok_push} email sent={ok_mail}", file=sys.stderr)
        return 0

    if args.premarket:
        return send_premarket(cfg, configured, args)

    sig = json.loads(Path(args.signals).read_text())
    summary, body, priority = build_alert_text(sig)
    session_date = sig.get("session_date") or ""
    has_alerts = bool(sig.get("notify") or sig.get("notify_exits"))

    # None until a channel is actually attempted *and* configured, so "quiet
    # day, email only" stays distinguishable from "ntfy is broken".
    channels: dict[str, bool | None] = {"ntfy": None, "email": None}

    if has_alerts or cfg.get("NTFY_ALWAYS", "0") == "1":
        ok = send_ntfy(cfg, ntfy_title(summary, session_date), body, priority=priority)
        if configured["ntfy"]:
            channels["ntfy"] = ok
        print(f"[notify] ntfy sent={ok}", file=sys.stderr)

    if cfg.get("EMAIL_ALWAYS", "1") == "1" or has_alerts:
        report_path = Path(args.report) if args.report else None
        if report_path and report_path.exists():
            report_md = report_path.read_text()
        else:
            report_md, report_path = body + "\n\n" + sig.get("table_markdown", ""), None

        html_body = None
        try:
            html_body = build_email_html(report_md, sig)
        except Exception as e:
            print(f"[notify] HTML render failed, sending plain text: {e}", file=sys.stderr)

        ok = send_email(
            cfg, email_subject(summary, session_date),
            report_md, html_body=html_body, attachment=report_path,
        )
        if configured["email"]:
            channels["email"] = ok
        print(f"[notify] email sent={ok} (html={bool(html_body)}, "
              f"attached={bool(report_path)})", file=sys.stderr)

    record_delivery(session_date, channels, configured)

    # Still 0 when a channel fails: the report is on disk either way, and taking
    # the run down over a delivery problem would cost the archive and the
    # scorecard too. The failure is now recorded instead, and heartbeat.py is
    # what raises it -- from its own timer, which is the only way an alarm about
    # a broken channel reaches anyone.
    failed = [name for name, ok in channels.items() if ok is False]
    if failed:
        print(f"[notify] WARNING: {', '.join(failed)} did not deliver; recorded in "
              f"{DELIVERY_LOG.name} for the heartbeat to pick up", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
