"""Backup tool: once-per-day semantics, faithful copies, retention pruning."""
import importlib.util, os, pathlib, sqlite3

def _load():
    p = pathlib.Path(__file__).resolve().parent.parent / "tools" / "backup.py"
    spec = importlib.util.spec_from_file_location("backup_tool", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

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

def test_config_defaults(monkeypatch):
    b = _load()
    monkeypatch.setenv("CLAUDE_RAG_BACKUP_KEEP", "3")
    assert b.keep_count() == 3
    monkeypatch.setenv("CLAUDE_RAG_BACKUP_DIR", "/tmp/xyz")
    assert b.backup_dir() == "/tmp/xyz"
