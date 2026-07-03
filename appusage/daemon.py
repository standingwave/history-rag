#!/usr/bin/env python3
"""
App-usage tracker daemon (macOS). Samples the frontmost app and idle time every
INTERVAL seconds and records how long you spend in each app into ~/.claude/
appusage.db. Time while the machine is idle (no input for IDLE_THRESHOLD
seconds) or asleep is not counted.

Zero dependencies — reads the frontmost app via `lsappinfo` and idle time via
`ioreg`, both permission-free. Run directly for testing, or under launchd (see
com.user.appusage.plist) to track continuously.
"""
import os, sys, re, time, signal, subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from appusage import store

INTERVAL = int(os.environ.get("APPUSAGE_INTERVAL", "20"))       # seconds per sample
IDLE_THRESHOLD = int(os.environ.get("APPUSAGE_IDLE", "120"))    # idle secs -> stop counting

_NAME_RE = re.compile(r'"[^"]*"="([^"]*)"')
_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')

def frontmost_app():
    """Localized name of the frontmost app, or None during transitions."""
    try:
        asn = subprocess.run(["lsappinfo", "front"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(["lsappinfo", "info", "-only", "name", asn],
                             capture_output=True, text=True, timeout=5).stdout
        m = _NAME_RE.search(out)
        return m.group(1) if m and m.group(1) else None
    except (subprocess.SubprocessError, OSError):
        return None

def idle_seconds():
    """Seconds since the last user input (keyboard/mouse)."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                             capture_output=True, text=True, timeout=5).stdout
        m = _IDLE_RE.search(out)
        return int(m.group(1)) / 1_000_000_000 if m else 0.0
    except (subprocess.SubprocessError, OSError):
        return 0.0

def _open_segment(db):
    return db.execute(
        "SELECT id, app, end_ts FROM segments WHERE closed=0 ORDER BY id DESC LIMIT 1"
    ).fetchone()

def tick(db, app, now, max_gap=None):
    """One sampling step: extend, close, or open segments for `app` at `now`.
    A same-app gap longer than max_gap means the machine slept between ticks —
    close the old segment instead of back-filling the sleep as usage."""
    if max_gap is None:
        max_gap = INTERVAL * 3
    cur = _open_segment(db)
    if app is None:                             # idle, asleep, or unknown
        if cur:
            db.execute("UPDATE segments SET closed=1 WHERE id=?", (cur[0],))
    elif cur and cur[1] == app and now - cur[2] <= max_gap:
        db.execute("UPDATE segments SET end_ts=? WHERE id=?", (now, cur[0]))
    else:                                       # switched apps, or woke up
        if cur:
            db.execute("UPDATE segments SET closed=1 WHERE id=?", (cur[0],))
        db.execute("INSERT INTO segments(app, start_ts, end_ts, closed) "
                   "VALUES (?,?,?,0)", (app, now, now))
    db.commit()

def main():
    db = store.connect()
    store.setup(db)
    # Any segment left open by a previous run is stale — finalize it.
    db.execute("UPDATE segments SET closed=1 WHERE closed=0")
    db.commit()

    running = {"v": True}
    def stop(*_):
        running["v"] = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running["v"]:
        active = idle_seconds() < IDLE_THRESHOLD
        tick(db, frontmost_app() if active else None, time.time())

        # Sleep in short slices so SIGTERM is handled promptly.
        for _ in range(INTERVAL):
            if not running["v"]:
                break
            time.sleep(1)

    db.execute("UPDATE segments SET closed=1 WHERE closed=0")
    db.commit()

if __name__ == "__main__":
    main()
