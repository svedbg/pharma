"""The committed catalysts.toml itself, not a fixture.

Every other catalyst test builds its own TOML in tmp_path, so the file the desk
actually reads has never been checked by anything. It is the one committed file
an unattended LLM appends to nightly, and its failure mode is silent: a
malformed append makes `load_catalysts` print one WARNING to a log nobody reads
and return {}, so *every* name loses its catalyst clock at once and the report
says nothing is scheduled -- indistinguishable from a quiet week, on the layer
CLAUDE.md calls one of the three the desk is actually for.

A bad date is narrower but the same shape: that entry alone is dropped, and the
binary it described stops warning.

So this asserts what the file's own header promises: every entry parses, is
sourced, and uses the documented vocabulary.
"""

from __future__ import annotations

import tomllib

import pytest

from signals import ROOT, _catalyst_date

# From the header of catalysts.toml. Listed rather than derived from the file,
# so a typo introduced by an append fails here instead of quietly widening the
# vocabulary to include itself.
KINDS = {"PDUFA", "AdCom", "readout", "conference", "other"}
CONFIDENCES = {"confirmed", "expected", "rumored"}

CATALYSTS = ROOT / "catalysts.toml"


@pytest.fixture(scope="module")
def entries() -> list[dict]:
    """The real file, parsed. Fails loudly rather than degrading to {}."""
    assert CATALYSTS.exists(), (
        f"{CATALYSTS.name} is missing. load_catalysts returns {{}} for an absent "
        f"file without even the WARNING a malformed one gets, so this is the "
        f"quietest way of all for the calendar to die.")
    try:
        raw = tomllib.loads(CATALYSTS.read_text())
    except tomllib.TOMLDecodeError as e:
        # Raised rather than pytest.fail'd: both stop the fixture with the same
        # message, but only a raise is terminal to a reader and to static
        # analysis. pytest.fail is `-> NoReturn`, so `raw` below can never
        # really be unbound; CodeQL does not model that and read it as a use
        # before assignment.
        raise AssertionError(
            f"{CATALYSTS.name} does not parse: {e}. Every name would lose its "
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


def test_every_catalyst_carries_the_fields_a_report_cites(entries):
    """An unsourced date is forbidden: it silently gates sizing forever after."""
    for c in entries:
        who = c.get("symbol") or repr(c)[:60]
        for field in ("symbol", "date", "description", "source"):
            assert c.get(field), f"{who}: missing {field}"


def test_every_date_is_readable(entries):
    """_catalyst_date returning None means the entry is dropped with a warning
    and the binary it describes stops appearing in any report."""
    for c in entries:
        assert _catalyst_date(c.get("date")) is not None, (
            f"{c.get('symbol')}: unreadable date {c.get('date')!r} -- "
            f'dates must be quoted ISO strings, e.g. date = "2026-09-15"'
        )


def test_kind_and_confidence_stay_in_the_documented_vocabulary(entries):
    """A typo here does not fail anything at runtime; it just quietly stops
    matching what the report and the prompt expect the value to be."""
    for c in entries:
        who = c.get("symbol")
        assert c.get("kind") in KINDS, f"{who}: kind={c.get('kind')!r}"
        assert c.get("confidence") in CONFIDENCES, (
            f"{who}: confidence={c.get('confidence')!r}")
