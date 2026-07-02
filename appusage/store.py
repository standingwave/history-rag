"""Shared storage for the app-usage tracker.

The daemon records one row per continuous stretch in an app (a "segment");
readers aggregate segments into per-day, per-app totals. Segments carry a
`closed` flag: the current segment stays open (0) and its end_ts advances each
tick until the app changes or the machine goes idle, at which point it closes.
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
        start_ts REAL NOT NULL,
        end_ts REAL NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0)""")
    db.commit()

def daily_durations(db):
    """Return {date_iso: {app: seconds}}. A segment is attributed to the day it
    started on (midnight-spanning segments count wholly toward the start day)."""
    out = collections.defaultdict(lambda: collections.defaultdict(float))
    for app, start, end in db.execute("SELECT app, start_ts, end_ts FROM segments"):
        if end <= start:
            continue
        day = datetime.date.fromtimestamp(start).isoformat()
        out[day][app] += end - start
    return out

def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"
