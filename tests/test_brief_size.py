"""brief.py has to notice when it becomes the problem it was written to solve.

`brief.py` exists because reading `signals.json` whole had grown to 487KB --
about 122,000 tokens, fifteen times the report it produced -- and the prompt told
every run to start there. CLAUDE.md states the sequel plainly:

    It will need moving again: the file grows with the watchlist, and nothing in
    the pipeline notices.

These pin the noticing. The measurement is deliberately cheap and coarse: it
runs on every invocation, it goes to stderr so it lands in the run log without
being read as part of the brief, and it never changes the output.
"""

from __future__ import annotations

import io
import sys

import brief


def test_a_normal_sized_brief_says_nothing():
    """A warning that fires on an ordinary busy session gets silenced, and then
    it is not a warning any more."""
    assert brief.oversize_warning(0) is None
    assert brief.oversize_warning(20_000 * brief.CHARS_PER_TOKEN) is None


def test_the_budget_boundary_is_inclusive():
    """At exactly the budget there is nothing to complain about yet."""
    at = brief.BRIEF_TOKEN_BUDGET * brief.CHARS_PER_TOKEN
    assert brief.oversize_warning(at) is None
    assert brief.oversize_warning(at + brief.CHARS_PER_TOKEN * 2) is not None


def test_going_over_says_what_to_do_about_it():
    """A size warning with no remedy is just a number nobody acts on. The two
    remedies are the ones CLAUDE.md names: move material to detail.py, or raise
    the triage bar so fewer names get a full block."""
    msg = brief.oversize_warning(90_000 * brief.CHARS_PER_TOKEN)
    assert msg is not None
    assert "detail.py" in msg
    assert "prompts/daily.md" in msg
    assert "90,000" in msg, "the actual figure has to be in the message"


def test_the_budget_stays_well_under_the_read_it_replaced():
    """If these ever converged the guard would permit the exact regression it
    watches for: a brief as expensive as reading signals.json whole, passing
    silently because the bar had been raised to meet it."""
    assert brief.BRIEF_TOKEN_BUDGET < brief.SUPERSEDED_READ_TOKENS / 2, (
        f"a {brief.BRIEF_TOKEN_BUDGET:,}-token budget is not meaningfully "
        f"below the {brief.SUPERSEDED_READ_TOKENS:,}-token read brief.py "
        f"replaced")


def test_the_counting_stream_forwards_everything_it_measures():
    """It sits in front of stdout on every run. Miscounting is a bad warning;
    dropping or mangling a byte would corrupt the only list-wide view the
    analysis pass gets."""
    sink = io.StringIO()
    counted = brief._CountingStream(sink)
    counted.write("hello ")
    counted.write("world")
    assert sink.getvalue() == "hello world", "output must pass through unchanged"
    assert counted.chars == 11
    counted.flush()          # must not raise -- print() calls it


def test_the_entry_point_measures_real_output(monkeypatch, capsys):
    """The threshold being right is worth nothing if nothing measures.

    `run()` is what `__main__` calls, so this drives the whole path: a stubbed
    main() that prints a known amount, and a budget low enough that it trips.
    """
    monkeypatch.setattr(brief, "main", lambda: (sys.stdout.write("x" * 4_000), 0)[1])
    monkeypatch.setattr(brief, "BRIEF_TOKEN_BUDGET", 100)

    assert brief.run() == 0
    out, err = capsys.readouterr()
    assert out == "x" * 4_000, "the brief itself must be unchanged"
    assert "WARNING" in err and "1,000 tokens" in err
    assert sys.stdout is not None and not isinstance(sys.stdout, brief._CountingStream), \
        "run() must put the real stdout back, even though it wrapped it"


def test_a_brief_under_budget_leaves_stderr_clean(monkeypatch, capsys):
    """The other direction, which is the state the desk is normally in."""
    monkeypatch.setattr(brief, "main", lambda: (sys.stdout.write("short"), 0)[1])
    assert brief.run() == 0
    assert capsys.readouterr().err == ""
