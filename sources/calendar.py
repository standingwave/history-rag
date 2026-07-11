"""Calendar source: events as recall anchors — meetings and appointments are
the strongest "what did I do Tuesday" hooks the index otherwise lacks, and
future chunks answer "what's coming up Thursday".

Structured like the browser source: per-app readers behind one contract,
config picks which run ([calendar].apps, default [] so the source no-ops
until opted in — the apple store exists for every macOS user, so membership
in ALL_SOURCES must not mean indexing by default). Locations are namespaced
"app:calendar". Google/iCloud/Exchange are accounts, not apps: added via
System Settings -> Internet Accounts they sync into the same apple store as
Store rows, so the reader joins Calendar -> Store, skips disabled accounts,
and carries the account name in meta.

Each reader returns normalized events
    (uid, start, end, all_day, title, calendar, account,
     attendees, location_str, notes, repeats)
where `start` is unix seconds for timed events but an ISO date string for
all-day ones (whose stored instants are calendar days, not moments), and
`repeats` is a human rule ("yearly") only on a series-master fallback event.
Cross-app dedup is by (uid, start), first-configured app wins.

The apple store keeps expanded occurrences of recurring events in
OccurrenceCache — a ROLLING window (verified ~18 months back / 2 years
ahead), not an archive. Occurrences roll out of it and stop being yielded;
PRUNE_WINDOW_DAYS keeps them in the index as archive (index.py bounds
`--prune --source calendar` to chunks stamped inside the window). A series
with no cached rows falls back to one master chunk describing its rule —
never hand-rolled RRULE expansion. One-off events come from CalendarItem
directly (the cache holds those too; reading both would double-emit).

Failure semantics differ from browser because this source is pruned: a read
error in one app while another yielded raises (failing the run keeps prune
away from the failed app's chunks — all apps share one source column);
every configured store missing or unreadable yields nothing quietly
(calendar enabled before Full Disk Access is granted).

Secret filtering is per-field: a credential-looking title drops the event,
but notes or a room string only drop that field — Zoom invites are full of
"Password:" lines, and the meeting itself is exactly what recall needs.
"""
import os, hashlib, shutil, sqlite3, sys, tempfile
from datetime import datetime, timedelta, timezone
from sources.common import SECRET_RE

FUTURE_DAYS = 90         # index upcoming events this far ahead
PRUNE_WINDOW_DAYS = 30   # prune bound: chunks older than this are archive
NOTES_MAX = 300          # agendas help recall, pasted Zoom blurbs don't
ATTENDEES_MAX = 8        # names shown in text; meta always carries all

APPLE_DB = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb")

_APPLE_EPOCH = 978307200      # Apple/CF epoch (2001-01-01) -> unix

# Subscription/derived noise nobody means when asking what they were doing.
# User excludes ([calendar].exclude_calendars) add to these, never replace.
_DEFAULT_EXCLUDES = {
    "US Holidays", "Holiday", "Birthdays", "Facebook Birthdays",
    "Found in Mail", "Found in Natural Language", "DEFAULT_CALENDAR_NAME",
}

_FREQ = {1: ("daily", "days"), 2: ("weekly", "weeks"),
         3: ("monthly", "months"), 4: ("yearly", "years")}

def _settings():
    import config
    apps = [str(a) for a in config.get("calendar", "apps", "", []) or []]
    excl = _DEFAULT_EXCLUDES | {
        str(c) for c in config.get("calendar", "exclude_calendars", "", [])}
    return apps, excl

def _connect_copy(path: str):
    """calendard holds the store open; copy it and read the copy (browser's
    locked-DB pattern). Caller unlinks tmp."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copyfile(path, tmp)
        return sqlite3.connect(tmp), tmp
    except OSError:
        os.unlink(tmp)
        raise

def _attendee_names(db):
    """owner CalendarItem ROWID -> display names. Never emails: a bare-email
    identity contributes its local part, which is a name, not an address."""
    names: dict[int, list] = {}
    for owner, dn, first, last, email in db.execute(
            "SELECT p.owner_id, i.display_name, i.first_name, i.last_name, "
            "p.email FROM Participant p "
            "LEFT JOIN Identity i ON i.ROWID = p.identity_id"):
        name = dn or " ".join(x for x in (first, last) if x) or email or ""
        name = name.split("@")[0] if "@" in name else name
        if name and owner is not None:
            names.setdefault(owner, []).append(name)
    return names

def _repeat_desc(rule) -> str:
    if not rule or (rule[0] or 0) not in _FREQ:
        return "periodically"
    adverb, noun = _FREQ[rule[0]]
    n = int(rule[1] or 1)
    return f"every {n} {noun}" if n > 1 else adverb

def _read_apple(excludes):
    """Events from Calendar.sqlitedb. All-day starts: masters store UTC
    midnight of the date (take the UTC date), cache occurrences store LOCAL
    midnight (take the local date) — both verified live."""
    if not os.path.exists(APPLE_DB):
        return []
    db, tmp = _connect_copy(APPLE_DB)
    try:
        cals = {}      # Calendar ROWID -> (title, account)
        for cid, title, account in db.execute(
                "SELECT c.ROWID, c.title, s.name FROM Calendar c "
                "JOIN Store s ON s.ROWID = c.store_id "
                "WHERE IFNULL(s.disabled, 0) = 0"):
            if (title or "") not in excludes:
                cals[cid] = (title or "", account or "")
        names = _attendee_names(db)
        rooms = dict(db.execute("SELECT ROWID, title FROM Location"))
        horizon = (datetime.now(timezone.utc)
                   + timedelta(days=FUTURE_DAYS)).timestamp() - _APPLE_EPOCH

        out = []
        def add(uid, start, end, all_day, title, cal, att, room, notes,
                repeats=None):
            if all_day:
                # start arrives as a date already extracted by the caller
                out.append((uid, start, None, True, title, *cal, att,
                            room, notes, repeats))
            else:
                out.append((uid, start + _APPLE_EPOCH,
                            (end or start) + _APPLE_EPOCH, False, title,
                            *cal, att, room, notes, repeats))

        for (rid, title, notes, start, end, all_day, cal_id, uid, room_id,
             recurs) in db.execute(
                "SELECT ROWID, summary, description, start_date, end_date, "
                "all_day, calendar_id, unique_identifier, location_id, "
                "has_recurrences FROM CalendarItem "
                "WHERE summary IS NOT NULL AND start_date IS NOT NULL "
                "AND IFNULL(hidden, 0) = 0").fetchall():
            cal = cals.get(cal_id)
            if cal is None:
                continue
            uid = uid or f"rowid-{rid}"
            att = names.get(rid, [])
            room = rooms.get(room_id) or ""
            notes = notes or ""
            if not recurs:
                if start > horizon:
                    continue
                day = (datetime.fromtimestamp(start + _APPLE_EPOCH,
                                              tz=timezone.utc).date()
                       .isoformat() if all_day else None)
                add(uid, day if all_day else start, end, bool(all_day),
                    title, cal, att, room, notes)
                continue
            occ = db.execute(
                "SELECT occurrence_date, occurrence_end_date "
                "FROM OccurrenceCache WHERE event_id = ? "
                "AND occurrence_date <= ? ORDER BY occurrence_date",
                (rid, horizon)).fetchall()
            if occ:
                for s, e in occ:
                    day = (datetime.fromtimestamp(s + _APPLE_EPOCH).date()
                           .isoformat() if all_day else None)
                    add(uid, day if all_day else s, e, bool(all_day),
                        title, cal, att, room, notes)
            else:                    # cache rebuilt or series out of window
                rule = db.execute(
                    "SELECT frequency, interval FROM Recurrence "
                    "WHERE owner_id = ?", (rid,)).fetchone()
                day = (datetime.fromtimestamp(start + _APPLE_EPOCH,
                                              tz=timezone.utc).date()
                       .isoformat() if all_day else None)
                add(uid, day if all_day else start, end, bool(all_day),
                    title, cal, att, room, notes,
                    repeats=_repeat_desc(rule))
        return out
    finally:
        db.close()
        os.unlink(tmp)

_READERS = {"apple": _read_apple}

def _day_utc(day: str) -> str:
    """Local midnight of a local calendar day, in UTC (appusage convention),
    so local-day query windows contain the chunk."""
    return datetime.fromisoformat(day).astimezone(timezone.utc).isoformat()

def _chunk(app, ev):
    (uid, start, end, all_day, title, cal, account, att, room, notes,
     repeats) = ev
    if SECRET_RE.search(title):
        return None
    if room and SECRET_RE.search(room):
        room = ""
    if notes and SECRET_RE.search(notes):
        notes = ""
    notes = " ".join(notes.split())[:NOTES_MAX]

    if all_day:
        day, start_key = start, start
        ts = _day_utc(day)
        when = (f"on {day} "
                f"({datetime.fromisoformat(day):%A}), all day")
        meta_start, meta_end = day, day
    else:
        s = datetime.fromtimestamp(start, tz=timezone.utc)
        e = datetime.fromtimestamp(end or start, tz=timezone.utc)
        ts = start_key = s.isoformat()
        sl, el = s.astimezone(), e.astimezone()
        when = f"on {sl:%Y-%m-%d} ({sl:%A}) {sl:%H:%M}–{el:%H:%M}"
        meta_start, meta_end = s.isoformat(), e.isoformat()

    head = (f"Recurring calendar event ({repeats}) starting" if repeats
            else "Calendar event")
    when = when if not repeats else when.removeprefix("on ").split(", all")[0]
    text = f"{head} {when}: {title}"
    if room:
        text += f" at {room}"
    if att:
        shown = att[:ATTENDEES_MAX]
        more = len(att) - len(shown)
        text += " — with " + ", ".join(shown) + (f" +{more} more" if more else "")
    text += f" ({app}:{cal})."
    if notes:
        text += f" Notes: {notes}"

    cid = "calendar:" + hashlib.sha256(
        f"{app}:{uid}:{start_key}".encode()).hexdigest()[:24]
    meta = {"app": app, "account": account, "calendar": cal, "uid": uid,
            "start": meta_start, "end": meta_end, "all_day": bool(all_day),
            "attendees": att}
    if repeats:
        meta["repeats"] = repeats
    return cid, text, {"source": "calendar", "timestamp": ts,
                       "location": f"{app}:{cal}", "meta": meta}

def iter_chunks():
    apps, excludes = _settings()
    if not apps:
        return
    unknown = [a for a in apps if a not in _READERS]
    if unknown:
        raise ValueError(f"calendar: unknown app(s) {unknown}; "
                         f"known: {', '.join(_READERS)}")
    kept, failures, yielded_apps = [], [], []
    seen = set()                       # (uid, start): first-configured wins
    for app in apps:
        try:
            events = _READERS[app](excludes)
        except (OSError, sqlite3.Error) as e:
            failures.append(f"{app}: {e}")
            continue
        if events:
            yielded_apps.append(app)
        for ev in events:
            key = (ev[0], ev[1])
            if key not in seen:
                seen.add(key)
                kept.append((app, ev))
    if failures:
        # Partial failure must fail the source (prune safety); total failure
        # is the quiet no-op (store unreadable until FDA is granted).
        if yielded_apps:
            raise RuntimeError("calendar: " + "; ".join(failures))
        print(f"calendar: skipping ({'; '.join(failures)})", file=sys.stderr)
        return
    for app, ev in kept:
        chunk = _chunk(app, ev)
        if chunk:
            yield chunk
