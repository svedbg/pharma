"""The public site must spell its domain in exactly one place.

`www/site.toml` holds `base_url`, and `tools/build_site.py` rewrites every
absolute self-URL in the copied pages to it. That rewrite used to walk a list of
two filenames -- index.html and llms.txt -- so `sample-report.html`, which
`tools/make_sample_report.py` generates with the default GitHub Pages address
baked into its canonical and og:url, kept pointing at an address the live site
301s away from. The page nobody edits by hand was the one that went stale, and
nothing said so: the build reported success and the file looked fine on its own.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _build_site():
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "tools" / "build_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_custom_domain_reaches_every_page_the_build_copies(tmp_path):
    build_site = _build_site()
    base = "https://desk.example.net/"

    out = tmp_path / "site"
    build_site.copy_www(out)
    # A page carrying the default address and nothing else: the shape of
    # sample-report.html, which is generated rather than hand-edited.
    (out / "generated-page.html").write_text(
        f'<link rel="canonical" href="{build_site.DEFAULT_BASE}generated-page.html">',
        encoding="utf-8",
    )
    build_site.rewrite_urls(out, base)

    stale = [
        p.relative_to(out)
        for p in sorted(out.rglob("*"))
        if p.is_file()
        and p.suffix in {".html", ".txt", ".xml"}
        and build_site.DEFAULT_BASE in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not stale, f"still pointing at the default address: {stale}"


def test_the_cname_file_follows_the_configured_domain(tmp_path):
    build_site = _build_site()

    out = tmp_path / "site"
    out.mkdir()
    assert build_site.write_cname(out, "https://desk.example.net/") is True
    assert (out / "CNAME").read_text(encoding="utf-8").strip() == "desk.example.net"

    # A github.io base needs no CNAME, and writing one would break the deploy.
    out2 = tmp_path / "pages"
    out2.mkdir()
    assert build_site.write_cname(out2, build_site.DEFAULT_BASE) is False
    assert not (out2 / "CNAME").exists()


def test_the_landing_page_links_its_own_llms_txt():
    """A published llms.txt nothing points at is a file, not a discoverable one.

    It sat unlinked from the first deploy: served at the root, absent from the
    head and the footer, so the only way to reach it was to guess the filename.
    """
    page = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    assert 'rel="alternate"' in page and 'href="llms.txt"' in page.split("</head>")[0], (
        "index.html no longer advertises llms.txt in its head"
    )
    assert page.split("</head>")[1].count('href="llms.txt"') >= 1, (
        "index.html no longer links llms.txt where a reader can see it"
    )


def test_the_brief_is_given_a_screen_layout_without_touching_the_print_one(tmp_path):
    """The brief is one file rendered two ways, and only one of them may move.

    docs/capability-brief.html is typeset for A4: every size is in pt and the
    body has no margin, so on screen it ran the full width of the viewport with
    no gutter. The wrapper adds a centred column for screens only — `make brief`
    prints the PDF from the same document, and a layout rule that reached print
    media would change something that is already published.
    """
    build_site = _build_site()

    out = tmp_path / "site"
    out.mkdir()
    assert build_site.wrap_brief(out, "https://desk.example.net/") is True
    doc = (out / "brief.html").read_text(encoding="utf-8")

    screen = doc.split('<style media="screen">')[1].split("</style>")[0]
    assert "max-width" in screen and "margin: 0 auto" in screen, (
        "the brief no longer gets a centred column on screen"
    )
    assert "padding" in screen, "the brief no longer gets a gutter on screen"
    assert 'url("fonts/inter-var.woff2")' in screen, (
        "the brief no longer gets the site's own screen face"
    )
    assert "https://" not in screen.split("@font-face")[1].split("}")[0], (
        "the brief's screen font is being fetched from somewhere else: the site "
        "loads nothing from a third party, and a blocked font is a silent one"
    )

    printed = doc.split('<style media="print">')[1].split("</style>")[0]
    assert "body" not in printed, (
        "print media restyles the body: the committed PDF would move"
    )


def test_the_brief_counts_its_tests_rather_than_quoting_a_number(tmp_path):
    """The number on the cover is counted at build time, in both renderings.

    It was typed as 87 when the brief was written and was 267 by the time anyone
    checked — and the landing page had taken its own figure from this document,
    so one stale number became two. The slot is filled by the build now, which
    means `make brief` has to print the PDF from the built page: pointed back at
    the raw source it would print the literal "{{tests}}" on the cover of a
    document that gets handed to people.
    """
    src = (ROOT / "docs" / "capability-brief.html").read_text(encoding="utf-8")
    assert "{{tests}}" in src, "the brief has gone back to typing its own test count"

    build_site = _build_site()
    out = tmp_path / "site"
    out.mkdir()
    assert build_site.wrap_brief(out, "https://desk.example.net/") is True
    doc = (out / "brief.html").read_text(encoding="utf-8")
    assert "{{tests}}" not in doc, "the wrapper no longer fills the brief's test count"
    assert f'<div class="n">{build_site.count_tests()}</div>' in doc

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    brief_target = makefile.split("\nbrief:")[1].split("\n\n")[0]
    assert "brief-build/brief.html" in brief_target, (
        "make brief prints from the raw source again: the PDF cover would read {{tests}}"
    )


def test_the_brief_links_its_own_site_and_keeps_its_sizes_reachable():
    """Two things the screen typography depends on.

    The author's site was printed as plain text in three places — a byline, a
    call to action and a footer — on a page whose whole purpose is to be read by
    someone deciding whether to get in touch. And every font size has to live in
    the stylesheet: the screen rules scale the document by ~1.19, and a size set
    inline beats them, so an inline one comes out smaller than the body text
    around it while looking perfectly correct in the PDF.
    """
    src = (ROOT / "docs" / "capability-brief.html").read_text(encoding="utf-8")
    assert src.count('href="https://sved.net"') == 3, "a sved.net mention is back to plain text"
    assert 'href="https://github.com/svedbg/pharma"' in src

    body = src.split("<body>")[1]
    assert "font-size" not in body, (
        "an inline font size is out of reach of the screen-only scale"
    )
