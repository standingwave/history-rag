"""Shared storage for the app-usage tracker.

The daemon records one row per continuous stretch in an app (a "segment");
readers aggregate segments into per-day, per-app totals. Segments carry a
`closed` flag: the current segment stays open (0) and its end_ts advances each
tick until the app changes or the machine goes idle, at which point it closes.
Segments also carry the app's bundle ID — the stable identity display names
don't provide; rows written before bundle capture existed have NULL.
"""
import sqlite3
import datetime
import collections

# Repo root is on sys.path when run via index.py; the daemon/report scripts add
# it themselves before importing this module.
from config import APPUSAGE_DB

def connect():
    db = sqlite3.connect(APPUSAGE_DB, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")   # daemon writes while readers read
    return db

def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS segments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app TEXT NOT NULL,
        bundle_id TEXT,
        start_ts REAL NOT NULL,
        end_ts REAL NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0)""")
    cols = {row[1] for row in db.execute("PRAGMA table_info(segments)")}
    if "bundle_id" not in cols:
        _add_bundle_column(db)
    db.commit()

def _add_bundle_column(db):
    """Migrate a pre-bundle-ID table. Daemon, report, index source, and server
    all run setup(), so another process can win the ALTER between our column
    check and this statement — losing that race is success, not an error."""
    try:
        db.execute("ALTER TABLE segments ADD COLUMN bundle_id TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

def same_app(name_a, bundle_a, name_b, bundle_b):
    """Whether two samples/segments are the same app: bundle IDs when both
    sides have one, else display names. Falling back (rather than treating a
    NULL-bundle legacy segment as a distinct app) keeps continuity across the
    daemon upgrade that introduced bundle capture."""
    if bundle_a and bundle_b:
        return bundle_a == bundle_b
    return name_a == name_b

def daily_apps(db):
    """{date_iso: {display_name: {"seconds": float, "bundle_id": str|None}}}.
    Segments sharing a bundle ID coalesce under the group's most recent
    display name (localized names can drift across app versions); NULL-bundle
    rows group by name. A segment is attributed to the day it started on
    (midnight-spanning segments count wholly toward the start day)."""
    # (day, group key) -> [seconds, latest start, latest name, bundle].
    # The \x00 prefix keeps name keys from ever colliding with a bundle ID.
    groups = {}
    for app, bundle, start, end in db.execute(
            "SELECT app, bundle_id, start_ts, end_ts FROM segments"):
        if end <= start:
            continue
        day = datetime.date.fromtimestamp(start).isoformat()
        g = groups.setdefault((day, bundle or "\x00" + app), [0.0, -1.0, app, bundle])
        g[0] += end - start
        if start >= g[1]:
            g[1], g[2] = start, app
    out = {}
    for (day, _), (secs, _, name, bundle) in groups.items():
        # Accumulate, never assign: on the transition day the same app shows
        # up as both a NULL-bundle group and a bundle group with this name.
        entry = out.setdefault(day, {}).setdefault(
            name, {"seconds": 0.0, "bundle_id": None})
        entry["seconds"] += secs
        entry["bundle_id"] = entry["bundle_id"] or bundle
    return out

def daily_durations(db):
    """Return {date_iso: {app: seconds}} (see daily_apps for the grouping
    rules). Nested defaultdicts, so absent apps read as 0."""
    out = collections.defaultdict(lambda: collections.defaultdict(float))
    for day, apps in daily_apps(db).items():
        for name, info in apps.items():
            out[day][name] = info["seconds"]
    return out

def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"
