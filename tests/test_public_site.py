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
