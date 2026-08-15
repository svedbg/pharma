#!/usr/bin/env python3
"""Build a local, browsable archive of the daily reports.

Reports accumulate one markdown file per weekday, which is fine for grep and
awful for reading. This renders them into a small static site with an index and
prev/next navigation, so months of history stay navigable.

Deliberately **local only**. The reports contain entry zones, invalidation
levels and broker routing -- the same material `watchlist.toml` is gitignored to
protect -- so `site/` is gitignored too and nothing is uploaded anywhere. Open
it straight from disk:

    make site && xdg-open site/index.html

Reuses md_to_html() from render_email.py rather than carrying a second markdown
converter, but styles for a screen instead of a mail client: a real stylesheet,
sticky navigation, and no inline-style contortions.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from render_email import md_to_html

REPORTS = ROOT / "reports"
SUMMARIES = ROOT / "data" / "summaries"
SITE = ROOT / "site"

STYLE = """
:root {
  --bg:#f7f8fa; --card:#fff; --ink:#14181d; --body:#2b3138; --muted:#6b7280;
  --line:#e4e7eb; --accent:#0f4c81; --good:#0b6b43; --warn:#b45309; --bad:#a41f1f;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1216; --card:#161b21; --ink:#e8ebee; --body:#c8ced5;
          --muted:#8b949e; --line:#252c34; --accent:#6aa9e0; --good:#3fb984;
          --warn:#e0a154; --bad:#e06c6c; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--body);
       font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:900px; margin:0 auto; padding:0 20px 60px; }
nav { position:sticky; top:0; background:var(--card); border-bottom:1px solid var(--line);
      z-index:10; margin-bottom:24px; }
nav .inner { max-width:900px; margin:0 auto; padding:12px 20px; display:flex;
             gap:12px; align-items:center; flex-wrap:wrap; }
nav a { color:var(--accent); text-decoration:none; font-weight:600; font-size:14px; }
nav a:hover { text-decoration:underline; }
nav a.off { color:var(--muted); pointer-events:none; }
nav .spacer { flex:1; }
select { background:var(--card); color:var(--body); border:1px solid var(--line);
         border-radius:6px; padding:5px 8px; font-size:14px; }
h1 { font-size:30px; line-height:1.15; margin:24px 0 6px; color:var(--ink);
     letter-spacing:-.5px; }
h2 { font-size:20px; margin:30px 0 8px; color:var(--ink); padding-bottom:5px;
     border-bottom:1px solid var(--line); }
h3 { font-size:16px; margin:22px 0 5px; color:var(--ink); }
p, li { color:var(--body); }
a { color:var(--accent); }
code { background:rgba(127,127,127,.14); padding:1px 5px; border-radius:4px;
       font-size:13px; }
pre { background:var(--card); border:1px solid var(--line); border-radius:8px;
      padding:12px; overflow-x:auto; }
blockquote { border-left:3px solid var(--line); margin:12px 0; padding:4px 14px;
             color:var(--muted); }
table { border-collapse:collapse; width:100%; font-size:13px; margin:12px 0; }
th { text-align:left; padding:8px 9px; border-bottom:2px solid var(--line);
     font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); }
td { padding:8px 9px; border-bottom:1px solid var(--line); }
tbody tr:hover { background:rgba(127,127,127,.06); }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:8px; margin:12px 0; }
.scroll table { margin:0; }
.sub { color:var(--muted); font-size:14px; margin:0 0 18px; }
.pill { display:inline-block; padding:2px 8px; border-radius:20px; font-size:12px;
        font-weight:700; }
.pill.act { background:rgba(11,107,67,.15); color:var(--good); }
.pill.setup { background:rgba(180,83,9,.15); color:var(--warn); }
.pill.watch { background:rgba(15,76,129,.13); color:var(--accent); }
.pill.quiet { background:rgba(127,127,127,.12); color:var(--muted); }
.idx td.d { white-space:nowrap; font-weight:600; }
.idx td.head { color:var(--muted); font-size:13px; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
         gap:10px; margin:18px 0 26px; }
.stats div { background:var(--card); border:1px solid var(--line); border-radius:8px;
             padding:12px 14px; }
.stats .n { font-size:22px; font-weight:700; color:var(--ink); }
.stats .l { font-size:11px; text-transform:uppercase; letter-spacing:.6px;
            color:var(--muted); margin-top:2px; }
footer { margin-top:40px; padding-top:14px; border-top:1px solid var(--line);
         color:var(--muted); font-size:12px; }
"""


def read_summary(day: str) -> dict:
    f = SUMMARIES / f"{day}.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return {}


def headline(md: str) -> str:
    """First substantive sentence under the bottom-line heading."""
    m = re.search(r"^##+\s*Bottom line\s*$(.+?)^##", md, re.S | re.M | re.I)
    text = m.group(1) if m else md
    text = re.sub(r"[*_`#\[\]]|\(https?://[^)]+\)", "", text)
    for line in (ln.strip() for ln in text.splitlines()):
        if len(line) > 25:
            return line[:200]
    return "—"


def tier_pill(summary: dict) -> str:
    t = summary.get("tiers") or {}
    if t.get("ACT"):
        return f'<span class="pill act">{t["ACT"]} ACT</span>'
    if t.get("SETUP"):
        return f'<span class="pill setup">{t["SETUP"]} SETUP</span>'
    if t.get("WATCH"):
        return f'<span class="pill watch">{t["WATCH"]} watch</span>'
    return '<span class="pill quiet">quiet</span>'


def page(title: str, body: str, nav: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}</style></head>
<body><nav><div class="inner">{nav}</div></nav><div class="wrap">{body}
<footer>Local archive — not published. Generated by scripts/publish.py.
Research support for your own decisions, not financial advice.</footer>
</div></body></html>"""


def build() -> int:
    if not REPORTS.exists():
        print(f"no reports at {REPORTS}", file=sys.stderr)
        return 1
    days = sorted(
        p for p in REPORTS.glob("*.md")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)
    )
    if not days:
        print("no dated reports yet — run ./run_daily.sh first", file=sys.stderr)
        return 1

    SITE.mkdir(exist_ok=True)
    order = [p.stem for p in days]
    options = "".join(
        f'<option value="{d}.html">{d}</option>' for d in reversed(order)
    )

    for i, path in enumerate(days):
        day = path.stem
        prev_d = order[i - 1] if i > 0 else None
        next_d = order[i + 1] if i < len(order) - 1 else None
        summary = read_summary(day)

        nav = (
            f'<a href="{prev_d}.html">← {prev_d}</a>' if prev_d
            else '<a class="off">← older</a>'
        )
        nav += ' <a href="index.html">index</a> '
        nav += (
            f'<a href="{next_d}.html">{next_d} →</a>' if next_d
            else '<a class="off">newer →</a>'
        )
        nav += ('<span class="spacer"></span>'
                f'<select onchange="location=this.value">{options}</select>')
        nav = nav.replace(f'<option value="{day}.html">',
                          f'<option value="{day}.html" selected>')

        t = summary.get("tiers") or {}
        stats = ""
        if t:
            cat = summary.get("next_catalyst") or {}
            stats = (
                '<div class="stats">'
                f'<div><div class="n">{t.get("ACT",0)}</div><div class="l">Act</div></div>'
                f'<div><div class="n">{t.get("SETUP",0)}</div><div class="l">Setup</div></div>'
                f'<div><div class="n">{t.get("WATCH",0)}</div><div class="l">Watch</div></div>'
                f'<div><div class="n">{len(summary.get("vetoed") or [])}</div>'
                '<div class="l">Vetoed</div></div>'
                f'<div><div class="n">{len(summary.get("alerts") or [])}</div>'
                '<div class="l">Alerts</div></div>'
                + (f'<div><div class="n">{cat["days_until"]}d</div>'
                   f'<div class="l">{html.escape(cat["symbol"])} {html.escape(cat.get("kind") or "")}</div></div>'
                   if cat else "")
                + '</div>'
            )

        body = md_to_html(path.read_text())
        # Tables need their own scroll container on a phone.
        body = body.replace("<table", '<div class="scroll"><table').replace(
            "</table>", "</table></div>")
        (SITE / f"{day}.html").write_text(
            page(f"Biotech desk — {day}", stats + body, nav))

    # ---- index -------------------------------------------------------------
    rows = []
    for path in reversed(days):
        day = path.stem
        s = read_summary(day)
        md = path.read_text()
        alerts = s.get("alerts") or []
        exits = s.get("exits") or []
        flags = ""
        if alerts:
            flags += f' <span class="pill setup">{", ".join(alerts)}</span>'
        if exits:
            flags += f' <span class="pill act">exit: {", ".join(exits)}</span>'
        cat = s.get("next_catalyst") or {}
        rows.append(
            f'<tr><td class="d"><a href="{day}.html">{day}</a><br>'
            f'<span style="font-weight:400;color:var(--muted);font-size:12px">'
            f'{datetime.strptime(day, "%Y-%m-%d"):%a}</span></td>'
            f'<td>{tier_pill(s)}{flags}</td>'
            f'<td class="head">{html.escape(headline(md))}</td>'
            f'<td class="head" style="white-space:nowrap">'
            f'{(html.escape(cat["symbol"]) + " " + str(cat["days_until"]) + "d") if cat else "—"}</td></tr>'
        )

    total_alerts = sum(len(read_summary(p.stem).get("alerts") or []) for p in days)
    index_body = (
        "<h1>Biotech desk</h1>"
        f'<p class="sub">{len(days)} report'
        f'{"s" if len(days) != 1 else ""} · '
        f'{order[0]} to {order[-1]} · {total_alerts} alert'
        f'{"s" if total_alerts != 1 else ""} raised</p>'
        '<div class="scroll"><table class="idx"><thead><tr>'
        "<th>Date</th><th>Signal</th><th>Bottom line</th><th>Next catalyst</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    (SITE / "index.html").write_text(
        page("Biotech desk — archive", index_body,
             '<a href="index.html">index</a><span class="spacer"></span>'
             f'<select onchange="location=this.value">{options}</select>'))

    print(f"built {len(days)} page(s) -> {SITE}/index.html")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the local report archive")
    ap.add_argument("--open", action="store_true", help="open the index afterwards")
    args = ap.parse_args()
    rc = build()
    if rc == 0 and args.open:
        import subprocess
        subprocess.run(["xdg-open", str(SITE / "index.html")], check=False)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
