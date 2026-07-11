"""snapshot_db must pick the right strategy per store, never hang, and never
miss committed rows. Both failure modes actually happened (July 2026): the
bare main-file copy of atuin's WAL store read as 'database disk image is
malformed', and the first backup-API fix wedged two indexers in Python's
infinite SQLITE_BUSY retry against Helium's exclusively-locked History."""
import os, sqlite3
import pytest
from sources.common import snapshot_db

def wal_db(tmp_path, exclusive=False):
    """A live WAL database ('dir with space' pins URI escaping) whose writer
    stays open: two committed rows, both only in the -wal sidecar."""
    d = tmp_path / "dir with space"
    d.mkdir()
    path = str(d / "live.db")
    src = sqlite3.connect(path, isolation_level=None)
    if exclusive:
        src.execute("PRAGMA locking_mode=EXCLUSIVE")
    src.execute("PRAGMA journal_mode=WAL")
    src.execute("PRAGMA wal_autocheckpoint=0")
    src.execute("CREATE TABLE t(x)")
    src.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    assert os.path.getsize(path + "-wal") > 0     # rows live in the WAL
    return path, src

def test_pending_wal_uses_backup_and_sees_all_rows(tmp_path):
    path, src = wal_db(tmp_path)
    db, tmp = snapshot_db(path)
    try:
        assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close(); os.unlink(tmp); src.close()

def test_no_wal_takes_plain_copy(tmp_path):
    path = str(tmp_path / "plain.db")
    src = sqlite3.connect(path)
    src.execute("CREATE TABLE t(x)")
    src.execute("INSERT INTO t VALUES (1)")
    src.commit(); src.close()                     # journal mode: no -wal
    db, tmp = snapshot_db(path)
    try:
        assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        db.close(); os.unlink(tmp)

def test_exclusively_locked_store_falls_back_to_copy_not_hang(tmp_path):
    # the Chromium shape: reads are locked out entirely; a snapshot may be
    # stale but must come back within the bounded probe, never block
    path, src = wal_db(tmp_path, exclusive=True)
    db, tmp = snapshot_db(path, lock_timeout=0.2)
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close(); os.unlink(tmp); src.close()

def test_missing_file_raises_and_leaves_no_temp(tmp_path):
    import glob, tempfile
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.db")))
    with pytest.raises((OSError, sqlite3.Error)):
        snapshot_db(str(tmp_path / "nope.db"))
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "*.db"))) == before
