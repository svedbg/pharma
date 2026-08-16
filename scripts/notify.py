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
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localconfig
from render_email import build_email_html

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    """Settings come from scripts/localconfig.py so there is one loader, not two."""
    return localconfig.load()


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
               attachment: Path | None = None) -> bool:
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
        msg.add_attachment(
            attachment.read_bytes(), maintype="text", subtype="markdown",
            filename=attachment.name,
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Send alerts for the daily desk run")
    ap.add_argument("--signals", default=str(ROOT / "data" / "signals.json"))
    ap.add_argument("--report", default=None, help="path to the day's markdown report")
    ap.add_argument("--failure", default=None, help="send a failure notice instead")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg:
        # Name the file the loader actually prefers. This used to point at
        # notify.env, which is only read for backward compatibility, so the one
        # message a new user sees named the wrong path.
        print(f"[notify] no config at {localconfig.CONFIG_FILES[0]}; nothing sent",
              file=sys.stderr)
        return 0

    if args.failure:
        ok_push = send_ntfy(cfg, "Biotech desk FAILED", args.failure, priority="high", tags="warning")
        ok_mail = send_email(cfg, "[biotech desk] run failed", args.failure)
        print(f"[notify] ntfy sent={ok_push} email sent={ok_mail}", file=sys.stderr)
        return 0

    sig = json.loads(Path(args.signals).read_text())
    summary, body, priority = build_alert_text(sig)
    session_date = sig.get("session_date") or ""
    has_alerts = bool(sig.get("notify") or sig.get("notify_exits"))

    if has_alerts or cfg.get("NTFY_ALWAYS", "0") == "1":
        ok = send_ntfy(cfg, ntfy_title(summary, session_date), body, priority=priority)
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
        print(f"[notify] email sent={ok} (html={bool(html_body)}, "
              f"attached={bool(report_path)})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
