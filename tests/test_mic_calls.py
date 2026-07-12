"""Mic capture (probe, mic_segments ticks, idle-veto ordering) and the
derived calls timeline. Local times assume the America/Los_Angeles pin."""
import datetime, sqlite3
import pytest
from appusage import store, daemon, mic, report
from sources import appusage as appusage_src

B = datetime.datetime(2025, 1, 6, 10, 0).timestamp()   # Mon 10:00 local

@pytest.fixture(autouse=True)
def _no_category_resolution(monkeypatch):
    """Keep iter_chunks off the real mdfind: resolution is machine-dependent
    and covered by test_app_category.py."""
    monkeypatch.setattr(store, "_resolve_category", lambda bundle_id: None)

def seg(db, app, start, end, bundle=None):
    db.execute("INSERT INTO segments(app, bundle_id, start_ts, end_ts, closed) "
               "VALUES (?,?,?,?,1)", (app, bundle, start, end))

def mic_seg(db, start, end):
    db.execute("INSERT INTO mic_segments(start_ts, end_ts, closed) "
               "VALUES (?,?,1)", (start, end))

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

# ── call derivation ──────────────────────────────────────────────────────────

def test_short_mic_blips_are_not_calls():
    db = _db()
    seg(db, "Xcode", B, B + 700, "x")
    mic_seg(db, B + 100, B + 250)                    # 150s < CALL_MIN
    assert store.day_calls(db, "2025-01-06") == []

def test_call_labeled_by_max_overlap_meeting_app():
    db = _db()
    seg(db, "Supacode", B, B + 1000, "app.supabit.supacode")
    seg(db, "zoom.us", B + 1000, B + 1400, "us.zoom.xos")
    seg(db, "Supacode", B + 1400, B + 3000, "app.supabit.supacode")
    mic_seg(db, B + 900, B + 2900)                   # call spans all three
    calls = store.day_calls(db, "2025-01-06")
    assert calls == [(B + 900, B + 2900, "zoom.us")] # meeting app wins, not dominant Supacode

def test_background_call_without_meeting_app_is_unlabeled():
    db = _db()
    seg(db, "Supacode", B, B + 2000, "app.supabit.supacode")
    mic_seg(db, B + 100, B + 1900)
    assert store.day_calls(db, "2025-01-06") == [(B + 100, B + 1900, None)]

def test_calls_attributed_to_start_day():
    db = _db()
    prev = datetime.datetime(2025, 1, 5, 23, 50).timestamp()
    mic_seg(db, prev, prev + 1200)                   # starts Sunday
    seg(db, "Xcode", B, B + 700, "x")
    assert store.day_calls(db, "2025-01-06") == []

# ── surfacing ────────────────────────────────────────────────────────────────

def test_dayshape_chunk_calls_sentence_and_meta():
    db = store.connect()                 # frozen at the conftest tmp path
    store.setup(db)
    seg(db, "zoom.us", B, B + 1200, "us.zoom.xos")
    seg(db, "Xcode", B + 1200, B + 2000, "x")
    mic_seg(db, B, B + 1140)                         # 19m call
    db.commit()
    try:
        chunk = next(c for c in appusage_src.iter_chunks()
                     if "first" in c[2]["meta"])
        assert "On calls 19m: 10:00–10:19 (zoom.us)." in chunk[1]
        assert chunk[2]["meta"]["calls"] == [
            {"start": "10:00", "minutes": 19, "app": "zoom.us"}]
    finally:
        db.execute("DELETE FROM segments")
        db.execute("DELETE FROM mic_segments")
        db.commit()

def test_report_shape_line_includes_calls():
    shape = {"first": B, "last": B + 3600, "active_seconds": 3600.0,
             "switches": 2, "breaks": [], "focus": [],
             "calls": [(B, B + 2820, "zoom.us")]}
    assert "calls: 47m" in report.shape_line(shape)
