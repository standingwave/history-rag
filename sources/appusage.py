"""App-usage source: daily per-app time totals from the tracker daemon.

Emits one chunk per (day, app), including today. Today's total keeps growing, so
its chunk text changes between index runs; the indexer re-embeds chunks whose
text changed, so today stays current and past days settle once finished.

Yields nothing if the tracker DB doesn't exist, so this source is a no-op for
anyone not running the daemon.
"""
import os, datetime, hashlib
from config import APPUSAGE_DB

MIN_SECONDS = 60         # skip apps you barely touched on a given day

def _day_utc(day: str) -> str:
    """A chunk summarizes a *local* day; stamp it at local midnight in UTC so
    local-day query windows (converted to UTC by the server) contain it."""
    return datetime.datetime.fromisoformat(day).astimezone(
        datetime.timezone.utc).isoformat()

def iter_chunks():
    if not os.path.exists(APPUSAGE_DB):
        return
    from appusage import store
    db = store.connect()
    store.setup(db)

    for day, apps in store.daily_durations(db).items():
        weekday = datetime.date.fromisoformat(day).strftime("%A")
        for app, secs in apps.items():
            if secs < MIN_SECONDS:
                continue
            cid = "appusage:" + hashlib.sha256(f"{day}:{app}".encode()).hexdigest()[:24]
            text = f"On {day} ({weekday}), spent {store.fmt_duration(secs)} in {app}."
            yield cid, text, {
                "source": "appusage",
                "timestamp": _day_utc(day),
                "location": "appusage",
                "meta": {"app": app, "date": day, "seconds": int(secs)},
            }
