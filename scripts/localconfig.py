#!/usr/bin/env python3
"""Local settings that must never enter the repository.

One file, `~/.config/pharma/pharma.env`, holds everything machine-specific:
the SEC contact address, ntfy topic and SMTP credentials. It lives outside the
project directory so it cannot be committed by accident, and so the repository
can be made public without an audit.

Resolution order for every key: real environment variable first (useful for
one-off overrides and CI), then the config file. Nothing is hardcoded.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR = Path(os.path.expanduser("~/.config/pharma"))
# Older installs used notify.env; both are read so nothing breaks on upgrade.
CONFIG_FILES = (CONFIG_DIR / "pharma.env", CONFIG_DIR / "notify.env")


def _clean_value(raw: str) -> str:
    """Strip a trailing inline comment and surrounding quotes.

    `KEY=1  # explanation` must yield "1", not the whole string -- otherwise a
    commented line silently becomes part of a value. A '#' only starts a comment
    when preceded by whitespace, so secrets containing '#' survive intact.
    """
    v = raw.strip()
    if v[:1] in ('"', "'"):
        quote = v[0]
        end = v.find(quote, 1)
        return v[1:end] if end > 0 else v[1:]
    for sep in (" #", "\t#"):
        if sep in v:
            v = v.split(sep, 1)[0]
    return v.strip()


def load() -> dict:
    """Merged settings from the config file(s), overridden by the environment."""
    cfg: dict = {}
    for path in reversed(CONFIG_FILES):  # pharma.env wins over notify.env
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = _clean_value(v)
    cfg.update({k: v for k, v in os.environ.items()
                if k.startswith(("NTFY_", "SMTP_", "EMAIL_", "SEC_"))})
    return cfg


def sec_contact() -> str:
    """Contact address for the SEC User-Agent header.

    SEC's fair-access policy requires a real contact address and throttles or
    blocks generic agents, so there is no safe default to fall back on. Failing
    loudly with instructions beats being silently rate-limited at 3am.
    """
    value = load().get("SEC_CONTACT_EMAIL", "").strip()
    if not value or "example.com" in value:
        raise SystemExit(
            "SEC_CONTACT_EMAIL is not set.\n\n"
            "SEC requires a real contact address in the User-Agent header and\n"
            "throttles requests without one. Set it once:\n\n"
            f"    mkdir -p {CONFIG_DIR}\n"
            f"    echo 'SEC_CONTACT_EMAIL=you@example.org' >> {CONFIG_DIR / 'pharma.env'}\n"
            f"    chmod 600 {CONFIG_DIR / 'pharma.env'}\n\n"
            "or export SEC_CONTACT_EMAIL for a one-off run."
        )
    return value
