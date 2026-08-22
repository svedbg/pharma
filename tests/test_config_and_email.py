"""Local config parsing, email rendering, and the stdlib-only guarantee."""

from __future__ import annotations

import ast
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

import localconfig
import notify
from render_email import BAD, GOOD, MAX_HTML_BYTES, build_email_html, md_to_html

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


def test_a_quote_in_a_link_cannot_escape_the_href_attribute():
    """Escaping the text was not enough on its own. `html.escape(..., quote=False)`
    leaves the quote character alone and the URL went straight into `href="..."`,
    so a link target carrying a quote closed the attribute and opened one of its
    own -- rendering a live event handler. Inert in a mail client, but publish.py
    reuses this converter for site/, which is opened in a real browser, and the
    report is written from fetched web content rather than trusted input.
    """
    rendered = md_to_html('[x](https://h.example/a"onmouseover="alert(1))')

    # The payload may survive as text inside the href value -- that is harmless
    # and is what escaping looks like. What must not survive is a second
    # *attribute*, so the anchor is parsed and its attribute names checked
    # rather than the raw string being grepped.
    class _Anchors(HTMLParser):
        def __init__(self):
            super().__init__()
            self.attrs = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.attrs.append({k for k, _ in attrs})

    p = _Anchors()
    p.feed(rendered)
    assert p.attrs == [{"href", "style"}], f"injected attribute: {p.attrs}"
    assert "&quot;onmouseover=&quot;" in rendered


@pytest.mark.parametrize("url", [
    "javascript:alert(1",                 # the regex stops at the first ')'
    "javascript:alert%281%29",            # so this is the working payload
    "JaVaScRiPt:alert%281%29",
    "java\x00script:alert%281%29",        # NUL is not \s, so it reaches the href
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
])
def test_a_link_cannot_smuggle_an_executable_scheme_into_the_archive(url):
    """Quote-escaping closed the attribute-breakout hole and left the scheme
    alone, so `[x](javascript:alert%281%29)` still rendered as a live handler --
    no quote required, nothing to escape.

    Same threat model as the breakout, and the one CLAUDE.md already names: the
    report is written from WebFetch and WebSearch content, and publish.py feeds
    this converter into site/, which is opened in a real browser rather than a
    sandboxed mail client.

    The invariant is that no anchor survives, not how it was stopped: a target
    containing whitespace never matches the link regex in the first place, while
    these reach _link and are refused there.
    """
    rendered = md_to_html(f"[click me]({url})", inline_styles=False)
    assert "<a href" not in rendered, rendered
    assert "click me" in rendered
    assert "unsafe link removed" in rendered


@pytest.mark.parametrize("url", [
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=1",
    "http://example.org/a",
    "mailto:someone@example.org",
    "#section",
])
def test_the_links_the_report_actually_uses_still_render(url):
    """The allowlist must not break chart_links, EDGAR or in-page anchors."""
    rendered = md_to_html(f"[x]({url})", inline_styles=False)
    assert "<a href=" in rendered, rendered
    assert "unsafe link removed" not in rendered


def test_the_archive_gets_semantic_markup_not_mail_client_styles():
    """publish.py ships a stylesheet with a dark palette, and an inline style
    beats a stylesheet on every element -- so the archive was rendered in
    near-black #1a1d21 body text on a #0f1216 dark background. The stylesheet was
    written, correct, and completely overridden. The site target must therefore
    emit no inline CSS at all."""
    md = "# Title\n\nSome **text** with a [link](https://example.org).\n\n- item\n"
    site = md_to_html(md, inline_styles=False)
    assert 'style="' not in site, "an inline style overrides the archive's theme"
    assert "<h1>" in site and "<p>" in site and "<li>" in site
    # The link target still has to survive as an attribute.
    assert 'href="https://example.org"' in site


def test_the_email_target_is_unchanged_and_still_inlines_everything():
    """Mail clients strip <style> blocks, so the default target must keep
    carrying CSS on every element."""
    email = md_to_html("# Title\n\nSome text.\n")
    assert 'style="font-size:20px' in email
    assert "color:" in email


def test_each_table_gets_exactly_one_scroll_container():
    """publish.py used to bolt a second container on with a string replace, so
    every table in the archive sat inside two nested overflow boxes with the
    mail client's light-mode border baked into the outer one."""
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n\n| C | D |\n|---|---|\n| 3 | 4 |"
    site = md_to_html(md, inline_styles=False)
    assert site.count("<table") == 2
    assert site.count('class="scroll"') == 2
    # And the email keeps its own inline-styled wrapper, one per table.
    assert md_to_html(md).count("overflow-x:auto") == 2


def test_table_cells_are_classified_the_same_way_for_both_targets():
    """The two targets must agree about which cells are significant and differ
    only in how they say so, or the site and the email disagree about the data."""
    md = "| Chg | Tier |\n|---|---|\n| -2.5% | ACT |"
    site = md_to_html(md, inline_styles=False)
    email = md_to_html(md)
    assert '<td class="neg">' in site and '<td class="act">' in site
    assert f"color:{BAD}" in email and f"color:{GOOD}" in email


def test_query_strings_in_links_are_not_double_escaped():
    """The fix must handle the quote only: the text arrives already escaped with
    quote=False, so re-escaping wholesale would turn every `&` in a chart URL
    into `&amp;amp;`."""
    html = md_to_html("[EDGAR](https://www.sec.gov/cgi-bin/browse-edgar?CIK=1&type=8-K)")
    assert "CIK=1&amp;type=8-K" in html
    assert "&amp;amp;" not in html


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
    assert localconfig._is_placeholder(address) is True


@pytest.mark.parametrize("address", [
    "svedbg@users.noreply.github.com",
    "research@exampleclinic.co.uk",   # contains 'example' but is a real domain
    "a.b+tag@sub.domain.org",
])
def test_real_addresses_are_accepted(address):
    assert localconfig._is_placeholder(address) is False


def test_a_run_with_no_session_date_still_renders_html():
    """`sig.get("session_date", "")` returns None when the key is present and
    null, and html.escape(None) raises. notify.py catches that and falls back to
    plain text, so the HTML email vanished silently on exactly the run that had
    already gone wrong enough to lose its session date."""
    sig = {"session_date": None, "signals": [], "notify": []}
    html = build_email_html("# Report\n\nNothing to do today.", sig)
    assert "Nothing to do today" in html
    assert "None" not in html


# ---------------------------------------------------- notification text


def _sig(session_date="2026-08-15", alerts=(), exits=()):
    return {"session_date": session_date, "notify": list(alerts),
            "notify_exits": list(exits)}


ONE_SETUP = ({"symbol": "X", "tier": "SETUP", "close": 1.0,
              "previous_tier": None, "reason": "oversold"},)
ONE_EXIT = ({"symbol": "Y", "close": 2.0, "flags": ["invalidation_breached"]},)


@pytest.mark.parametrize(("alerts", "exits"), [
    ((), ()),                    # quiet day
    (ONE_SETUP, ()),
    (ONE_SETUP, ONE_EXIT),
])
def test_each_channel_stamps_the_session_date_exactly_once(alerts, exits):
    """One string used to serve both the standalone ntfy title and the email
    subject's suffix, while the subject also prefixed its own date. Every
    notification carried it twice, and a quiet day went out as
    "[biotech desk] 2026-08-15 - Biotech desk 2026-08-15"."""
    sig = _sig(alerts=alerts, exits=exits)
    summary, _body, _priority = notify.build_alert_text(sig)
    date = sig["session_date"]
    assert date not in summary, "the summary must not carry a date of its own"
    assert notify.ntfy_title(summary, date).count(date) == 1
    assert notify.email_subject(summary, date).count(date) == 1


def test_a_quiet_day_reports_what_happened_rather_than_the_product_name():
    summary, body, priority = notify.build_alert_text(_sig())
    assert summary == "No new setups"
    assert "Report written" in body
    assert priority == "default"


def test_an_exit_still_outranks_a_setup_in_both_the_summary_and_the_priority():
    summary, body, priority = notify.build_alert_text(
        _sig(alerts=ONE_SETUP, exits=ONE_EXIT))
    assert summary == "1 EXIT + 1 new setup"
    assert body.startswith("EXIT Y"), "a thesis breaking leads the message"
    assert priority == "high"


def test_a_run_with_no_session_date_leaves_no_dangling_separator():
    summary, _, _ = notify.build_alert_text(_sig(session_date=None, alerts=ONE_SETUP))
    assert notify.ntfy_title(summary, "") == "1 new setup"
    assert notify.email_subject(summary, "") == "[biotech desk] 1 new setup"


# ------------------------------------------------------- scheduler units


def _units():
    return sorted((ROOT / "systemd").glob("pharma-*.service"))


def test_no_unit_relies_on_a_bare_execstart_name():
    """systemd resolves a bare ExecStart name against its OWN compiled-in search
    path, NOT against the unit's `Environment=PATH`.

    So `ExecStart=python3 ...` silently ignores the pyenv shim the units put
    first on PATH and pins itself to the system interpreter -- and a name that
    exists only on Environment=PATH does not launch at all (verified on systemd
    255). `/usr/bin/env python3` is the form that honours it, because env itself
    is absolute and then does the lookup with the inherited PATH.
    """
    assert _units(), "no unit files found"
    for unit in _units():
        for line in unit.read_text().splitlines():
            if line.startswith("ExecStart="):
                cmd = line.split("=", 1)[1].split()[0]
                assert cmd.startswith(("/", "%h", "%")), (
                    f"{unit.name}: ExecStart '{cmd}' is a bare name, which systemd "
                    f"resolves against its own path and not Environment=PATH")


def test_the_outer_timeout_outlives_the_run_it_is_bounding():
    """TimeoutStartSec is the last resort behind run_daily.sh's own per-stage
    timeouts, and a last resort that fires first is not one.

    At 3600 against ~6400s of inner bounds, systemd's SIGTERM arrived 47 minutes
    early and after the failure handler -- so a slow run died with no report and
    no notification, looking exactly like the silence the desk emits on a quiet
    day. This recomputes the inner sum from the script, so adding a stage
    without raising the outer bound fails here rather than in production.
    """
    # Both files: the network probe and capture_if_ok moved into the sourced
    # prologue, and a bounded stage added there counts against the same outer
    # ceiling. Summing only run_daily.sh would silently stop seeing half of them.
    script = ((ROOT / "run_daily.sh").read_text()
              + (ROOT / "lib" / "run_preamble.sh").read_text())
    inner = sum(int(m) for m in re.findall(
        r"^\s*(?:run_with_timeout|capture_if_ok \S+ \S+) (\d+)", script, re.M))
    assert inner > 0, "found no stage timeouts to add up"

    desk = (ROOT / "systemd" / "pharma-desk.service").read_text()
    outer = int(re.search(r"^TimeoutStartSec=(\d+)", desk, re.M).group(1))
    assert outer > inner, (
        f"TimeoutStartSec={outer} is below the {inner}s of stage timeouts in "
        f"run_daily.sh; systemd would kill the run before it could report")


def test_the_analysis_pass_cannot_overwrite_an_existing_report():
    """Two runs on different days can name their report for the same session --
    the session is the newest bar, not the run date -- so the analysis pass has
    to displace the report it finds *before* Claude writes over it.

    Friday's run wrote reports/2026-08-14.md at 23:39; Monday's run analysed the
    same Friday bar and wrote straight over it. reports/ is gitignored, so
    Friday's analysis existed only as the attachment on Friday's email. The
    ordering is the whole fix: preserving it after the pass preserves the new
    file, not the old one.
    """
    script = (ROOT / "run_daily.sh").read_text()

    preserve = script.find('cp -p "$REPORT" "$PRIOR_REPORT"')
    analysis = script.find("claude -p")
    assert preserve != -1, "run_daily.sh no longer preserves the existing report"
    assert analysis != -1, "found no analysis invocation to order against"
    assert preserve < analysis, (
        "the existing report is preserved after the analysis pass has already "
        "overwritten it")
    assert "reports/superseded" in script, \
        "the displaced report must land in reports/superseded/, which the " \
        "archive builder skips"
    assert ".written-$stamp.md" in script, \
        "the displaced report must be stamped with when it was written"


def test_an_unchanged_report_is_a_failed_run_not_a_second_delivery():
    """`[[ -f "$REPORT" ]]` passes on the *previous* run's file, so leniently
    keeping the old report in place -- which is what makes a failed analysis pass
    non-destructive -- also lets the run email a report that was already
    delivered, as if it were new. A full inbox and no new analysis is the one
    outcome this desk cannot see, so the byte comparison turns it into a loud
    failure."""
    script = (ROOT / "run_daily.sh").read_text()
    assert '"$now_hash" == "$PRIOR_HASH"' in script
    guard = script[script.find('"$now_hash" == "$PRIOR_HASH"'):]
    assert re.search(r"fail \"the analysis pass left", guard), \
        "an unchanged report must go through fail(), which notifies"


def _systemd_schedule(unit: str) -> tuple[str, int, int]:
    """(day spec, hour, minute) out of a timer's OnCalendar line."""
    timer = (ROOT / "systemd" / unit).read_text()
    m = re.search(r"^OnCalendar=(\S+) (\d+):(\d+)", timer, re.M)
    assert m, f"{unit} has no parseable OnCalendar line"
    return m.group(1), int(m.group(2)), int(m.group(3))


def test_both_schedulers_agree_on_the_schedule():
    """The launchd job bakes its times into the plist and the systemd timer
    keeps its own. Nothing enforces that they match, and a desk that runs at two
    different times on two machines is a difference nobody would look for.

    The DAY SPEC is compared as well as the clock time, because it stopped being
    a constant. The desk runs after midnight on the session it analyses, so its
    days are Tue-Sat while the heartbeat's are still Mon-Fri; the launchd side
    used to hardcode range(1, 6) for everything, which would have skipped
    Monday's session and analysed Friday's twice on a Mac while Linux did the
    right thing. A schedule that is wrong on one platform only is the exact
    drift this test exists to catch.
    """
    installer = (ROOT / "launchd" / "install-launchd.sh").read_text()
    for unit, label in (("pharma-desk.timer", "com.pharma.desk"),
                        ("pharma-premarket.timer", "com.pharma.premarket"),
                        ("pharma-heartbeat.timer", "com.pharma.heartbeat")):
        days, h, m = _systemd_schedule(unit)
        call = re.search(rf'"{re.escape(label)}", \w+, "([\w-]+)", (\d+), (\d+),',
                         installer)
        assert call, f"launchd installer schedules no job for {label}"
        assert (call.group(1), int(call.group(2)), int(call.group(3))) == (days, h, m), (
            f"{unit} says {days} {h:02d}:{m:02d}, but the launchd job for {label} "
            f"says {call.group(1)} {int(call.group(2)):02d}:{int(call.group(3)):02d}")
        # on_days() looks the spec up in DAY_SETS, so an unmapped one is a
        # KeyError at install time -- on the Mac, during setup, far from here.
        assert f'"{days}":' in installer, (
            f"launchd's DAY_SETS has no entry for {days}, so installing would raise")


def test_the_desk_day_spec_matches_the_side_of_midnight_it_runs_on():
    """A run after midnight analyses the PREVIOUS day's session, so its day spec
    has to be shifted one day later than the sessions it covers.

    The desk fires at 01:30 to give the price provider time to publish the daily
    bar -- at the old 23:18, 18 minutes after the close, it lost that race on
    every single scheduled run and every report was named for the previous
    session. Moving the clock without moving the days would leave Monday's
    session analysed by nothing and Friday's analysed twice, which looks like a
    quiet Monday rather than a scheduling bug. Moving the days back without the
    clock is the same mistake mirrored.
    """
    days, hour, _ = _systemd_schedule("pharma-desk.timer")
    if hour < 12:
        assert days == "Tue-Sat", (
            f"the desk runs at {hour:02d}:xx, i.e. after midnight on the session it "
            f"analyses, so its days must be Tue-Sat and not {days}")
    else:
        assert days == "Mon-Fri", (
            f"the desk runs at {hour:02d}:xx, the same evening as the session it "
            f"analyses, so its days must be Mon-Fri and not {days}")


def test_the_launchd_label_array_matches_the_jobs_it_generates():
    """install-launchd.sh has two halves that must agree: a shell LABELS array
    that bootstraps, checks and uninstalls, and a Python `jobs` dict that writes
    the plists.

    A label in only one of them is a job that is written and never loaded, or
    loaded and never removed. Adding the pre-market job meant touching both, and
    the shell array is the half that is easy to miss because nothing fails when
    it is wrong -- the plist appears on disk and simply never runs.
    """
    installer = (ROOT / "launchd" / "install-launchd.sh").read_text()
    array = re.search(r"^LABELS=\(([^)]*)\)", installer, re.M)
    assert array, "no LABELS array found"
    declared = set(array.group(1).split())
    generated = set(re.findall(r'^\s*"(com\.pharma\.[a-z]+)": job\(', installer, re.M))
    assert declared == generated, (
        f"LABELS has {sorted(declared)} but jobs generates {sorted(generated)}; "
        f"the difference is a job that is never loaded or never removed")


def test_the_premarket_pass_records_its_delivery_separately():
    """Two runs, two delivery records, both checked by the heartbeat.

    One shared file let the 14:30 pre-market pass overwrite the 01:30 nightly
    run's verdict, so a nightly report that never reached anyone read as a
    healthy desk -- the exact blind spot the delivery record exists to close,
    reopened by adding a second sender to it.
    """
    notify = (ROOT / "scripts" / "notify.py").read_text()
    heartbeat = (ROOT / "scripts" / "heartbeat.py").read_text()
    assert "last_delivery_premarket.json" in notify, \
        "notify.py does not write a separate pre-market delivery record"
    assert "last_delivery_premarket.json" in heartbeat, \
        "heartbeat.py does not read the pre-market delivery record"
    assert "last_delivery.json" in heartbeat, \
        "heartbeat.py stopped reading the nightly delivery record"


def test_the_premarket_run_cannot_write_the_desks_shared_state():
    """The pre-market pass runs over a session the nightly run has already
    recorded, so every shared artefact has to be isolated or it corrupts the
    record: --no-persist keeps it out of history.sqlite, --screening plus its own
    --state keeps it out of the alert log and the archive summary, and its
    outputs land under data/premarket/.

    Without --no-persist it would consume `new_filings_since_last_run` and the
    nightly report would never mention the 8-K, having been beaten to it by an
    email that is not the desk's record of anything.
    """
    script = (ROOT / "run_premarket.sh").read_text()
    assert "--no-persist" in script, "the pre-market fetch would write to history.sqlite"
    assert "--screening" in script, "the pre-market signals pass would run as live"
    assert "state/premarket_alerts.json" in script, \
        "the pre-market pass would consume the live alert state"
    assert '--out "$PM/signals.json"' in script, \
        "the pre-market signals would overwrite data/signals.json"
    # Named for the run date under reports/premarket/, which is outside the
    # reports/*.md glob that publish.py and heartbeat.py use. A pre-market note
    # must not be able to satisfy the check that asks whether the desk still
    # produces reports.
    assert 'REPORT="$ROOT/reports/premarket/$DATE.md"' in script


def test_neither_the_archive_nor_the_heartbeat_sees_premarket_reports():
    """reports/premarket/ must stay invisible to both. publish.py pairs
    reports/<d>.md with data/summaries/<d>.json, and heartbeat.py answers "has
    the desk gone quiet" from the same glob -- a daily pre-market note dropped
    into that namespace would keep the heartbeat permanently happy while the
    nightly run was dead, which is the one failure it exists to catch.

    Both use a non-recursive glob, so a subdirectory is already excluded. This
    pins that: a change to rglob would be silent and catastrophic.
    """
    for mod in ("publish.py", "heartbeat.py"):
        src = (ROOT / "scripts" / mod).read_text()
        assert 'glob("*.md")' in src, f"{mod} no longer globs reports non-recursively"
        assert "rglob" not in src, (
            f"{mod} uses rglob, which would pull reports/premarket/ into the "
            f"archive and the staleness check")


def test_both_entry_points_share_one_prologue():
    """The lock, the interpreter probe, run_with_timeout, capture_if_ok and the
    network wait live in lib/run_preamble.sh, and both run_daily.sh and
    run_premarket.sh source it rather than carrying a copy.

    Every one of those encodes a failure that already happened here: a recycled
    pid making a lock look held forever, a bare `flock` exiting 127 on macOS and
    being read as "already running", an interpreter missing pyexpat silently
    deleting the insider layer from every report. Two copies would drift, and
    the copy that drifts is the one that is NOT the nightly run -- so the drift
    would present as the pre-market pass quietly doing nothing.
    """
    provided = ("busy", "notify_failure", "fail", "run_with_timeout",
                "capture_if_ok")
    preamble = (ROOT / "lib" / "run_preamble.sh").read_text()
    for fn in provided:
        assert re.search(rf"^{fn}\(\) \{{", preamble, re.M), \
            f"lib/run_preamble.sh no longer defines {fn}()"

    for entry in ("run_daily.sh", "run_premarket.sh"):
        script = (ROOT / entry).read_text()
        assert 'source "$ROOT/lib/run_preamble.sh"' in script, \
            f"{entry} does not source the shared prologue"
        assert re.search(r'^RUN_LABEL=', script, re.M), \
            f"{entry} sets no RUN_LABEL, so a failure notice would be unattributed"
        for fn in provided:
            assert not re.search(rf"^{fn}\(\) \{{", script, re.M), (
                f"{entry} redefines {fn}(), which is the drift the shared "
                f"prologue exists to prevent")


def test_both_entry_points_take_the_same_lock():
    """The pre-market pass reads history.sqlite and data/ while the nightly run
    writes both. They must not be able to overlap, so the lock has to be one
    lock -- and it is, because both get it from the same sourced prologue."""
    preamble = (ROOT / "lib" / "run_preamble.sh").read_text()
    assert 'LOCK="$ROOT/data/.run.lock"' in preamble
    for entry in ("run_daily.sh", "run_premarket.sh"):
        script = (ROOT / entry).read_text()
        assert "LOCK=" not in script, (
            f"{entry} defines its own lock path, so the two runs could overlap "
            f"on sqlite and data/")


def test_both_reports_are_written_in_one_shared_register():
    """The caveman register the two reports are written in is defined once, in
    lib/run_preamble.sh, and appended to both prompts from there.

    Same argument as the prologue itself: two copies would drift, and the copy
    that drifts is the one that is not the nightly run, so the drift would
    present as the pre-market note quietly reverting to prose while the report
    it is read alongside stayed compressed.

    Three of the assertions are about the directive not turning a shorter
    report into a thinner one. The skill's own boundary is that anything
    persisted outside the chat stays normal prose, so the override has to be
    explicit or the run compresses its own chatter and leaves the report --
    the only artefact this is for -- untouched. `full` rather than `ultra` is
    pinned because ultra strips conjunctions, and this document states which
    way a veto went. And the negation rule is pinned because an inverted
    "no hard veto" is the one compression failure that reads as a buy.
    """
    preamble = (ROOT / "lib" / "run_preamble.sh").read_text()
    assert "CAVEMAN_DIRECTIVE=\"$(cat <<'CAVEMAN'" in preamble, \
        "lib/run_preamble.sh no longer defines CAVEMAN_DIRECTIVE"

    directive = preamble.split("CAVEMAN_DIRECTIVE=", 1)[1]
    assert "`caveman` skill" in directive, \
        "the directive no longer names the skill it activates"
    assert "persisted outside the chat" in directive, (
        "the directive no longer overrides the skill's persisted-prose "
        "boundary, so the report itself would stay uncompressed")
    assert "`full`, not `ultra`" in directive, (
        "the directive no longer pins the intensity; ultra strips the "
        "conjunctions this document needs to say which way a veto went")
    assert "links_md" in directive, \
        "the directive no longer protects the links_md lines"
    assert '"not", "no",' in directive, \
        "the directive no longer forbids dropping a negation"

    for entry in ("run_daily.sh", "run_premarket.sh"):
        script = (ROOT / entry).read_text()
        assert "CAVEMAN_DIRECTIVE=" not in script, (
            f"{entry} defines its own register, which is the drift the shared "
            f"prologue exists to prevent")
        used = script.find("$CAVEMAN_DIRECTIVE")
        assert used != -1, f"{entry} does not write its report in the register"
        run = script.find('claude -p "$PROMPT"')
        assert run != -1, f"{entry} no longer runs the analysis pass"
        assert used < run, (
            f"{entry} interpolates the register after the analysis pass has "
            f"already been handed its prompt")
        assert re.search(r'^ALLOWED_TOOLS=".*\bSkill\b', script, re.M), (
            f"{entry} does not allow the Skill tool, so the run cannot reach "
            f"the caveman skill and would write a normal-prose report without "
            f"saying why")


def test_the_list_wide_view_is_actually_wired_into_both_runs():
    """brief.py only saves anything if the prompts and the runs point at it.

    A view nothing reads is a view that drifts: signals.json would go on growing
    with the watchlist, the prompt would go on saying "start here", and the only
    evidence would be the token bill. So both prompts name brief.py, both forbid
    reading signals.json wholesale, and both entry points tell the run how to
    invoke it -- with `--dataset` on the pre-market side, where reading the
    nightly state against this morning's delta is the standing hazard.
    """
    daily = (ROOT / "prompts" / "daily.md").read_text()
    assert "scripts/brief.py" in daily, "the daily prompt no longer starts at brief.py"
    assert "Do not read `data/signals.json` wholesale" in daily, (
        "the daily prompt no longer forbids reading signals.json whole, which is "
        "the 122,000-token read brief.py exists to replace")
    assert "Do not read `data/latest.json` directly" in daily, (
        "the older and larger of the two prohibitions went missing")

    premarket = (ROOT / "prompts" / "premarket.md").read_text()
    assert "scripts/brief.py --dataset data/premarket" in premarket, (
        "the pre-market prompt does not pass --dataset to brief.py, so it would "
        "read the nightly state against this morning's delta")

    for entry, flag in (("run_daily.sh", ""),
                        ("run_premarket.sh", " --dataset data/premarket")):
        script = (ROOT / entry).read_text()
        assert f"scripts/brief.py{flag}" in script, (
            f"{entry} does not tell the run how to invoke the list-wide view")
