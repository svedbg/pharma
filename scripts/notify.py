#!/usr/bin/env python3
"""Delivery: ntfy phone push + email.

Credentials live in ~/.config/pharma/notify.env, never in the repo. Every
channel is optional and failures are non-fatal -- a broken SMTP password should
not cost you the report, which is on disk either way.

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
CONFIG = Path("~/.config/pharma/notify.env").expanduser()


def _clean_value(raw: str) -> str:
    """Strip a trailing inline comment and surrounding quotes.

    `KEY=1  # explanation` must yield "1", not the whole string -- otherwise an
    `EMAIL_ALWAYS=1 # ...` line silently disables email, and a commented-out
    password line reads as a real credential. A '#' only starts a comment when
    preceded by whitespace, so passwords containing '#' survive intact; quoted
    values are taken verbatim up to the closing quote.
    """
    v = raw.strip()
    if v[:1] in ('"', "'"):
        quote = v[0]
        end = v.find(quote, 1)
        return v[1:end] if end > 0 else v[1:]
    for sep in (" #", "\t#"):
        if sep in v:
            v = v.split(sep, 1)[0]
    return v.strip()


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


def build_alert_text(sig: dict) -> tuple[str, str, str]:
    alerts = sig.get("notify") or []
    exits = sig.get("notify_exits") or []
    date = sig.get("session_date", "")
    if not alerts and not exits:
        return (f"Biotech desk {date}", "No new setups. Report written.", "default")

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
    return f"{' + '.join(bits)} - {date}", "\n".join(lines), priority


def main() -> int:
    ap = argparse.ArgumentParser(description="Send alerts for the daily desk run")
    ap.add_argument("--signals", default=str(ROOT / "data" / "signals.json"))
    ap.add_argument("--report", default=None, help="path to the day's markdown report")
    ap.add_argument("--failure", default=None, help="send a failure notice instead")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg:
        print(f"[notify] no config at {CONFIG}; nothing sent", file=sys.stderr)
        return 0

    if args.failure:
        ok_push = send_ntfy(cfg, "Biotech desk FAILED", args.failure, priority="high", tags="warning")
        ok_mail = send_email(cfg, "[biotech desk] run failed", args.failure)
        print(f"[notify] ntfy sent={ok_push} email sent={ok_mail}", file=sys.stderr)
        return 0

    sig = json.loads(Path(args.signals).read_text())
    title, body, priority = build_alert_text(sig)
    has_alerts = bool(sig.get("notify") or sig.get("notify_exits"))

    if has_alerts or cfg.get("NTFY_ALWAYS", "0") == "1":
        ok = send_ntfy(cfg, title, body, priority=priority)
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
            cfg, f"[biotech desk] {sig.get('session_date')} - {title}",
            report_md, html_body=html_body, attachment=report_path,
        )
        print(f"[notify] email sent={ok} (html={bool(html_body)}, "
              f"attached={bool(report_path)})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
