#!/usr/bin/env python3
"""
App-usage tracker daemon (macOS). Samples the frontmost app and idle time every
INTERVAL seconds and records how long you spend in each app into ~/.claude/
appusage.db. Time while the machine is idle (no input for IDLE_THRESHOLD
seconds) or asleep is not counted — unless a display wake lock is held
(video playback, presenting) or the mic is live (a call), both of which
count as present-but-passive. Mic-live stretches are also recorded into
mic_segments so readers can derive a calls timeline.

Zero dependencies — reads the frontmost app via `lsappinfo`, idle time via
`ioreg`, wake locks via `pmset`, and mic state via in-process CoreAudio
(see mic.py), all permission-free. Run directly for testing, or under launchd (see
com.user.appusage.plist) to track continuously.
"""
import os, sys, re, time, signal, subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from appusage import store, mic

INTERVAL = int(os.environ.get("APPUSAGE_INTERVAL", "20"))       # seconds per sample
IDLE_THRESHOLD = int(os.environ.get("APPUSAGE_IDLE", "120"))    # idle secs -> stop counting

_NAME_RE = re.compile(r'"LSDisplayName"="([^"]*)"')
_BUNDLE_RE = re.compile(r'"CFBundleIdentifier"="([^"]*)"')
_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')
_WAKE_RE = re.compile(r"^\s*PreventUserIdleDisplaySleep\s+(\d+)", re.M)

def frontmost_app():
    """(localized name, bundle id) of the frontmost app; (None, None) during
    transitions. A name with no bundle ID is still a valid sample."""
    try:
        asn = subprocess.run(["lsappinfo", "front"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if not asn:
            return None, None
        out = subprocess.run(
            ["lsappinfo", "info", "-only", "name", "-only", "bundleid", asn],
            capture_output=True, text=True, timeout=5).stdout
        name = _NAME_RE.search(out)
        bundle = _BUNDLE_RE.search(out)
        return ((name.group(1) or None) if name else None,
                (bundle.group(1) or None) if bundle else None)
    except (subprocess.SubprocessError, OSError):
        return None, None

def idle_seconds():
    """Seconds since the last user input (keyboard/mouse)."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"],
                             capture_output=True, text=True, timeout=5).stdout
        m = _IDLE_RE.search(out)
        return int(m.group(1)) / 1_000_000_000 if m else 0.0
    except (subprocess.SubprocessError, OSError):
        return 0.0

def display_wake_lock():
    """True while some process holds PreventUserIdleDisplaySleep — video
    playback, presenting, or a video call. Parses the summary count only;
    the owners are helper processes and aren't worth mapping to apps."""
    try:
        out = subprocess.run(["pmset", "-g", "assertions"],
                             capture_output=True, text=True, timeout=5).stdout
        m = _WAKE_RE.search(out)
        return bool(m) and int(m.group(1)) > 0
    except (subprocess.SubprocessError, OSError):
        return False

def is_active(mic_live=False):
    """Present at the machine: recent input, a live mic (a call), or a
    display wake lock (playback, presenting). Ordered cheapest-first —
    mic_live is already sampled, so the pmset subprocess only runs when
    idle and mic-silent."""
    return idle_seconds() < IDLE_THRESHOLD or mic_live or display_wake_lock()

def _open_segment(db):
    return db.execute(
        "SELECT id, app, bundle_id, end_ts FROM segments "
        "WHERE closed=0 ORDER BY id DESC LIMIT 1").fetchone()

def tick(db, app, now, bundle=None, max_gap=None):
    """One sampling step: extend, close, or open segments for `app` at `now`.
    A same-app gap longer than max_gap means the machine slept between ticks —
    close the old segment instead of back-filling the sleep as usage."""
    if max_gap is None:
        max_gap = INTERVAL * 3
    cur = _open_segment(db)
    if app is None:                             # idle, asleep, or unknown
        if cur:
            db.execute("UPDATE segments SET closed=1 WHERE id=?", (cur[0],))
    elif cur and store.same_app(cur[1], cur[2], app, bundle) and now - cur[3] <= max_gap:
        db.execute("UPDATE segments SET end_ts=? WHERE id=?", (now, cur[0]))
    else:                                       # switched apps, or woke up
        if cur:
            db.execute("UPDATE segments SET closed=1 WHERE id=?", (cur[0],))
        db.execute("INSERT INTO segments(app, bundle_id, start_ts, end_ts, closed) "
                   "VALUES (?,?,?,?,0)", (app, bundle, now, now))
    db.commit()

def _open_mic(db):
    return db.execute("SELECT id, end_ts FROM mic_segments WHERE closed=0 "
                      "ORDER BY id DESC LIMIT 1").fetchone()

def mic_tick(db, live, now, max_gap=None):
    """mic_segments mirror of tick(): extend, close, or open on the mic
    boolean, with the same sleep-gap rule (a gap longer than max_gap means
    the machine slept — close rather than back-fill)."""
    if max_gap is None:
        max_gap = INTERVAL * 3
    cur = _open_mic(db)
    if not live:
        if cur:
            db.execute("UPDATE mic_segments SET closed=1 WHERE id=?", (cur[0],))
    elif cur and now - cur[1] <= max_gap:
        db.execute("UPDATE mic_segments SET end_ts=? WHERE id=?", (now, cur[0]))
    else:
        if cur:
            db.execute("UPDATE mic_segments SET closed=1 WHERE id=?", (cur[0],))
        db.execute("INSERT INTO mic_segments(start_ts, end_ts, closed) "
                   "VALUES (?,?,0)", (now, now))
    db.commit()

def main():
    db = store.connect()
    store.setup(db)
    # Any segment left open by a previous run is stale — finalize it.
    db.execute("UPDATE segments SET closed=1 WHERE closed=0")
    db.execute("UPDATE mic_segments SET closed=1 WHERE closed=0")
    db.commit()

    running = {"v": True}
    def stop(*_):
        running["v"] = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while running["v"]:
        mic_live = mic.mic_in_use()
        app, bundle = frontmost_app() if is_active(mic_live) else (None, None)
        now = time.time()
        tick(db, app, now, bundle=bundle)
        mic_tick(db, mic_live, now)

        # Sleep in short slices so SIGTERM is handled promptly.
        for _ in range(INTERVAL):
            if not running["v"]:
                break
            time.sleep(1)

    db.execute("UPDATE segments SET closed=1 WHERE closed=0")
    db.execute("UPDATE mic_segments SET closed=1 WHERE closed=0")
    db.commit()

if __name__ == "__main__":
    main()
