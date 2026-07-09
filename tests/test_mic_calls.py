"""Mic capture (probe, mic_segments ticks, idle-veto ordering) and the
derived calls timeline. Local times assume the America/Los_Angeles pin."""
import sqlite3
from appusage import store, daemon, mic

def _db():
    db = sqlite3.connect(":memory:")
    store.setup(db)
    return db

# ── probe ─────────────────────────────────────────────────────────────────────

def test_mic_in_use_logic(monkeypatch):
    monkeypatch.setattr(mic, "_coreaudio", lambda: object())
    monkeypatch.setattr(mic, "_devices", lambda ca: [1, 2, 3])
    monkeypatch.setattr(mic, "_has_input", lambda ca, d: d != 1)   # 1 = output-only
    monkeypatch.setattr(mic, "_running", lambda ca, d: d == 2)
    assert mic.mic_in_use() is True          # device 2: input and running
    monkeypatch.setattr(mic, "_running", lambda ca, d: d == 1)     # only output runs
    assert mic.mic_in_use() is False
    monkeypatch.setattr(mic, "_devices", lambda ca: (_ for _ in ()).throw(OSError))
    assert mic.mic_in_use() is False         # any failure -> not in use
    monkeypatch.setattr(mic, "_coreaudio", lambda: None)           # non-mac
    assert mic.mic_in_use() is False

def test_mic_in_use_live_returns_bool():
    assert mic.mic_in_use() in (True, False)   # real call; False off-mac

# ── mic ticks ─────────────────────────────────────────────────────────────────

def test_mic_tick_extend_close_and_sleep_gap():
    db = _db()
    daemon.mic_tick(db, True, 1000, max_gap=60)
    daemon.mic_tick(db, True, 1020, max_gap=60)      # extend
    daemon.mic_tick(db, True, 9000, max_gap=60)      # slept: close + reopen
    rows = db.execute(
        "SELECT start_ts, end_ts, closed FROM mic_segments ORDER BY id").fetchall()
    assert rows == [(1000, 1020, 1), (9000, 9000, 0)]
    daemon.mic_tick(db, False, 9020, max_gap=60)     # mic off closes
    assert db.execute(
        "SELECT COUNT(*) FROM mic_segments WHERE closed=0").fetchone()[0] == 0

# ── active decision ordering ──────────────────────────────────────────────────

def test_mic_live_vetoes_idle_before_pmset(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "idle_seconds", lambda: 999.0)
    monkeypatch.setattr(daemon, "display_wake_lock",
                        lambda: calls.append("pmset") or False)
    assert daemon.is_active(mic_live=True) is True
    assert calls == []                               # pmset never consulted
    assert daemon.is_active(mic_live=False) is False
    assert calls == ["pmset"]                        # idle + silent -> pmset
