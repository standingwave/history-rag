"""Day-shape derived metrics (switches, focus blocks, breaks, bounds),
bundle-ID capture/migration/coalescing, and the day-shape chunk + report line.
Local times assume the America/Los_Angeles pin in conftest."""
import datetime, sqlite3
from appusage import store, daemon, report
from sources import appusage as appusage_src

B = datetime.datetime(2025, 1, 6, 8, 42).timestamp()   # Mon 08:42 local

def _db():
    db = sqlite3.connect(":memory:")
    store.setup(db)
    return db

def seg(db, app, start, end, bundle=None):
    db.execute("INSERT INTO segments(app, bundle_id, start_ts, end_ts, closed) "
               "VALUES (?,?,?,?,1)", (app, bundle, start, end))

# ── schema migration ─────────────────────────────────────────────────────────

def test_setup_migrates_legacy_schema():
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE segments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app TEXT NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0)""")
    store.setup(db)
    cols = {r[1] for r in db.execute("PRAGMA table_info(segments)")}
    assert "bundle_id" in cols
    store.setup(db)                    # idempotent on a current DB
    store._add_bundle_column(db)       # lost ALTER race is swallowed, not raised

# ── tick keying ───────────────────────────────────────────────────────────────

def test_tick_keys_on_bundle_id():
    db = _db()
    daemon.tick(db, "Code", 0, bundle="com.ms.code", max_gap=60)
    daemon.tick(db, "Visual Studio Code", 20, bundle="com.ms.code", max_gap=60)
    rows = db.execute("SELECT app, end_ts, closed FROM segments").fetchall()
    assert rows == [("Code", 20, 0)]   # renamed app, same bundle -> extends
    daemon.tick(db, "Code", 40, bundle="com.other", max_gap=60)
    assert db.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 2

def test_tick_null_bundle_falls_back_to_name():
    db = _db()
    daemon.tick(db, "Xcode", 0, max_gap=60)                       # legacy sample
    daemon.tick(db, "Xcode", 20, bundle="com.apple.dt.Xcode", max_gap=60)
    assert db.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1

# ── coalescing ────────────────────────────────────────────────────────────────

def test_daily_apps_coalesces_by_bundle():
    db = _db()
    seg(db, "Code", B, B + 100, "com.ms.code")
    seg(db, "Visual Studio Code", B + 200, B + 300, "com.ms.code")
    seg(db, "Slack", B + 400, B + 500)              # NULL bundle keeps name key
    apps = store.daily_apps(db)["2025-01-06"]
    assert apps["Visual Studio Code"]["seconds"] == 200   # latest name labels
    assert apps["Visual Studio Code"]["bundle_id"] == "com.ms.code"
    assert "Code" not in apps
    assert apps["Slack"] == {"seconds": 100.0, "bundle_id": None}

def test_transition_day_sums_null_and_bundle_groups():
    db = _db()
    seg(db, "Xcode", B, B + 100)                              # pre-upgrade
    seg(db, "Xcode", B + 200, B + 500, "com.apple.dt.Xcode")  # post-upgrade
    assert store.daily_durations(db)["2025-01-06"]["Xcode"] == 400
    assert store.daily_apps(db)["2025-01-06"]["Xcode"]["bundle_id"] == "com.apple.dt.Xcode"

# ── switches ─────────────────────────────────────────────────────────────────

def test_switch_counting():
    db = _db()
    seg(db, "A", B, B + 100)
    seg(db, "B", B + 101, B + 200)          # 1s gap, different app -> switch
    seg(db, "A", B + 400, B + 500)          # 200s gap = idle return, no switch
    shape = store.day_shape(db, "2025-01-06")
    assert shape["switches"] == 1

def test_upgrade_boundary_is_not_a_switch():
    db = _db()
    seg(db, "Xcode", B, B + 100)
    seg(db, "Xcode", B + 101, B + 200, "com.apple.dt.Xcode")
    assert store.day_shape(db, "2025-01-06")["switches"] == 0

# ── focus blocks ─────────────────────────────────────────────────────────────

def test_focus_merges_small_gaps_and_qualifies_on_active_sum():
    db = _db()
    seg(db, "Xcode", B, B + 1000, "x")
    seg(db, "Xcode", B + 1200, B + 1800, "x")       # 200s gap <= merge gap
    focus = store.day_shape(db, "2025-01-06")["focus"]
    assert focus == [("Xcode", B, B + 1800, 1600)]  # 1600s active >= 25m

def test_focus_split_by_interruption_and_subthreshold_excluded():
    db = _db()
    seg(db, "Xcode", B, B + 1000, "x")
    seg(db, "Slack", B + 1001, B + 1060, "s")       # 1m glance still splits
    seg(db, "Xcode", B + 1061, B + 2000, "x")
    assert store.day_shape(db, "2025-01-06")["focus"] == []   # runs 1000s + 939s

def test_focus_merges_across_upgrade_boundary():
    db = _db()
    seg(db, "Xcode", B, B + 1000)                             # legacy NULL bundle
    seg(db, "Xcode", B + 1010, B + 1600, "com.apple.dt.Xcode")
    focus = store.day_shape(db, "2025-01-06")["focus"]
    assert focus == [("Xcode", B, B + 1600, 1590)]

# ── breaks + bounds ──────────────────────────────────────────────────────────

def test_breaks_bounds_and_start_day_attribution():
    db = _db()
    seg(db, "A", B, B + 600)
    seg(db, "A", B + 1600, B + 2000)                # 1000s gap >= 15m = break
    shape = store.day_shape(db, "2025-01-06")
    assert shape["breaks"] == [(B + 600, 1000)]
    assert shape["first"] == B and shape["last"] == B + 2000
    # midnight-spanning segment belongs to its start day
    late = datetime.datetime(2025, 1, 6, 23, 59).timestamp()
    seg(db, "A", late, late + 600)
    assert store.day_shape(db, "2025-01-06")["last"] == late + 600
    assert store.day_shape(db, "2025-01-07") is None

# ── day-shape chunk ──────────────────────────────────────────────────────────

def _dayshape_chunks():
    return {c[2]["meta"]["date"]: c for c in appusage_src.iter_chunks()
            if "first" in c[2]["meta"]}

def test_dayshape_chunk_text_meta_and_min_day_floor():
    db = store.connect()                 # frozen at the conftest tmp path
    store.setup(db)
    seg(db, "Xcode", B, B + 3120, "com.apple.dt.Xcode")   # 52m focus
    seg(db, "Slack", B + 7120, B + 7420)                  # after a 4000s break
    seg(db, "Chrome", B + 7421, B + 7541, "com.chrome")   # 1s gap = switch
    tiny = datetime.datetime(2025, 1, 7, 9, 0).timestamp()
    seg(db, "Slack", tiny, tiny + 400)                    # under the 10m floor
    db.commit()
    try:
        chunks = _dayshape_chunks()
        assert "2025-01-07" not in chunks                 # floor holds
        cid, text, rec = chunks["2025-01-06"]
        assert text == ("On 2025-01-06 (Monday), active 08:42–10:47. "
                        "1 break totaling 1h 6m away. 1 app switch (1.0/hour). "
                        "Focus sessions: 52m in Xcode (08:42–09:34).")
        assert rec["timestamp"] == "2025-01-06T08:00:00+00:00"   # PST midnight
        m = rec["meta"]
        assert (m["switches"], m["active_seconds"]) == (1, 3540)
        assert m["breaks"] == [{"start": "09:34", "minutes": 66}]
        assert m["focus"] == [{"app": "Xcode", "start": "08:42", "minutes": 52}]
        assert cid == next(iter(_dayshape_chunks().values()))[0]  # id stable
        # per-app chunks carry bundle_id only when known
        by_app = {r["meta"]["app"]: r["meta"] for _, _, r in appusage_src.iter_chunks()
                  if r["meta"].get("date") == "2025-01-06" and "app" in r["meta"]}
        assert by_app["Xcode"]["bundle_id"] == "com.apple.dt.Xcode"
        assert "bundle_id" not in by_app["Slack"]
    finally:
        db.execute("DELETE FROM segments")
        db.commit()

def test_dayshape_chunk_omits_empty_clauses():
    db = store.connect()
    store.setup(db)
    seg(db, "Xcode", B, B + 700, "x")     # no breaks, no focus, no switches
    db.commit()
    try:
        _, text, _ = _dayshape_chunks()["2025-01-06"]
        assert "break" not in text and "Focus" not in text
        assert "0 app switches (0.0/hour)." in text
    finally:
        db.execute("DELETE FROM segments")
        db.commit()

# ── report line ──────────────────────────────────────────────────────────────

def test_report_shape_line():
    shape = {"first": B, "last": B + 3540, "active_seconds": 3540.0,
             "switches": 47, "breaks": [(B + 600, 3900)],
             "focus": [("Xcode", B, B + 3120, 3120)], "calls": []}
    line = report.shape_line(shape)
    assert line == ("08:42–09:41 · 47 switches (47.8/h) · "
                    "1 break (1h 5m) · focus: Xcode 52m")
