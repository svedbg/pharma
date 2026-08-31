"""The real catalyst files themselves, not a fixture.

Every other catalyst test builds its own TOML in tmp_path, so the file the desk
actually reads has never been checked by anything. It is the one file an
unattended LLM appends to nightly, and its failure mode is silent: a malformed
append makes `load_catalysts` print one WARNING to a log nobody reads and
return {}, so *every* name loses its catalyst clock at once and the report says
nothing is scheduled -- indistinguishable from a quiet week, on the layer
CLAUDE.md calls one of the three the desk is actually for.

A bad date is narrower but the same shape: that entry alone is dropped, and the
binary it described stops warning.

So this asserts what the file's own header promises: every entry parses, is
sourced, and uses the documented vocabulary.

**Two files, because catalysts.toml is gitignored.** It names the companies the
desk follows and what it thinks their binaries are worth, and the nightly run
appends to it on its own schedule -- so tracking it published names unattended.
That leaves the checks below in an odd position: the file they exist to protect
is the one CI cannot see.

Both are therefore checked, with deliberately different absence rules:

- `catalysts.example.toml` is committed, so it is always required. It is what a
  new clone copies, and an example that does not satisfy these rules teaches
  the format wrongly and produces an unrunnable catalysts.toml on the first
  `cp`.
- `catalysts.toml` is checked whenever it exists and skipped when it does not.
  On the desk's own machine -- the only place a nightly append can land, and so
  the only place this can catch one -- it exists, and `make check` runs these
  against it. In CI it does not, and skipping is the honest answer rather than
  a green tick implying the live file was read.

The skip is the cost of the split and worth naming: a catalysts.toml that goes
missing outright now reads as a skip here rather than a failure. `load_catalysts`
treats an absent file as an empty calendar by design, so the loud check that
used to sit here is genuinely gone. What replaces it is that the file is no
longer shared, and so no longer arrives missing from someone else's checkout.

Each test collects every offending entry rather than stopping at the first.
A machine appends here nightly and can introduce several problems in one pass;
failing on one at a time turns fixing them into a re-run per typo.
"""

from __future__ import annotations

import tomllib
from collections import Counter

import pytest

from signals import ROOT, _catalyst_date

# From the header of catalysts.toml. Listed rather than derived from the file,
# so a typo introduced by an append fails here instead of quietly widening the
# vocabulary to include itself.
KINDS = {"PDUFA", "AdCom", "readout", "conference", "other"}
CONFIDENCES = {"confirmed", "expected", "rumored"}

CATALYSTS = ROOT / "catalysts.toml"
EXAMPLE = ROOT / "catalysts.example.toml"


@pytest.fixture(scope="module", params=[EXAMPLE, CATALYSTS],
                ids=["example", "live"])
def entries(request) -> list[dict]:
    """Each real file in turn, parsed. Fails loudly rather than degrading to {}.

    The example is committed and never skipped. The live file is gitignored, so
    it is absent in CI and present on the desk; skipping there beats asserting
    on a file this checkout was never meant to have.
    """
    path = request.param
    if not path.exists():
        if path is EXAMPLE:
            raise AssertionError(
                f"{path.name} is committed and must exist: it is what `cp "
                f"{path.name} catalysts.toml` copies, and it is the only "
                f"catalyst file CI can see.")
        pytest.skip(
            f"{path.name} is gitignored and absent from this checkout -- "
            f"expected in CI, where only {EXAMPLE.name} is visible. On the "
            f"desk itself this file exists and is checked.")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        # Raised rather than pytest.fail'd: both stop the fixture with the same
        # message, but only a raise is terminal to a reader and to static
        # analysis. pytest.fail is `-> NoReturn`, so `raw` below can never
        # really be unbound; CodeQL does not model that and read it as a use
        # before assignment.
        raise AssertionError(
            f"{path.name} does not parse: {e}. Every name would lose its "
            f"catalyst clock at once and the report would read as 'nothing "
            f"scheduled'.") from e
    found = raw.get("catalyst", [])
    assert found, "no [[catalyst]] entries -- the calendar would be empty"
    # `[catalyst]` instead of `[[catalyst]]` parses fine and yields a truthy
    # dict; iterating it then hands each test a key string and an AttributeError
    # that points nowhere near the actual mistake.
    assert isinstance(found, list), (
        "catalyst must be a table ARRAY -- write [[catalyst]], not [catalyst]")
    return found


def _report(bad: list[str], headline: str) -> None:
    """One assertion carrying every offending entry, not just the first."""
    assert not bad, f"{headline}\n  " + "\n  ".join(bad)


def _who(i: int, c: dict) -> str:
    """Symbol alone does not locate an entry -- most appear several times."""
    return f"#{i} {c.get('symbol') or '<no symbol>'} {c.get('date') or ''}".strip()


def test_every_catalyst_carries_the_fields_a_report_cites(entries):
    """An unsourced date is forbidden: it silently gates sizing forever after.

    Checked with strip(), so a whitespace-only source cannot satisfy the file
    header's "ONLY add dates you can source" by being merely non-empty.
    """
    bad = [f"{_who(i, c)}: missing {field}"
           for i, c in enumerate(entries)
           for field in ("symbol", "date", "description", "source")
           if not str(c.get(field, "")).strip()]
    _report(bad, "entries missing a field the report cites:")


def test_every_date_is_readable(entries):
    """_catalyst_date returning None means the entry is dropped with a warning
    and the binary it describes stops appearing in any report."""
    bad = [f"{_who(i, c)}: unreadable date {c.get('date')!r}"
           for i, c in enumerate(entries)
           if _catalyst_date(c.get("date")) is None]
    _report(bad, 'unreadable dates (use quoted ISO, e.g. date = "2026-09-15"):')


def test_kind_and_confidence_stay_in_the_documented_vocabulary(entries):
    """A typo here does not fail anything at runtime; it just quietly stops
    matching what the report and the prompt expect the value to be."""
    bad = []
    for i, c in enumerate(entries):
        if c.get("kind") not in KINDS:
            bad.append(f"{_who(i, c)}: kind={c.get('kind')!r}")
        if c.get("confidence") not in CONFIDENCES:
            bad.append(f"{_who(i, c)}: confidence={c.get('confidence')!r}")
    _report(bad, f"outside the documented vocabulary {sorted(KINDS)} / "
                 f"{sorted(CONFIDENCES)}:")


def test_no_entry_is_appended_twice(entries):
    """A re-appended entry is the likeliest corruption of a file a machine
    writes to unattended, and it is not merely untidy: `analyse` truncates to
    the first three catalysts per name, so a duplicate silently evicts a real
    binary from the report. The same shape took the 2026-08-18 run down.

    Keyed on the description too, because one name legitimately carries several
    catalysts on one date: PCRX has both the ZILRETTA shoulder-OA and the
    PCRX-201 readouts on the 2026-10-01 quarter placeholder, and NMRA two of
    its own. Only a byte-identical repeat is an error.
    """
    seen = Counter((c.get("symbol"), c.get("date"), c.get("kind"),
                    c.get("description")) for c in entries)
    bad = [f"{sym} {date} {kind}: appended {n} times"
           for (sym, date, kind, _), n in seen.items() if n > 1]
    _report(bad, "duplicate entries -- each one evicts a real catalyst:")
