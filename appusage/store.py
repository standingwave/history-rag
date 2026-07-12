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
import os
import plistlib
import subprocess
import time

# Repo root is on sys.path when run via index.py; the daemon/report scripts add
# it themselves before importing this module.
from config import APPUSAGE_DB

# Derived-metric definitions (definitions, not preferences — no config knobs).
SWITCH_GAP = 60          # max gap (s) for adjacent segments to count as a direct app switch
FOCUS_MERGE_GAP = 300    # same-app gap (s) bridged when merging focus blocks
FOCUS_MIN = 25 * 60      # active seconds for a merged block to count as focus
BREAK_MIN = 15 * 60      # gap (s) that counts as a break rather than a pause
CALL_MIN = 180           # mic-live seconds before a stretch counts as a call
MEETING_APPS = {         # bundle ids whose overlap labels a call
    "us.zoom.xos", "com.microsoft.teams", "com.microsoft.teams2",
    "com.apple.FaceTime", "com.tinyspeck.slackmacgap", "com.hnc.Discord",
    "Cisco-Systems.Spark",
}
CATEGORY_RECHECK = 7 * 86400  # cached categories re-resolve after this (s):
                              # apps gain categories on update, deleted apps age out

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
    db.execute("""CREATE TABLE IF NOT EXISTS mic_segments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_ts REAL NOT NULL,
        end_ts REAL NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS app_meta(
        bundle_id TEXT PRIMARY KEY,
        category TEXT,            -- NULL = looked, none declared (or app gone)
        checked_ts REAL NOT NULL)""")
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

def _resolve_category(bundle_id: str):
    """The app's own LSApplicationCategoryType, located via Spotlight, with
    the public.app-category. prefix stripped. The key is optional outside
    the App Store, so None is a first-class answer — and any failure
    (Spotlight off, app gone, unreadable plist) degrades to None, never
    an error."""
    try:
        r = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"],
            capture_output=True, text=True, timeout=10)
        hits = r.stdout.splitlines() if r.returncode == 0 else []
        if not hits or not hits[0].strip():
            return None
        with open(os.path.join(hits[0].strip(), "Contents", "Info.plist"),
                  "rb") as f:
            cat = plistlib.load(f).get("LSApplicationCategoryType") or ""
        return cat.removeprefix("public.app-category.") or None
    except Exception:
        return None

def categories(db, bundle_ids) -> dict:
    """{bundle_id: category|None} through the app_meta cache; misses and
    entries older than CATEGORY_RECHECK resolve via Spotlight."""
    now = time.time()
    cache = {b: (cat, ts) for b, cat, ts in db.execute(
        "SELECT bundle_id, category, checked_ts FROM app_meta")}
    out, dirty = {}, False
    for b in {b for b in bundle_ids if b}:
        hit = cache.get(b)
        if hit and now - hit[1] < CATEGORY_RECHECK:
            out[b] = hit[0]
            continue
        out[b] = _resolve_category(b)
        db.execute("INSERT OR REPLACE INTO app_meta VALUES (?,?,?)",
                   (b, out[b], now))
        dirty = True
    if dirty:
        db.commit()
    return out

def category_rollup(apps: dict, cats: dict):
    """{category|'other': seconds} for one day's daily_apps entry, or None
    when nothing resolves. Uncategorized and unbundled time lands in
    'other', never dropped — omitting the biggest app would misrepresent
    the day."""
    by, resolved = {}, False
    for info in apps.values():
        cat = cats.get(info["bundle_id"]) if info["bundle_id"] else None
        resolved = resolved or cat is not None
        by[cat or "other"] = by.get(cat or "other", 0.0) + info["seconds"]
    return by if resolved else None

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

def day_segments(db, day: str):
    """Ordered (app, bundle_id, start_ts, end_ts) for segments starting on
    local day `day` (same start-day attribution as daily_apps)."""
    d = datetime.date.fromisoformat(day)
    t0 = datetime.datetime.combine(d, datetime.time()).timestamp()
    t1 = datetime.datetime.combine(
        d + datetime.timedelta(days=1), datetime.time()).timestamp()
    return db.execute(
        "SELECT app, bundle_id, start_ts, end_ts FROM segments "
        "WHERE start_ts >= ? AND start_ts < ? AND end_ts > start_ts "
        "ORDER BY start_ts", (t0, t1)).fetchall()

def day_calls(db, day: str, segs=None):
    """Calls for one local day (start-day attribution, like day_segments):
    [(start_ts, end_ts, label|None)]. A call is a mic segment >= CALL_MIN —
    shorter blips are Siri/dictation, not calls. The label is the overlapping
    MEETING_APPS app with the most overlap; a background call you never look
    at gets None."""
    d = datetime.date.fromisoformat(day)
    t0 = datetime.datetime.combine(d, datetime.time()).timestamp()
    t1 = datetime.datetime.combine(
        d + datetime.timedelta(days=1), datetime.time()).timestamp()
    mics = db.execute(
        "SELECT start_ts, end_ts FROM mic_segments "
        "WHERE start_ts >= ? AND start_ts < ? AND end_ts - start_ts >= ? "
        "ORDER BY start_ts", (t0, t1, CALL_MIN)).fetchall()
    if not mics:
        return []
    if segs is None:
        segs = day_segments(db, day)
    out = []
    for cs, ce in mics:
        overlap = {}
        for app, bundle, s, e in segs:
            if bundle in MEETING_APPS and min(ce, e) > max(cs, s):
                overlap[app] = overlap.get(app, 0) + min(ce, e) - max(cs, s)
        out.append((cs, ce, max(overlap, key=overlap.get) if overlap else None))
    return out

def day_shape(db, day: str):
    """Derived shape of one local day, or None when it has no segments:
    {first, last: epoch bounds, active_seconds, switches,
     breaks: [(gap_start_ts, gap_seconds)],
     focus: [(display_name, start_ts, end_ts, active_seconds)],
     calls: day_calls() result}.
    A switch is an adjacent different-app pair within SWITCH_GAP — idle
    returns don't qualify (idle gaps are >= the daemon's 120s threshold).
    A focus block merges same-app segments across gaps <= FOCUS_MERGE_GAP;
    any other-app segment splits it regardless of duration."""
    segs = day_segments(db, day)
    if not segs:
        return None

    switches, breaks = 0, []
    for (pa, pb, _, pe), (ca, cb, cs, _) in zip(segs, segs[1:]):
        gap = cs - pe
        if gap >= BREAK_MIN:
            breaks.append((pe, gap))
        elif gap <= SWITCH_GAP and not same_app(pa, pb, ca, cb):
            switches += 1

    focus, run = [], None   # run: [name, bundle, start, end, active]
    for name, bundle, s, e in segs:
        if run and same_app(run[0], run[1], name, bundle) and s - run[3] <= FOCUS_MERGE_GAP:
            run[4] += e - s
            run[0], run[1], run[3] = name, bundle or run[1], e
        else:
            if run and run[4] >= FOCUS_MIN:
                focus.append((run[0], run[2], run[3], run[4]))
            run = [name, bundle, s, e, e - s]
    if run and run[4] >= FOCUS_MIN:
        focus.append((run[0], run[2], run[3], run[4]))

    return {"first": segs[0][2], "last": segs[-1][3],
            "active_seconds": sum(e - s for _, _, s, e in segs),
            "switches": switches, "breaks": breaks, "focus": focus,
            "calls": day_calls(db, day, segs)}

def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"

def fmt_clock(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
