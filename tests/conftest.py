"""Run every test as if the machine had never been configured.

A test that reads the developer's own `~/.config/pharma/pharma.env` passes here
and fails in CI, which is the worst way round: the failure appears after the
push, on a branch that looked green, and it says nothing about which test
actually depends on the ambient value.

That happened. The CIK-map fallback tests monkeypatched `fetch.get_json` and
called `load_cik_map()`, but `sec_ua()` is evaluated as an *argument* to that
call, so it runs first -- and on a machine with no SEC_CONTACT_EMAIL it raises
SystemExit before the cache logic under test is ever reached. Locally there was
a contact address, so six tests passed for a reason that had nothing to do with
what they asserted.

So the default is now "unconfigured", for the whole suite:

  * SEC_CONTACT_EMAIL and the notify/email variables are cleared from the
    environment,
  * the config files are pointed at a path that does not exist,
  * fetch's memoised User-Agent is reset, since it is module-level state that
    would otherwise leak a real address between tests.

A test that needs configuration sets it up explicitly, which is what
`test_sec_contact_accepts_a_real_address` and the delivery tests already do.
This costs nothing and makes `pytest` locally mean the same thing as `pytest`
in CI.
"""

from __future__ import annotations

import os

import pytest

import fetch
import localconfig

# Every prefix localconfig.load() copies out of the environment. Listed rather
# than inferred: this must fail loudly if that list grows, not silently start
# leaking a new variable into the suite.
_CONFIG_PREFIXES = ("NTFY_", "SMTP_", "EMAIL_", "SEC_")


@pytest.fixture(autouse=True)
def _unconfigured_machine(monkeypatch, tmp_path_factory):
    for name in tuple(os.environ):
        if name.startswith(_CONFIG_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    absent = tmp_path_factory.mktemp("noconfig") / "absent.env"
    monkeypatch.setattr(localconfig, "CONFIG_FILES", (absent,))
    monkeypatch.setattr(fetch, "_SEC_UA", None)
