#!/usr/bin/env python3
"""Turn the daily markdown report into a mobile-readable HTML email.

Deliberately stdlib-only and deliberately small: this runs unattended every
weeknight, and a markdown library that breaks on an upgrade would cost a
report. It handles exactly the constructs the report actually emits.

Email-client constraints shape the output:
  * styles are inlined on every element -- Gmail and Outlook strip or ignore
    <style> blocks in several contexts, so a stylesheet cannot be relied on
  * tables are wrapped in an overflow-x container, because the signal table is
    far wider than a phone and would otherwise force the whole page to scroll
  * no external CSS, fonts or images -- most clients block them by default
"""

from __future__ import annotations

import html
import re

BG = "#f6f7f9"
CARD = "#ffffff"
INK = "#1a1d21"
MUTED = "#6b7280"
LINE = "#e5e7eb"
ACCENT = "#1d4ed8"
GOOD = "#047857"
BAD = "#b91c1c"
WARN = "#b45309"

MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _inline(text: str) -> str:
    """Inline markdown -> HTML. Escapes first, so report text cannot inject markup."""
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`",
               rf'<code style="font-family:{MONO};font-size:12px;background:#f1f5f9;'
               r'padding:1px 4px;border-radius:3px">\1</code>', t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               rf'<a href="\2" style="color:{ACCENT};text-decoration:none">\1</a>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r'<strong style="font-weight:650">\1</strong>', t)
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", t)
    return t


def _cell_style(text: str, header: bool) -> str:
    base = (f"padding:6px 9px;border-bottom:1px solid {LINE};"
            f"font-size:12px;white-space:nowrap;")
    if header:
        return base + f"background:#f8fafc;font-weight:650;text-align:left;color:{MUTED};"
    colour = ""
    stripped = re.sub(r"[*`]", "", text).strip()
    if re.match(r"^[+-]?\d+\.?\d*%$|^[+-]\d+\.?\d*pp$", stripped):
        colour = f"color:{BAD};" if stripped.startswith("-") else f"color:{GOOD};"
    elif "ACT" in stripped:
        colour = f"color:{GOOD};font-weight:700;"
    elif "SETUP" in stripped:
        colour = f"color:{WARN};font-weight:650;"
    return base + colour


def md_to_html(md: str) -> str:
    """Convert the report's markdown subset to email-safe HTML."""
    out: list[str] = []
    lines = md.splitlines()
    i, in_code, list_type = 0, False, None

    def close_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith("```"):
            close_list()
            if in_code:
                out.append("</pre>")
            else:
                out.append(f'<pre style="font-family:{MONO};font-size:12px;background:#f8fafc;'
                           f'border:1px solid {LINE};border-radius:6px;padding:10px;'
                           f'overflow-x:auto;white-space:pre">')
            in_code = not in_code
            i += 1
            continue
        if in_code:
            out.append(html.escape(line))
            i += 1
            continue

        # Table: a header row followed by a |---|---| separator
        if (line.lstrip().startswith("|") and i + 1 < len(lines)
                and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1])):
            close_list()
            rows = []
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append('<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;'
                       f'border:1px solid {LINE};border-radius:6px;margin:12px 0">')
            out.append('<table role="presentation" cellspacing="0" cellpadding="0" '
                       'style="border-collapse:collapse;width:100%">')
            out.append("<tr>" + "".join(
                f'<th style="{_cell_style(c, True)}">{_inline(c)}</th>' for c in header) + "</tr>")
            for r in rows:
                out.append("<tr>" + "".join(
                    f'<td style="{_cell_style(c, False)}">{_inline(c)}</td>' for c in r) + "</tr>")
            out.append("</table></div>")
            continue

        stripped = line.strip()
        if not stripped:
            close_list()
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,})$", stripped):
            close_list()
            out.append(f'<hr style="border:0;border-top:1px solid {LINE};margin:18px 0">')
        elif stripped.startswith("#"):
            close_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped.lstrip("#").strip()
            sizes = {1: 20, 2: 17, 3: 15, 4: 14}
            size = sizes.get(level, 13)
            top = 20 if level <= 2 else 16
            out.append(f'<h{min(level,4)} style="font-size:{size}px;line-height:1.3;margin:{top}px 0 6px;'
                       f'color:{INK};font-weight:700">{_inline(text)}</h{min(level,4)}>')
        elif stripped.startswith(">"):
            close_list()
            out.append(f'<blockquote style="margin:10px 0;padding:8px 12px;border-left:3px solid {LINE};'
                       f'color:{MUTED};font-size:14px">{_inline(stripped.lstrip("> "))}</blockquote>')
        elif re.match(r"^[-*]\s+", stripped):
            if list_type != "ul":
                close_list()
                out.append('<ul style="margin:8px 0;padding-left:20px">')
                list_type = "ul"
            item = _inline(re.sub(r"^[-*]\s+", "", stripped))
            out.append(f'<li style="font-size:14px;line-height:1.5;margin:3px 0;color:{INK}">'
                       f'{item}</li>')
        elif re.match(r"^\d+\.\s+", stripped):
            if list_type != "ol":
                close_list()
                out.append('<ol style="margin:8px 0;padding-left:22px">')
                list_type = "ol"
            item = _inline(re.sub(r"^\d+\.\s+", "", stripped))
            out.append(f'<li style="font-size:14px;line-height:1.5;margin:3px 0;color:{INK}">'
                       f'{item}</li>')
        else:
            close_list()
            out.append(f'<p style="font-size:14px;line-height:1.55;margin:8px 0;color:{INK}">'
                       f'{_inline(stripped)}</p>')
        i += 1

    close_list()
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def _pill(label: str, value, colour: str) -> str:
    return (f'<td style="padding:0 6px 0 0"><div style="background:{colour}1a;border:1px solid {colour}40;'
            f'border-radius:6px;padding:6px 10px;text-align:center">'
            f'<div style="font-size:19px;font-weight:700;color:{colour};line-height:1">{value}</div>'
            f'<div style="font-size:10px;color:{MUTED};text-transform:uppercase;'
            f'letter-spacing:.4px;margin-top:2px">{label}</div></div></td>')


# Gmail clips a message body past roughly 102KB and hides the rest behind a
# "View entire message" link -- which on a phone is exactly where the reader
# gives up. Markdown expands to ~4x its size once inlined styles are added, so
# the source is trimmed well before that.
MAX_MD_CHARS = 12_000


def _truncate_md(md: str, limit: int = MAX_MD_CHARS) -> tuple[str, bool]:
    """Trim at a heading boundary so the email never ends mid-sentence."""
    if len(md) <= limit:
        return md, False
    cut = md[:limit]
    for boundary in ("\n## ", "\n### ", "\n\n"):
        idx = cut.rfind(boundary)
        if idx > limit * 0.5:
            return cut[:idx], True
    return cut, True


def _movers_block(sig: dict, limit: int = 8) -> str:
    """Names that moved unusually today, ranked by sigma rather than percentage.

    Percentage alone ranks these wrong: a 19% day in a name whose normal day is
    8.7% is quieter than a 17% day in one that normally moves 4.2%.
    """
    rows = [r for r in sig.get("signals", []) if (r.get("move") or {}).get("big_move")]
    if not rows:
        return ""
    rows.sort(key=lambda r: -abs(r["move"].get("sigma") or 0))
    rows = rows[:limit]

    items = []
    for r in rows:
        m = r["move"]
        up = m["direction"] == "up"
        col = GOOD if up else BAD
        sigma = f"{abs(m['sigma']):.1f}\u03c3" if m.get("sigma") is not None else ""
        extra = []
        if m.get("excess_1d_pct") is not None:
            extra.append(f"{m['excess_1d_pct']:+.1f}pp vs XBI")
        if m.get("gap_pct") is not None and abs(m["gap_pct"]) >= 3:
            extra.append(f"gap {m['gap_pct']:+.1f}%")
        if m.get("typical_daily_move_pct"):
            extra.append(f"normal day {m['typical_daily_move_pct']}%")
        link = (r.get("links") or {}).get("chart_6m", "#")
        items.append(
            f'<tr><td style="padding:7px 0;border-bottom:1px solid {LINE}">'
            f'<a href="{html.escape(link, quote=True)}" style="text-decoration:none;color:{INK};'
            f'font-weight:700;font-size:14px">{html.escape(r["symbol"])}</a>'
            f'<span style="color:{MUTED};font-size:12px"> · {html.escape(", ".join(extra))}</span>'
            f'</td>'
            f'<td style="padding:7px 0;border-bottom:1px solid {LINE};text-align:right;'
            f'white-space:nowrap">'
            f'<span style="color:{col};font-weight:700;font-size:15px">{m["chg_1d_pct"]:+.1f}%</span>'
            f'<span style="color:{col};font-size:11px;display:block">{sigma}</span></td></tr>'
        )
    return (f'<div style="margin:6px 0 16px"><div style="font-size:11px;text-transform:uppercase;'
            f'letter-spacing:.5px;color:{MUTED};font-weight:700;margin-bottom:4px">'
            f'Big movers vs yesterday</div>'
            f'<table role="presentation" cellspacing="0" cellpadding="0" style="width:100%">'
            f'{"".join(items)}</table></div>')


def _charts_block(sig: dict, limit: int = 14) -> str:
    """Per-name chart links, built from signals.json rather than the report.

    Deliberately independent of the report's own text: the links must be there
    every day whether or not the write-up remembered to include them.
    """
    tier_order = {"ACT": 0, "SETUP": 1, "WATCH": 2}
    rows = [r for r in sig.get("signals", []) if r.get("tier") in tier_order]
    if not rows:
        return ""
    rows.sort(key=lambda r: (tier_order[r["tier"]], r["symbol"]))
    rows = rows[:limit]

    def btn(label: str, url: str, strong: bool = False) -> str:
        colour = ACCENT if strong else MUTED
        return (f'<a href="{html.escape(url, quote=True)}" style="display:inline-block;'
                f'padding:5px 9px;margin:2px 3px 2px 0;border:1px solid {colour}55;'
                f'border-radius:5px;font-size:12px;color:{colour};text-decoration:none;'
                f'background:{colour}0f">{label}</a>')

    items = []
    for r in rows:
        lk = r.get("links") or {}
        if not lk:
            continue
        price = (r.get("price") or {}).get("close")
        tier = r["tier"]
        tcol = {"ACT": GOOD, "SETUP": WARN}.get(tier, ACCENT)
        items.append(
            f'<div style="padding:9px 0;border-bottom:1px solid {LINE}">'
            f'<div style="font-size:14px;font-weight:700;color:{INK};margin-bottom:4px">'
            f'{html.escape(r["symbol"])}'
            f'<span style="font-weight:400;color:{MUTED}"> ${price}</span> '
            f'<span style="color:{tcol};font-size:11px;font-weight:700">{tier}</span></div>'
            + btn("6M", lk.get("chart_6m", "#"), True)
            + btn("3Y", lk.get("chart_3y", "#"), True)
            + btn("10Y", lk.get("chart_10y", "#"), True)
            + btn("Interactive", lk.get("tradingview", "#"))
            + btn("Financials", lk.get("stockanalysis", "#"))
            + (btn("SEC", lk["sec_filings"]) if lk.get("sec_filings") else "")
            + "</div>"
        )
    if not items:
        return ""
    return (f'<div style="margin:6px 0 16px"><div style="font-size:11px;text-transform:uppercase;'
            f'letter-spacing:.5px;color:{MUTED};font-weight:700;margin-bottom:2px">'
            f'Charts</div>{"".join(items)}</div>')


MAX_HTML_BYTES = 95_000


def build_email_html(report_md: str, sig: dict) -> str:
    """Full HTML document, guaranteed to fit under the mail-client clip limit.

    Budgeting by markdown length does not work: a wide markdown table expands
    ~11x once every cell carries an inline style, while prose expands ~3x. So
    the output is measured and re-rendered smaller until it actually fits.
    """
    html_out = _render(report_md, sig, truncated=False)
    if len(html_out.encode("utf-8")) <= MAX_HTML_BYTES:
        return html_out
    budget = len(report_md)
    for _ in range(14):
        budget = int(budget * 0.88)
        trimmed, _ = _truncate_md(report_md, budget)
        html_out = _render(trimmed, sig, truncated=True)
        if len(html_out.encode("utf-8")) <= MAX_HTML_BYTES:
            return html_out
    # Shrinking the body failed to get under the limit -- a pathological report,
    # e.g. one that is almost entirely a very wide table. Returning the oversized
    # document would be silently clipped by the mail client mid-content, so fall
    # back to a document with no report body at all. The summary, movers, charts
    # and the attachment still carry everything that matters.
    return _render(
        "The report was too large to inline without being clipped by your mail "
        "client, so it has been omitted here. **The complete report is attached "
        "as markdown.**",
        sig, truncated=True,
    )


def _render(report_md: str, sig: dict, truncated: bool) -> str:
    signals = sig.get("signals", [])
    counts = {t: sum(1 for r in signals if r.get("tier") == t)
              for t in ("ACT", "SETUP", "WATCH")}
    alerts = sig.get("notify") or []
    session = sig.get("session_date", "")

    if alerts:
        head = "".join(
            f'<div style="font-size:14px;color:{INK};margin:3px 0">'
            f'<strong>{html.escape(a["symbol"])}</strong> '
            f'<span style="color:{MUTED}">${a.get("close")}</span> '
            f'<span style="color:{GOOD};font-weight:650">{html.escape(str(a.get("tier")))}</span>'
            f'</div>' for a in alerts)
        banner = (f'<div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;'
                  f'padding:12px 14px;margin:0 0 14px">'
                  f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;'
                  f'color:{GOOD};font-weight:700;margin-bottom:6px">New alerts</div>{head}</div>')
    else:
        banner = (f'<div style="background:#f8fafc;border:1px solid {LINE};border-radius:8px;'
                  f'padding:12px 14px;margin:0 0 14px;font-size:14px;color:{MUTED}">'
                  f'No new setups today. Nothing to act on.</div>')

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>Biotech desk {html.escape(session)}</title></head>
<body style="margin:0;padding:0;background:{BG};font-family:{SANS};-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">
{len(alerts)} new alert(s) · {counts['ACT']} ACT · {counts['SETUP']} SETUP · {counts['WATCH']} WATCH
</div>
<table role="presentation" cellspacing="0" cellpadding="0" width="100%" style="background:{BG}">
<tr><td align="center" style="padding:16px 10px">
<table role="presentation" cellspacing="0" cellpadding="0" width="100%" style="max-width:680px">
<tr><td style="background:{CARD};border:1px solid {LINE};border-radius:10px;padding:18px 16px">

<div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:{MUTED};font-weight:600">
Biotech desk</div>
<div style="font-size:22px;font-weight:700;color:{INK};margin:2px 0 14px">{html.escape(session)}</div>

<table role="presentation" cellspacing="0" cellpadding="0" style="width:100%;margin-bottom:14px"><tr>
{_pill('Act', counts['ACT'], GOOD)}
{_pill('Setup', counts['SETUP'], WARN)}
{_pill('Watch', counts['WATCH'], ACCENT)}
</tr></table>

{banner}
{_movers_block(sig)}
{_charts_block(sig)}
<hr style="border:0;border-top:1px solid {LINE};margin:4px 0 8px">

{md_to_html(report_md)}
{f'<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:11px 14px;margin:14px 0;font-size:13px;color:{WARN}">Report trimmed here to stay under the mail client clipping limit. The complete report is attached as markdown.</div>' if truncated else ''}

<hr style="border:0;border-top:1px solid {LINE};margin:22px 0 10px">
<p style="font-size:11px;color:{MUTED};line-height:1.5;margin:0">
Generated automatically from SEC EDGAR, Nasdaq, FINRA and ClinicalTrials.gov data.
Research support for your own decisions — not financial advice. The full markdown
report is attached.</p>

</td></tr></table></td></tr></table></body></html>"""
