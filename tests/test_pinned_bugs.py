"""Tests pinning the bugs found in the 2026-07-03 review (TESTING.md top)."""
import sqlite3
import pytest

def test_rebuild_with_source_is_forbidden(monkeypatch):
    import index
    monkeypatch.setattr("sys.argv", ["index.py", "--rebuild", "--source", "shell"])
    with pytest.raises(SystemExit) as e:
        index.main()
    assert "--source" in str(e.value) and "rebuild" in str(e.value).lower()

def test_prune_requires_source(monkeypatch):
    import index
    monkeypatch.setattr("sys.argv", ["index.py", "--prune"])
    with pytest.raises(SystemExit) as e:
        index.main()
    assert "--prune requires --source" in str(e.value)

def _mem_store():
    from appusage import store
    db = sqlite3.connect(":memory:")
    store.setup(db)
    return db

def test_daemon_sleep_gap_not_counted():
    """Waking into the same app must NOT back-fill the sleep as usage."""
    from appusage import daemon, store
    db = _mem_store()
    daemon.tick(db, "Figma", 1000, max_gap=60)
    daemon.tick(db, "Figma", 1020, max_gap=60)      # normal extend
    daemon.tick(db, "Figma", 9000, max_gap=60)      # 2h13m gap = slept
    days = store.daily_durations(db)
    total = sum(v for apps in days.values() for v in apps.values())
    assert total == 20                               # not 8000
    open_rows = db.execute("SELECT COUNT(*) FROM segments WHERE closed=0").fetchone()[0]
    assert open_rows == 1                            # fresh post-wake segment

def test_daemon_normal_transitions():
    from appusage import daemon, store
    db = _mem_store()
    daemon.tick(db, "A", 0, max_gap=60)
    daemon.tick(db, "A", 20, max_gap=60)
    daemon.tick(db, "B", 40, max_gap=60)             # switch closes A
    daemon.tick(db, None, 60, max_gap=60)            # idle closes B
    days = store.daily_durations(db)
    apps = next(iter(days.values()))
    assert apps["A"] == 20 and apps["B"] == 0        # B never extended
    assert db.execute("SELECT COUNT(*) FROM segments WHERE closed=0").fetchone()[0] == 0
