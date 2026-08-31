"""`scripts/backup.py` -- the only copy of the only copy.

Since the catalyst calendar was untracked, four things on this desk exist in
exactly one place: `watchlist.toml`, `catalysts.toml`, `STRATEGY.local.md` and
`data/history.sqlite`. The code is on GitHub and reclones in a second; none of
these do. So the failure mode that matters here is not "the backup did not run"
-- that is loud -- but "the backup ran, reported success, and wrote an archive
that does not restore". Each test below pins one way that can happen.

The database case is the one worth reading. `data/history.sqlite` is written by
the nightly run, and a backup is exactly the sort of thing someone schedules to
fire near it. Copying a live sqlite file can capture a torn write, producing an
archive that looks fine -- right size, right name, exits 0 -- and is discovered
to be corrupt only on the day it is needed.
"""

from __future__ import annotations

import sqlite3
import tarfile
import tomllib

import backup


def _desk(root, *, catalysts='[[catalyst]]\nsymbol = "AAA"\ndate = "2026-09-01"\n'):
    """A miniature checkout carrying one of everything backup.py claims to take."""
    (root / "data" / "summaries").mkdir(parents=True)
    (root / "state").mkdir()
    (root / "reports").mkdir()
    (root / "watchlist.toml").write_text('[[ticker]]\nsymbol = "AAA"\n')
    (root / "catalysts.toml").write_text(catalysts)
    (root / "STRATEGY.local.md").write_text("# strategy\n")
    (root / "state" / "alerts.json").write_text("{}")
    (root / "data" / "summaries" / "2026-08-28.json").write_text("{}")
    (root / "reports" / "2026-08-28.md").write_text("# report\n")
    # Derived files, which must NOT be carried: the next run rebuilds them and
    # together they dwarf everything above.
    (root / "data" / "latest.json").write_text("x" * 5000)
    (root / "data" / "signals.json").write_text("x" * 5000)
    db = root / "data" / "history.sqlite"
    c = sqlite3.connect(db)
    c.execute("create table bars (d text, close real)")
    c.executemany("insert into bars values (?,?)", [(f"2026-08-{i:02d}", i) for i in range(1, 29)])
    c.commit()
    c.close()
    return root


def _members(archive):
    with tarfile.open(archive) as tar:
        return {m.name.split("/", 1)[1] for m in tar.getmembers() if "/" in m.name}


def _only_archive(dest):
    found = list(dest.glob("pharma-backup-*.tar.gz"))
    assert len(found) == 1, f"expected one archive, found {found}"
    return found[0]


def test_the_irreplaceable_files_are_carried_and_the_derived_ones_are_not(tmp_path):
    """The set is the whole point: miss one and it is gone, carry the rebuilt
    snapshots and the archive is 20x bigger for nothing, which is how a backup
    becomes slow enough that it stops being run."""
    root, dest = _desk(tmp_path / "desk"), tmp_path / "out"
    assert backup.main(["--root", str(root), "--dest", str(dest)]) == 0
    names = _members(_only_archive(dest))

    for must in ("repo/watchlist.toml", "repo/catalysts.toml",
                 "repo/STRATEGY.local.md", "repo/data/history.sqlite",
                 "repo/state/alerts.json", "repo/reports/2026-08-28.md",
                 "repo/data/summaries/2026-08-28.json"):
        assert must in names, f"{must} is irreplaceable and was not archived"

    for must_not in ("repo/data/latest.json", "repo/data/signals.json"):
        assert must_not not in names, f"{must_not} is rebuilt by the next run"


def test_the_database_is_snapshotted_consistently_while_being_written(tmp_path):
    """The reason backup.py uses sqlite's backup API instead of copying a file.

    An open write transaction on the source is the ordinary state of affairs
    when a backup fires near the nightly run. The archived database must be
    internally consistent, and must show the committed history rather than
    another connection's uncommitted rows.
    """
    root, dest = _desk(tmp_path / "desk"), tmp_path / "out"

    writer = sqlite3.connect(root / "data" / "history.sqlite")
    writer.execute("begin")
    writer.execute("insert into bars values ('2026-08-29', 999)")  # deliberately uncommitted
    try:
        assert backup.main(["--root", str(root), "--dest", str(dest)]) == 0
    finally:
        writer.rollback()
        writer.close()

    with tarfile.open(_only_archive(dest)) as tar:
        tar.extractall(tmp_path / "restored", filter="data")
    restored = next((tmp_path / "restored").glob("*/repo/data/history.sqlite"))

    c = sqlite3.connect(restored)
    assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert c.execute("select count(*) from bars").fetchone()[0] == 28
    assert c.execute("select count(*) from bars where close = 999").fetchone()[0] == 0
    c.close()


def test_the_credential_is_stubbed_unless_it_is_asked_for(tmp_path, monkeypatch):
    """Backups get synced to Dropbox, NAS boxes and USB sticks. A Gmail app
    password is a credential to an account, not desk data, so it travels only
    when someone says so."""
    root, dest = _desk(tmp_path / "desk"), tmp_path / "out"
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "pharma.env").write_text("SMTP_USER=a@b.c\nSMTP_PASS=hunter2hunter2\n")
    monkeypatch.setattr(backup, "CONFIG", cfg / "pharma.env")

    assert backup.main(["--root", str(root), "--dest", str(dest)]) == 0
    with tarfile.open(_only_archive(dest)) as tar:
        body = tar.extractfile(
            next(m for m in tar.getmembers() if m.name.endswith("config/pharma.env"))
        ).read().decode()
    assert "hunter2hunter2" not in body, "the live password reached the archive"
    assert "SMTP_USER=a@b.c" in body, "everything else must survive, or a restore is manual"

    _only_archive(dest).unlink()
    assert backup.main(["--root", str(root), "--dest", str(dest), "--with-secrets"]) == 0
    with tarfile.open(_only_archive(dest)) as tar:
        body = tar.extractfile(
            next(m for m in tar.getmembers() if m.name.endswith("config/pharma.env"))
        ).read().decode()
    assert "hunter2hunter2" in body, "--with-secrets must actually include them"


def test_a_half_written_catalyst_file_is_archived_but_says_so(tmp_path, capsys):
    """The nightly run appends to catalysts.toml unattended, so a backup can
    catch it mid-write. Refusing to archive it would let the last good copy age
    out under --keep; archiving it silently would hide the corruption until a
    restore. So: archived, with a warning naming the file."""
    root = _desk(tmp_path / "desk", catalysts='[[catalyst]]\nsymbol = "AAA\n')
    dest = tmp_path / "out"
    assert backup.main(["--root", str(root), "--dest", str(dest)]) == 0

    assert "catalysts.toml" in capsys.readouterr().err
    assert "repo/catalysts.toml" in _members(_only_archive(dest))


def test_valid_toml_draws_no_warning(tmp_path, capsys):
    """The counterpart: the warning has to mean something, so the ordinary case
    must be silent or it becomes noise nobody reads."""
    root, dest = _desk(tmp_path / "desk"), tmp_path / "out"
    assert backup.main(["--root", str(root), "--dest", str(dest)]) == 0
    assert "does not parse" not in capsys.readouterr().err
    # and the archived copy is the same file, not a re-serialisation of it
    with tarfile.open(_only_archive(dest)) as tar:
        body = tar.extractfile(
            next(m for m in tar.getmembers() if m.name.endswith("repo/catalysts.toml"))
        ).read().decode()
    assert tomllib.loads(body)["catalyst"][0]["symbol"] == "AAA"


def test_pruning_keeps_the_newest_and_never_empties_the_directory(tmp_path):
    """--keep bounds the disk cost. Off-by-one here deletes the only backup."""
    dest = tmp_path / "out"
    dest.mkdir()
    for i in range(5):
        p = dest / f"pharma-backup-2026080{i}T000000Z.tar.gz"
        p.write_bytes(b"old")
    kept_newest = dest / "pharma-backup-20260804T000000Z.tar.gz"

    backup.prune(dest, keep=2)
    left = sorted(p.name for p in dest.glob("pharma-backup-*.tar.gz"))
    assert len(left) == 2
    assert kept_newest.name in left, "pruning must keep the newest, not the oldest"

    backup.prune(dest, keep=0)
    assert len(list(dest.glob("pharma-backup-*.tar.gz"))) == 2, "0 means keep all"
