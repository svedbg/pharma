#!/usr/bin/env python3
"""Snapshot the part of the desk that exists nowhere else.

`watchlist.toml`, `STRATEGY.local.md` and -- since the calendar was untracked --
`catalysts.toml` are gitignored, and `data/history.sqlite` never was in git at
all. Between them that is the trading plan, the strategy and every bar, filing
and alert the desk has accumulated. The code is on GitHub and can be recloned in
a second; none of this can be recovered from anywhere.

`catalysts.toml` is the sharpest case. The nightly run appends to it unattended,
so it grows on its own and its only copy is the working file being appended to.

Two things this does that `cp -r` does not, both of which produce a
silently-corrupt archive rather than a loud failure:

- **The database is copied through sqlite's online-backup API**, never as a
  file. `data/history.sqlite` is written by the nightly run, and a plain copy of
  a live database can capture a torn write or a partial WAL -- an archive that
  restores to a corrupt file, discovered only when it is needed. `Connection.
  backup()` takes a consistent snapshot of a database being written to.

- **Every TOML file is parsed before it is archived.** The run appends to
  `catalysts.toml` while it is running, so a backup fired mid-append can catch
  half a record. Backing up a broken file over a good one is how a backup
  becomes the thing that loses the data; a file that does not parse is archived
  anyway, under a loud warning, since a half-written file still beats none.

Secrets are stubbed by default. `~/.config/pharma/pharma.env` holds a Gmail app
password, and backups get synced to places credentials should not go. Pass
`--with-secrets` if the destination is one you would put a password in.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = Path.home() / "pharma-backups"
DEFAULT_KEEP = 14

# The env file lives outside the repo on purpose, so it is found, not walked to.
CONFIG = Path(
    os.environ.get("PHARMA_CONFIG_DIR", Path.home() / ".config" / "pharma")
) / "pharma.env"

# Anything matching this in the env file is replaced unless --with-secrets.
SECRET_KEYS = re.compile(r"^(SMTP_PASS)=.*$", re.MULTILINE)
STUB = r"\1=PASTE_YOUR_GMAIL_APP_PASSWORD_HERE"

# Irreplaceable: gitignored, or never in git, and not derived from anything.
# Ordered as a human would want to see them listed, not as they sit on disk.
IRREPLACEABLE = [
    "watchlist.toml",
    "catalysts.toml",
    "STRATEGY.local.md",
    "candidates.toml",
    "state/alerts.json",
    "data/summaries",
    "data/scorecard.txt",
    "data/paper_status.txt",
    "data/last_delivery.json",
    "data/last_delivery_premarket.json",
    "reports",
]

# Copied through the sqlite API rather than as a file. See the module docstring.
DATABASE = "data/history.sqlite"

# Deliberately absent: data/latest.json, data/signals.json, data/premarket/,
# data/candidates*.json, data/cik_map.json, site/, logs/. Every one of them is
# rebuilt by the next run, and together they are 20x the size of everything
# above. A backup nobody runs because it is slow protects nothing.


def _parses(path: Path) -> tuple[bool, str]:
    """TOML files only. Anything else is opaque here and archived as-is."""
    if path.suffix != ".toml":
        return True, ""
    try:
        tomllib.loads(path.read_text())
        return True, ""
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        return False, str(e)


def snapshot_database(src: Path, dst: Path) -> int:
    """A consistent copy of a database that may be being written to right now."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dst.stat().st_size


def stage(root: Path, staging: Path, *, with_secrets: bool) -> tuple[list[str], list[str]]:
    """Lay the archive out under `staging`. Returns (included, warnings)."""
    included: list[str] = []
    warnings: list[str] = []

    for rel in IRREPLACEABLE:
        src = root / rel
        if not src.exists():
            continue
        dst = staging / "repo" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
            included.append(f"{rel}/ ({sum(1 for _ in src.rglob('*') if _.is_file())} files)")
            continue
        ok, why = _parses(src)
        if not ok:
            warnings.append(
                f"{rel} does not parse ({why}). Archived anyway -- a "
                f"half-written file beats none -- but fix it before the next run.")
        shutil.copy2(src, dst)
        included.append(rel)

    db = root / DATABASE
    if db.exists():
        size = snapshot_database(db, staging / "repo" / DATABASE)
        included.append(f"{DATABASE} ({size // 1024} KB, via sqlite backup API)")
    else:
        warnings.append(f"{DATABASE} is missing -- the whole history is absent")

    if CONFIG.exists():
        text = CONFIG.read_text()
        if not with_secrets:
            text, n = SECRET_KEYS.subn(STUB, text)
            note = f" ({n} secret stubbed)" if n else ""
        else:
            note = " (SECRETS INCLUDED)"
        out = staging / "config" / CONFIG.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        out.chmod(0o600)
        included.append(f"config/{CONFIG.name}{note}")
    else:
        warnings.append(f"no config at {CONFIG} -- delivery settings not backed up")

    return included, warnings


def prune(dest: Path, keep: int) -> list[Path]:
    """Oldest first, so the newest `keep` survive."""
    if keep <= 0:
        return []
    existing = sorted(dest.glob("pharma-backup-*.tar.gz"),
                      key=lambda p: p.stat().st_mtime)
    doomed = existing[:-keep] if len(existing) > keep else []
    for p in doomed:
        p.unlink()
    return doomed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                    help=f"where archives are written (default {DEFAULT_DEST})")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help=f"how many to retain (default {DEFAULT_KEEP}; 0 keeps all)")
    ap.add_argument("--with-secrets", action="store_true",
                    help="include SMTP_PASS instead of stubbing it")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    args.dest.mkdir(parents=True, exist_ok=True)
    archive = args.dest / f"pharma-backup-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / f"pharma-backup-{stamp}"
        staging.mkdir()
        included, warnings = stage(args.root, staging, with_secrets=args.with_secrets)
        if not included:
            print("[backup] nothing to back up -- is --root a desk checkout?",
                  file=sys.stderr)
            return 1
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname=staging.name)

    archive.chmod(0o600)
    for w in warnings:
        print(f"[backup] WARNING: {w}", file=sys.stderr)
    print(f"[backup] wrote {archive} ({archive.stat().st_size // 1024} KB)")
    for item in included:
        print(f"           {item}")
    for gone in prune(args.dest, args.keep):
        print(f"[backup] pruned {gone.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
