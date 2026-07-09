"""App-usage source: daily per-app time totals from the tracker daemon, plus
one "day shape" chunk per day (active bounds, breaks, switch rate, focus
sessions) — the day's arc, derived entirely from the same segments.

Emits one chunk per (day, app), including today. Today's totals keep growing,
so its chunk texts change between index runs; the indexer re-embeds chunks
whose text changed, so today stays current and past days settle once finished.

Yields nothing if the tracker DB doesn't exist, so this source is a no-op for
anyone not running the daemon.
"""
import os, datetime, hashlib
from config import APPUSAGE_DB

MIN_SECONDS = 60         # skip apps you barely touched on a given day
MIN_DAY_SECONDS = 600    # skip the day-shape chunk for near-empty days

def _day_utc(day: str) -> str:
    """A chunk summarizes a *local* day; stamp it at local midnight in UTC so
    local-day query windows (converted to UTC by the server) contain it."""
    return datetime.datetime.fromisoformat(day).astimezone(
        datetime.timezone.utc).isoformat()

def _chunk(day: str, key: str, text: str, meta: dict):
    cid = "appusage:" + hashlib.sha256(key.encode()).hexdigest()[:24]
    return cid, text, {"source": "appusage", "timestamp": _day_utc(day),
                       "location": "appusage", "meta": meta}

def _dayshape_chunk(store, db, day: str, weekday: str):
    shape = store.day_shape(db, day)
    if not shape or shape["active_seconds"] < MIN_DAY_SECONDS:
        return None
    clock, dur = store.fmt_clock, store.fmt_duration
    parts = [f"On {day} ({weekday}), "
             f"active {clock(shape['first'])}–{clock(shape['last'])}."]
    if shape["breaks"]:
        n, away = len(shape["breaks"]), sum(gap for _, gap in shape["breaks"])
        parts.append(f"{n} break{'s' if n != 1 else ''} "
                     f"totaling {dur(away)} away.")
    n = shape["switches"]
    rate = n * 3600 / shape["active_seconds"]
    parts.append(f"{n} app switch{'es' if n != 1 else ''} ({rate:.1f}/hour).")
    if shape["focus"]:
        parts.append("Focus sessions: " + ", ".join(
            f"{dur(secs)} in {app} ({clock(s)}–{clock(e)})"
            for app, s, e, secs in shape["focus"]) + ".")
    meta = {"date": day, "first": clock(shape["first"]), "last": clock(shape["last"]),
            "switches": shape["switches"],
            "active_seconds": int(shape["active_seconds"]),
            "breaks": [{"start": clock(s), "minutes": int(gap // 60)}
                       for s, gap in shape["breaks"]],
            "focus": [{"app": app, "start": clock(s), "minutes": int(secs // 60)}
                      for app, s, _, secs in shape["focus"]]}
    return _chunk(day, f"day:{day}", " ".join(parts), meta)

def iter_chunks():
    if not os.path.exists(APPUSAGE_DB):
        return
    from appusage import store
    db = store.connect()
    store.setup(db)

    for day, apps in store.daily_apps(db).items():
        weekday = datetime.date.fromisoformat(day).strftime("%A")
        for app, info in apps.items():
            if info["seconds"] < MIN_SECONDS:
                continue
            meta = {"app": app, "date": day, "seconds": int(info["seconds"])}
            if info["bundle_id"]:
                meta["bundle_id"] = info["bundle_id"]
            yield _chunk(day, f"{day}:{app}",
                         f"On {day} ({weekday}), spent "
                         f"{store.fmt_duration(info['seconds'])} in {app}.", meta)
        shaped = _dayshape_chunk(store, db, day, weekday)
        if shaped:
            yield shaped
