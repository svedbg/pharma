"""Local config parsing, email rendering, and the stdlib-only guarantee."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import localconfig
from render_email import MAX_HTML_BYTES, build_email_html, md_to_html

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ config parsing


@pytest.mark.parametrize(("raw", "expected"), [
    ("1  # email every run", "1"),
    ("0", "0"),
    ("smtp.gmail.com", "smtp.gmail.com"),
    ("  spaced  # note", "spaced"),
    ("", ""),
])
def test_inline_comments_are_stripped(raw, expected):
    """`EMAIL_ALWAYS=1  # explanation` previously parsed as the whole string, so
    the `== "1"` check failed and email was silently disabled."""
    assert localconfig._clean_value(raw) == expected


@pytest.mark.parametrize("raw", ["p@ss#word1", "abc#def"])
def test_hash_without_leading_space_is_part_of_the_value(raw):
    # Otherwise a password containing '#' would be truncated into a wrong secret.
    assert localconfig._clean_value(raw) == raw


def test_quoted_values_are_taken_verbatim():
    assert localconfig._clean_value('"p@ss w#rd"') == "p@ss w#rd"


def test_sec_contact_refuses_a_placeholder(monkeypatch):
    """SEC throttles generic user agents, so a silent default would cause
    mysterious rate limiting rather than an obvious failure."""
    monkeypatch.setattr(localconfig, "load", lambda: {"SEC_CONTACT_EMAIL": "you@example.com"})
    with pytest.raises(SystemExit) as e:
        localconfig.sec_contact()
    assert "SEC_CONTACT_EMAIL" in str(e.value)


def test_sec_contact_accepts_a_real_address(monkeypatch):
    monkeypatch.setattr(localconfig, "load", lambda: {"SEC_CONTACT_EMAIL": "a@b.org"})
    assert localconfig.sec_contact() == "a@b.org"


# --------------------------------------------------------------- email render


def test_markdown_table_becomes_a_scrollable_html_table():
    html = md_to_html("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<table" in html
    # The signal table is wider than a phone; it must scroll inside its own box
    # rather than forcing the whole page sideways.
    assert "overflow-x:auto" in html


def test_markdown_links_and_emphasis_render():
    html = md_to_html("See [Finviz](https://example.org) and **bold**.")
    assert 'href="https://example.org"' in html
    assert "<strong" in html


def test_report_text_cannot_inject_markup():
    html = md_to_html("A <script>alert(1)</script> line")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_email_always_fits_under_the_clip_limit():
    """Gmail clips past ~102KB. Budgeting by markdown length failed because a
    wide table expands ~11x while prose expands ~3x, so the output is measured."""
    sig = {"session_date": "2026-08-14", "signals": [], "notify": []}
    row = "| AAAA | B | $1.00 | +1.0% | -2.0% | +1pp | -3% | 10 | 30 | 0.1 | 5q | - | WATCH |\n"
    huge = "# Report\n\n" + ("| a | b |\n|---|---|\n" + row * 400)
    html = build_email_html(huge, sig)
    assert len(html.encode()) <= MAX_HTML_BYTES
    assert "trimmed here" in html


def test_small_report_is_not_truncated():
    sig = {"session_date": "2026-08-14", "signals": [], "notify": []}
    html = build_email_html("# Report\n\nNothing to do today.", sig)
    assert "trimmed here" not in html
    assert "Nothing to do today" in html


# ---------------------------------------------------- stdlib-only guarantee


def test_runtime_has_no_third_party_imports():
    """The desk runs unattended from a timer with no virtualenv. A third-party
    import would mean an upgrade elsewhere on the machine can break the run."""
    local = {p.stem for p in (ROOT / "scripts").glob("*.py")}
    external = set()
    for f in sorted((ROOT / "scripts").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                external.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                external.add(node.module.split(".")[0])
    third_party = sorted(m for m in external
                         if m not in sys.stdlib_module_names and m not in local)
    assert not third_party, f"runtime must stay stdlib-only, found: {third_party}"


def test_modules_import_without_any_configuration(monkeypatch, tmp_path):
    """Importing a module must never require configuration.

    fetch.py used to resolve the SEC contact address at module level, so
    `import fetch` raised SystemExit whenever the setting was absent. That broke
    CI, broke a fresh clone's first test run, and would break any tool that
    merely imports the module. Configuration is needed to make a request, not to
    define a function.
    """
    import importlib
    # No environment variable and no config file anywhere.
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    monkeypatch.setattr(localconfig, "CONFIG_FILES", (tmp_path / "absent.env",))

    for name in ("fetch", "signals", "notify", "paper", "screen", "propose_zones"):
        importlib.reload(importlib.import_module(name))


def test_sec_contact_is_still_demanded_before_a_request(monkeypatch, tmp_path):
    """Lazy must not mean optional -- the failure moves, it does not disappear."""
    import fetch
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    monkeypatch.setattr(localconfig, "CONFIG_FILES", (tmp_path / "absent.env",))
    monkeypatch.setattr(fetch, "_SEC_UA", None)
    with pytest.raises(SystemExit, match="SEC_CONTACT_EMAIL"):
        fetch.sec_ua()


@pytest.mark.parametrize("address", [
    "you@example.org",      # the value actually shipped in pharma.env.example
    "you@example.com",
    "someone@example.net",
    "dev@myhost.test",
    "me@localhost",
    "nodomain",
    "",
])
def test_placeholder_addresses_are_refused(address):
    """The original check looked for the substring 'example.com' while the
    template shipped 'you@example.org', so anyone following the README verbatim
    sent SEC a placeholder and got throttled with no warning."""
    import localconfig
    assert localconfig._is_placeholder(address) is True


@pytest.mark.parametrize("address", [
    "svedbg@users.noreply.github.com",
    "research@exampleclinic.co.uk",   # contains 'example' but is a real domain
    "a.b+tag@sub.domain.org",
])
def test_real_addresses_are_accepted(address):
    import localconfig
    assert localconfig._is_placeholder(address) is False
