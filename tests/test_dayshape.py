"""Bundle-ID capture: schema migration, tick keying, and coalescing.
Local times assume the America/Los_Angeles pin in conftest."""
import datetime, sqlite3
from appusage import store, daemon

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
