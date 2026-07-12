"""Backup tool: once-per-day semantics, faithful copies, retention pruning."""
import os, sqlite3

from tests.helpers import load_script

def _load():
    return load_script("tools/backup.py", "backup_tool")

def _mk_db(path, rows):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t(x)")
    db.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    db.commit()
    db.close()

def test_backup_writes_once_per_day(tmp_path):
    b = _load()
    src = tmp_path / "history-rag.db"
    _mk_db(str(src), ["a", "b", "c"])
    out = tmp_path / "backups"
    out.mkdir()
    written = b.backup_one(str(src), str(out), "2026-07-03")
    assert written and written.endswith("history-rag-2026-07-03.db")
    copy = sqlite3.connect(written)
    assert copy.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
    # same day again -> skip
    assert b.backup_one(str(src), str(out), "2026-07-03") is None
    # missing source -> skip, no crash
    assert b.backup_one(str(tmp_path / "gone.db"), str(out), "2026-07-03") is None
    # no stray temp files
    assert not list(out.glob("*.tmp"))

def test_prune_keeps_newest(tmp_path):
    b = _load()
    for d in ("2026-06-28", "2026-06-29", "2026-06-30", "2026-07-01"):
        (tmp_path / f"history-rag-{d}.db").write_bytes(b"x")
    (tmp_path / "appusage-2026-06-01.db").write_bytes(b"x")   # other stem: untouched
    removed = b.prune(str(tmp_path), "history-rag", 2)
    assert [os.path.basename(r) for r in removed] == \
        ["history-rag-2026-06-28.db", "history-rag-2026-06-29.db"]
    left = sorted(p.name for p in tmp_path.glob("history-rag-*.db"))
    assert left == ["history-rag-2026-06-30.db", "history-rag-2026-07-01.db"]
    assert (tmp_path / "appusage-2026-06-01.db").exists()

def test_main_returns_written_or_current(tmp_path, monkeypatch):
    """The refresh driver's note mapping consumes these exact values."""
    import config
    b = _load()
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "history-rag.db"))
    monkeypatch.setattr(config, "APPUSAGE_DB", str(tmp_path / "appusage.db"))
    monkeypatch.setenv("CLAUDE_RAG_BACKUP_DIR", str(tmp_path / "bk"))
    _mk_db(str(tmp_path / "history-rag.db"), ["a"])
    assert b.main() == {"history-rag": "written", "appusage": "current"}
    assert b.main() == {"history-rag": "current", "appusage": "current"}

def test_config_defaults(monkeypatch):
    b = _load()
    monkeypatch.setenv("CLAUDE_RAG_BACKUP_KEEP", "3")
    assert b.keep_count() == 3
    monkeypatch.setenv("CLAUDE_RAG_BACKUP_DIR", "/tmp/xyz")
    assert b.backup_dir() == "/tmp/xyz"
