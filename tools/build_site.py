#!/usr/bin/env python3
"""Assemble the public marketing site into one directory.

    python3 tools/build_site.py [--out _site] [--base-url URL] [--serve]

Reads www/site.toml for the canonical base URL, then:

  1. copies www/ (minus site.toml and README.md) into --out
  2. wraps docs/capability-brief.html as brief.html — same content, plus a
     screen-only nav bar back to the site and the meta/OG tags a print
     stylesheet never needed
  3. copies the brief PDF if it has been built
  4. rewrites every absolute self-URL in index.html and llms.txt from the
     default GitHub Pages address to base_url, so moving to a custom domain
     is a one-line change in site.toml
  5. writes robots.txt and sitemap.xml from the files actually present, and a
     CNAME file when base_url is not a github.io address
  6. fills the template slots in index.html and llms.txt: {{tests}} is counted
     from tests/ every build; {{author:*}} comes from [author] in site.toml;
     {{stat:*}} comes from www/stats.toml (tools/site_stats.py) and the whole
     operating-record band is dropped when that file is absent. The LinkedIn
     link is dropped when [author].linkedin is empty.

Stdlib only, like everything else here. --serve starts http.server on the
result for a local check.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import shutil
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
WWW = ROOT / "www"
DEFAULT_BASE = "https://svedbg.github.io/pharma/"
SKIP = {"site.toml", "stats.toml", "README.md"}


TESTS = ROOT / "tests"


def count_tests() -> int:
    n = 0
    for f in TESTS.glob("test_*.py"):
        n += len(re.findall(r"^\s*(?:async\s+)?def test_\w+", f.read_text(encoding="utf-8"), re.M))
    return n


def human_date(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.strftime('%b')} {d.year}"


def strip_block(text: str, name: str) -> str:
    return re.sub(rf"<!-- {name}:start -->.*?<!-- {name}:end -->", "", text, flags=re.S)


def unwrap_block(text: str, name: str) -> str:
    return text.replace(f"<!-- {name}:start -->", "").replace(f"<!-- {name}:end -->", "")


def fill_templates(out: Path, cfg: dict) -> dict:
    author = cfg.get("author", {})
    a = {
        "name": author.get("name", "Svetoslav Rankov"),
        "title": author.get("title", "engineering lead"),
        "blurb": author.get("blurb", ""),
        "url": author.get("url", "https://sved.net"),
        "linkedin": author.get("linkedin", "").strip(),
    }
    a["url_label"] = re.sub(r"^https?://(www\.)?", "", a["url"]).rstrip("/")
    tests = count_tests()

    stats_path = WWW / "stats.toml"
    stats = None
    if stats_path.exists():
        with stats_path.open("rb") as f:
            st = tomllib.load(f)
        if "since" in st:
            stats = {
                "since": human_date(st["since"]),
                "latest": human_date(st["latest"]),
                "nights": str(st["nights"]),
                "missed": str(st["weeknights_missed"]),
            }

    for name in ("index.html", "llms.txt"):
        p = out / name
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        t = t.replace("{{tests}}", str(tests))
        t = t.replace("{{build:date}}", dt.date.today().isoformat())
        for k, v in a.items():
            t = t.replace(f"{{{{author:{k}}}}}", html.escape(v, quote=True) if k != "url" and k != "linkedin" else v)
        t = unwrap_block(t, "linkedin") if a["linkedin"] else strip_block(t, "linkedin")
        if stats:
            for k, v in stats.items():
                t = t.replace(f"{{{{stat:{k}}}}}", v)
            t = unwrap_block(t, "stats")
        else:
            t = strip_block(t, "stats")
        if name == "index.html":
            same_as = ["https://github.com/svedbg"] + ([a["linkedin"]] if a["linkedin"] else [])
            t = t.replace('"sameAs": ["https://github.com/svedbg"]',
                          '"sameAs": ' + json_list(same_as))
            org = author.get("organization", "").strip()
            if org:
                t = t.replace('"jobTitle": "Senior Director of Software Engineering",',
                              f'"jobTitle": {json_str(author.get("job_title", a["title"]))},\n'
                              f'      "worksFor": {{ "@type": "Organization", "name": {json_str(org)} }},')
        leftover = re.findall(r"{{[a-z:_]+}}", t)
        if leftover:
            print(f"warning: unfilled slots in {name}: {sorted(set(leftover))}", file=sys.stderr)
        p.write_text(t, encoding="utf-8")
    return {"tests": tests, "stats": bool(stats), "linkedin": bool(a["linkedin"])}


def json_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def json_list(vs: list[str]) -> str:
    return "[" + ", ".join(json_str(v) for v in vs) + "]"


def load_config() -> dict:
    cfg_path = WWW / "site.toml"
    if not cfg_path.exists():
        return {"base_url": DEFAULT_BASE}
    with cfg_path.open("rb") as f:
        return tomllib.load(f)


def norm(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def copy_www(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for p in WWW.rglob("*"):
        rel = p.relative_to(WWW)
        if rel.parts[0] in SKIP:
            continue
        dest = out / rel
        if p.is_dir():
            dest.mkdir(exist_ok=True)
        else:
            shutil.copy2(p, dest)


def wrap_brief(out: Path, base: str) -> bool:
    src = ROOT / "docs" / "capability-brief.html"
    if not src.exists():
        return False
    doc = src.read_text(encoding="utf-8")
    title_m = re.search(r"<title>(.*?)</title>", doc, re.S)
    title = html.unescape(title_m.group(1)).strip() if title_m else "The 91% Question"
    desc = ("Why a stock that fell 91% in one day is usually not a buying opportunity, "
            "and how a nightly research system was built to refuse rather than predict. "
            "Case study by Svetoslav Rankov.")
    head_extra = f"""
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{html.escape(desc)}">
<meta name="author" content="Svetoslav Rankov">
<meta name="robots" content="index, follow, max-image-preview:large">
<link rel="canonical" href="{base}brief.html">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Biotech desk">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:url" content="{base}brief.html">
<meta property="og:image" content="{base}og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{base}og.png">
<style media="screen">
  .site-nav {{
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; font-size: 13px;
    display: flex; gap: 18px; align-items: baseline; flex-wrap: wrap;
    padding: 12px 0 14px; margin: 0 0 24px; border-bottom: 1px solid #e3e6ea; color: #6b7280;
  }}
  .site-nav a {{ color: #0f4c81; text-decoration: none; }}
  .site-nav a:hover {{ text-decoration: underline; }}
  .site-nav .home {{ font-weight: 700; }}
  .site-nav .pdf {{ margin-left: auto; }}
  @media (max-width: 600px) {{ .site-nav .pdf {{ margin-left: 0; }} }}
</style>
<style media="print">
  .site-nav {{ display: none; }}
</style>
"""
    pdf_link = ""
    if (out / "the-91-percent-question.pdf").exists():
        pdf_link = '<a class="pdf" href="the-91-percent-question.pdf">Download as PDF</a>'
    nav = (f'<nav class="site-nav" aria-label="Site"><a class="home" href="./">Biotech desk</a>'
           f'<span>The 91% question — long-form brief</span>'
           f'<a href="https://github.com/svedbg/pharma">Source on GitHub</a>{pdf_link}</nav>\n')
    doc = doc.replace("</head>", head_extra + "</head>", 1)
    doc = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + "\n" + nav, doc, count=1)
    (out / "brief.html").write_text(doc, encoding="utf-8")
    return True


def rewrite_urls(out: Path, base: str) -> int:
    if base == DEFAULT_BASE:
        return 0
    n = 0
    for name in ("index.html", "llms.txt"):
        p = out / name
        if p.exists():
            t = p.read_text(encoding="utf-8")
            c = t.count(DEFAULT_BASE)
            p.write_text(t.replace(DEFAULT_BASE, base), encoding="utf-8")
            n += c
    return n


def write_robots_sitemap(out: Path, base: str) -> list[str]:
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {base}sitemap.xml\n", encoding="utf-8")
    today = dt.date.today().isoformat()
    pages = [("", "monthly", "1.0", True)]
    if (out / "brief.html").exists():
        pages.append(("brief.html", "yearly", "0.7", False))
    if (out / "sample-report.html").exists():
        pages.append(("sample-report.html", "monthly", "0.8", False))
    urls = []
    for path, freq, prio, with_image in pages:
        img = ""
        if with_image and (out / "og.png").exists():
            img = (f"\n    <image:image><image:loc>{base}og.png</image:loc>"
                   f"<image:title>Biotech desk — 59 names scanned, nothing to do tonight</image:title></image:image>")
        urls.append(f"  <url>\n    <loc>{base}{path}</loc>\n    <lastmod>{today}</lastmod>\n"
                    f"    <changefreq>{freq}</changefreq>\n    <priority>{prio}</priority>{img}\n  </url>")
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
               '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
               + "\n".join(urls) + "\n</urlset>\n")
    (out / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    return [p[0] or "index.html" for p in pages]


def write_cname(out: Path, base: str) -> bool:
    host = urlparse(base).hostname or ""
    if host.endswith("github.io"):
        return False
    (out / "CNAME").write_text(host + "\n", encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "_site")
    ap.add_argument("--base-url", help="override www/site.toml")
    ap.add_argument("--serve", action="store_true", help="serve the result on :8000")
    a = ap.parse_args()

    cfg = load_config()
    base = norm(a.base_url or cfg.get("base_url", DEFAULT_BASE))

    copy_www(a.out)
    pdf = ROOT / "docs" / "the-91-percent-question.pdf"
    if pdf.exists():
        shutil.copy2(pdf, a.out / pdf.name)
    got_brief = wrap_brief(a.out, base)
    filled = fill_templates(a.out, cfg)
    n_urls = rewrite_urls(a.out, base)
    pages = write_robots_sitemap(a.out, base)
    cname = write_cname(a.out, base)

    for must in ("index.html", "og.png", "fonts/inter-var.woff2"):
        if not (a.out / must).exists():
            print(f"missing: {must}", file=sys.stderr)
            return 1

    shown = a.out.relative_to(ROOT) if a.out.is_relative_to(ROOT) else a.out
    print(f"built {shown} for {base}")
    print(f"  pages: {', '.join(pages)}")
    print(f"  brief: {'wrapped' if got_brief else 'not found'} | pdf: {'yes' if pdf.exists() else 'no (make brief)'}"
          f" | urls rewritten: {n_urls} | CNAME: {'yes' if cname else 'no'}")
    print(f"  tests counted: {filled['tests']} | operating record: {'shown' if filled['stats'] else 'omitted (run tools/site_stats.py)'}"
          f" | linkedin: {'linked' if filled['linkedin'] else 'not set in site.toml'}")

    if a.serve:
        import functools
        import http.server
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(a.out))
        print("serving http://localhost:8000/ — ctrl-c to stop")
        http.server.ThreadingHTTPServer(("", 8000), handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
