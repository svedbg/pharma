#!/usr/bin/env python3
"""Publish one real nightly report as www/sample-report.html, redacted.

    python3 tools/make_sample_report.py reports/2026-09-08.md [--anonymize] [--dry-run]

The marketing page shows an illustrative night. A real one converts better and
settles the honesty question, but reports carry your trading plan: entry zones,
invalidation levels, position sizes, broker routing, paper P&L. This script
strips those before the file ever lands in www/.

What it removes, always:
  - whole sections whose heading matches --drop (default: paper, position,
    routing, broker, sizing, exposure)
  - every dollar figure or percentage on a line that mentions an entry zone,
    invalidation, stop, size, allocation or broker
  - anything between <!-- private --> and <!-- /private --> markers, if you use them

With --anonymize, every ticker in watchlist.toml becomes a stable placeholder
(A1, A2, B1, L1 ... by bucket) so the sample shows the shape of a night without
publishing your names.

--dry-run prints the redacted markdown and writes nothing. Read it before you
publish; the script is a filter, not a guarantee. Uses scripts/render_email.py
for the HTML so the sample renders exactly like the local archive does.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from render_email import md_to_html  # noqa: E402

SENSITIVE_LINE = re.compile(
    r"entry[ _-]?(zone|low|high)|invalidation|stop[- ]?loss|\bstop\b|position size|"
    r"\bsize\b|sizing|allocat|broker|route|routing|% of (allocated )?capital|shares? at \$",
    re.I,
)
FIGURE = re.compile(r"\$\s?\d[\d,]*(\.\d+)?[kKmMbB]?|\b\d[\d,]*(\.\d+)?\s?%|\b\d+(\.\d+)?\s?(shares|sh)\b", re.I)
PRIVATE_BLOCK = re.compile(r"<!--\s*private\s*-->.*?<!--\s*/private\s*-->", re.S | re.I)
DEFAULT_DROP = "paper|position|routing|broker|sizing|exposure"


def drop_sections(md: str, pattern: str) -> tuple[str, list[str]]:
    rx = re.compile(pattern, re.I)
    out, dropped, skipping, level, in_fence = [], [], False, 0, False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if not skipping:
                out.append(line)
            continue
        # A bootstrap block's own "# TICKER" TOML comments look exactly like
        # a level-1 markdown heading to this line-by-line parser, and a
        # fenced code block is the one place "#" never marks a heading.
        # Without this guard the first such comment reads as lvl=1 <= the
        # dropped section's level and ends the skip immediately, so nearly
        # all of "## Thesis bootstrap" leaked straight back into the output.
        m = None if in_fence else re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            lvl, title = len(m.group(1)), m.group(2).strip()
            if skipping and lvl <= level:
                skipping = False
            if not skipping and rx.search(title):
                skipping, level = True, lvl
                dropped.append(title)
                continue
        if not skipping:
            out.append(line)
    return "\n".join(out), dropped


def redact_lines(md: str) -> tuple[str, int]:
    n = 0
    out = []
    for line in md.splitlines():
        if SENSITIVE_LINE.search(line):
            new, c = FIGURE.subn("[redacted]", line)
            n += c
            out.append(new)
        else:
            out.append(line)
    return "\n".join(out), n


def load_tickers() -> dict[str, str]:
    wl = ROOT / "watchlist.toml"
    if not wl.exists():
        return {}
    with wl.open("rb") as f:
        data = tomllib.load(f)
    counters: dict[str, int] = {}
    mapping: dict[str, str] = {}
    for t in data.get("ticker", []):
        sym = str(t.get("symbol", "")).upper()
        if not sym:
            continue
        bucket = str(t.get("tier", "B")).upper()[:1]
        bucket = bucket if bucket in "ABL" else "B"
        counters[bucket] = counters.get(bucket, 0) + 1
        mapping[sym] = f"{bucket}{counters[bucket]}"
    return mapping


def anonymize(md: str, mapping: dict[str, str]) -> tuple[str, int]:
    if not mapping:
        return md, 0
    n = 0
    for sym in sorted(mapping, key=len, reverse=True):
        # Biotech drug candidates are routinely named TICKER-catalogNumber
        # (ORIC-944, VRDN-008). Swapping only the ticker and leaving the
        # number behind still names the asset, and the asset names the
        # company just as surely as the ticker did.
        md, c = re.subn(rf"\b{re.escape(sym)}(?:-\d+)?\b", mapping[sym], md)
        n += c
    return md, n


HEADING_NAME = re.compile(r"^(### \S+) — [^\[\n]+(\[)", re.M)
LINKS_ONLY_LINE = re.compile(
    r"(?m)^(?:\[[^\]]+\]\([^)]+\)[ \t]*(?:·[ \t]*)?)+\n?"
)
TRAILING_CITATION = re.compile(
    r"[ \t]*(?:\[[^\]]+\]\(https?://[^)]+\)[ \t]*(?:·[ \t]*)?)+$", re.M
)


def strip_identity(md: str) -> str:
    """Drop everything a ticker swap alone can't hide: the company name
    printed right next to the placeholder in every heading, the per-name
    research links (Financials/EDGAR embed the real slug and CIK, neither
    of which is a ticker-shaped string a text substitution can catch), and
    news-citation links, whose URLs are titled after the company they're
    about and so can't be laundered without breaking them."""
    md = HEADING_NAME.sub(r"\1 \2", md)
    md = LINKS_ONLY_LINE.sub("", md)
    md = TRAILING_CITATION.sub("", md)
    return md


STYLE = """
  :root{color-scheme:dark;--bg:#1a1c20;--panel:#2b2e35;--panel-2:#35383f;--line:#454851;--fg:#f5f6f7;--fg-2:#d2d3d8;--fg-3:#a8a9b0;--fg-4:#7d7e87;--mint:#34e0a1;--red:#ff6b70}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg-3);font:1.0625rem/1.7 "Inter",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
  @font-face{font-family:"Inter";font-weight:300 800;font-display:swap;src:url("fonts/inter-var.woff2") format("woff2")}
  @font-face{font-family:"JetBrains Mono";font-weight:400;font-display:swap;src:url("fonts/jetbrains-mono-400.woff2") format("woff2")}
  a{color:var(--mint)} a:hover{color:var(--fg)}
  .top{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line)}
  .top .in{max-width:52rem;margin:0 auto;padding:.8rem 1.25rem;display:flex;gap:.6rem;align-items:center;font:.75rem "JetBrains Mono",monospace;color:var(--fg-4)}
  .top .in a{color:var(--fg);text-decoration:none} .top .in .r{margin-left:auto;color:var(--fg-4);text-decoration:none}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--mint)}
  main{max-width:52rem;margin:0 auto;padding:2.5rem 1.25rem 4rem}
  .note{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--mint);border-radius:0 8px 8px 0;padding:.9rem 1.1rem;font-size:.9375rem;color:var(--fg-2);margin:0 0 2.5rem}
  .note code{font:.8125rem "JetBrains Mono",monospace;color:var(--fg)}
  h1,h2,h3{color:var(--fg);font-weight:500;letter-spacing:-.02em;line-height:1.2}
  h1{font-size:clamp(1.8rem,4.5vw,2.6rem);margin:0 0 1rem} h2{font-size:1.5rem;margin:2.5rem 0 .8rem;padding-top:1.5rem;border-top:1px solid var(--line)} h3{font-size:1.125rem;margin:1.6rem 0 .4rem}
  p{margin:0 0 1rem} strong{color:var(--fg);font-weight:500} em{color:var(--fg-2)}
  code,pre{font-family:"JetBrains Mono",monospace} code{font-size:.85em;background:var(--panel);border:1px solid var(--line);padding:.06em .35em;border-radius:4px;color:var(--fg-2)}
  pre{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:1rem;overflow-x:auto;font-size:.8125rem;line-height:1.7;color:var(--fg-2)} pre code{border:0;background:none;padding:0}
  .scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);border-radius:6px;margin:1rem 0 1.5rem}
  table{width:100%;border-collapse:collapse;font-size:.9rem}
  th,td{text-align:left;padding:.55rem .75rem .55rem 0;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}
  th{font:.75rem "JetBrains Mono",monospace;color:var(--fg-4)} td{color:var(--fg-3)}
  .pos{color:var(--mint)} .neg{color:var(--red)}
  .act{color:#052919;background:var(--mint);font:.6875rem "JetBrains Mono",monospace;padding:.1rem .45rem;border-radius:4px}
  .setup{color:var(--fg);background:var(--panel-2);border:1px solid var(--line);font:.6875rem "JetBrains Mono",monospace;padding:.1rem .45rem;border-radius:4px}
  blockquote{margin:1.25rem 0;padding:.2rem 0 .2rem 1.1rem;border-left:2px solid var(--mint);color:var(--fg-2)}
  ul,ol{padding-left:1.2rem} li{margin-bottom:.35rem} li::marker{color:var(--fg-4)}
  hr{border:0;border-top:1px solid var(--line);margin:2rem 0}
  footer{max-width:52rem;margin:0 auto;padding:1.5rem 1.25rem 3rem;border-top:1px solid var(--line);font-size:.875rem;color:var(--fg-4)}
"""


def page(body_html: str, day: str, redactions: int, dropped: list[str], anon: bool) -> str:
    desc = (f"A real nightly report from the biotech desk, {day}, published with entry zones, "
            f"invalidation levels and sizing redacted. Research support, not financial advice.")
    what = [f"{redactions} figure{'s' if redactions != 1 else ''} redacted"]
    if dropped:
        what.append(f"{len(dropped)} section{'s' if len(dropped) != 1 else ''} removed ({', '.join(dropped)})")
    if anon:
        what.append("tickers replaced with bucket placeholders")
        what.append("company names, per-name research links and news-citation links removed")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>A real nightly report — {html.escape(day)} — Biotech desk</title>
<meta name="description" content="{html.escape(desc)}">
<meta name="author" content="Svetoslav Rankov">
<meta name="robots" content="index, follow, max-image-preview:large">
<link rel="canonical" href="https://svedbg.github.io/pharma/sample-report.html">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Biotech desk">
<meta property="og:title" content="What the desk actually sent on {html.escape(day)}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:url" content="https://svedbg.github.io/pharma/sample-report.html">
<meta property="og:image" content="https://svedbg.github.io/pharma/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://svedbg.github.io/pharma/og.png">
<style>{STYLE}</style>
</head>
<body>
<div class="top"><div class="in"><span class="dot" aria-hidden="true"></span><a href="./">biotech desk</a><span>sample report · {html.escape(day)}</span><a class="r" href="https://github.com/svedbg/pharma">source</a></div></div>
<main>
<div class="note">This is the actual report the desk wrote on {html.escape(day)}, as it arrived in the inbox — with {html.escape("; ".join(what))}. Nothing else was edited beyond what's listed here. Every remaining figure was read from a filing or a price feed by <code>fetch.py</code>; the prose is the analysis pass working over that data.</div>
{body_html}
</main>
<footer>Research support for the owner's own decisions. Not financial advice; the desk places no orders. <a href="./">Back to the site</a> · <a href="https://github.com/svedbg/pharma">github.com/svedbg/pharma</a></footer>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", type=Path, help="reports/YYYY-MM-DD.md")
    ap.add_argument("--anonymize", action="store_true", help="replace watchlist tickers with bucket placeholders")
    ap.add_argument("--drop", default=DEFAULT_DROP, help="regex; sections whose heading matches are removed")
    ap.add_argument("--out", type=Path, default=ROOT / "www" / "sample-report.html")
    ap.add_argument("--dry-run", action="store_true", help="print redacted markdown, write nothing")
    a = ap.parse_args()

    md = a.report.read_text(encoding="utf-8")
    day_m = re.search(r"(\d{4}-\d{2}-\d{2})", a.report.name)
    day = day_m.group(1) if day_m else dt.date.today().isoformat()
    day_h = dt.date.fromisoformat(day).strftime("%-d %B %Y")

    md, priv = PRIVATE_BLOCK.subn("", md)
    md, dropped = drop_sections(md, a.drop)
    md, n_fig = redact_lines(md)
    n_anon = 0
    if a.anonymize:
        md = strip_identity(md)
        md, n_anon = anonymize(md, load_tickers())

    if a.dry_run:
        print(md)
        print(f"\n--- dry run: {n_fig} figures redacted, {len(dropped)} sections dropped, "
              f"{priv} private blocks, {n_anon} ticker mentions replaced ---", file=sys.stderr)
        return 0

    body = md_to_html(md, inline_styles=False)
    a.out.write_text(page(body, day_h, n_fig, dropped, a.anonymize), encoding="utf-8")
    shown = a.out.relative_to(ROOT) if a.out.is_relative_to(ROOT) else a.out
    print(f"wrote {shown}: {n_fig} figures redacted, {len(dropped)} sections dropped"
          f"{', tickers anonymized' if a.anonymize else ''}")
    print("read it before you push — this is a filter, not a guarantee. then: make www")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
