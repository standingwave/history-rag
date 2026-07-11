"""Calendar source against a fixture Calendar.sqlitedb (the confirmed schema
subset). Conventions pinned here were verified against the live store:
Apple-epoch dates, all-day masters at UTC midnight vs. cache occurrences at
LOCAL midnight, accounts as Store rows. TZ is America/Los_Angeles (conftest).
"""
import sqlite3
from datetime import datetime, timedelta, timezone
import pytest
import sources.calendar as cal

APPLE_EPOCH = 978307200

SCHEMA = """
CREATE TABLE Store (ROWID INTEGER PRIMARY KEY, name TEXT, disabled INTEGER);
CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, store_id INTEGER, title TEXT);
CREATE TABLE CalendarItem (ROWID INTEGER PRIMARY KEY, summary TEXT,
  description TEXT, start_date REAL, end_date REAL, all_day INTEGER,
  calendar_id INTEGER, unique_identifier TEXT, location_id INTEGER,
  has_recurrences INTEGER, hidden INTEGER);
CREATE TABLE OccurrenceCache (event_id INTEGER, occurrence_date REAL,
  occurrence_end_date REAL, day REAL);
CREATE TABLE Participant (owner_id INTEGER, identity_id INTEGER, email TEXT);
CREATE TABLE Identity (ROWID INTEGER PRIMARY KEY, display_name TEXT,
  first_name TEXT, last_name TEXT);
CREATE TABLE Recurrence (owner_id INTEGER, frequency INTEGER, interval INTEGER);
CREATE TABLE Location (ROWID INTEGER PRIMARY KEY, title TEXT);
"""

def apple_ts(iso: str) -> float:
    """UTC ISO datetime -> Apple-epoch seconds."""
    return datetime.fromisoformat(iso).timestamp() - APPLE_EPOCH

@pytest.fixture
def store(tmp_path, monkeypatch):
    """Fixture store with one account ('iCloud'), one Work calendar, and
    [calendar].apps = ['apple'] configured; tests add items."""
    path = str(tmp_path / "Calendar.sqlitedb")
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO Store VALUES (1, 'iCloud', 0)")
    db.execute("INSERT INTO Calendar VALUES (1, 1, 'Work')")
    db.commit()
    monkeypatch.setattr(cal, "APPLE_DB", path)
    import config
    monkeypatch.setattr(config, "_FILE", {"calendar": {"apps": ["apple"]}})
    return db

def add_event(db, rowid, title, start_iso, minutes=30, cal_id=1, notes=None,
              all_day=0, recurs=0, uid=None, location_id=None):
    start = apple_ts(start_iso)
    db.execute("INSERT INTO CalendarItem VALUES (?,?,?,?,?,?,?,?,?,?,0)",
               (rowid, title, notes, start, start + minutes * 60, all_day,
                cal_id, uid or f"UID-{rowid}", location_id, recurs))
    db.commit()

def chunks():
    return list(cal.iter_chunks())

# ── time handling ────────────────────────────────────────────────────────────

def test_timed_event_epoch_and_text(store):
    add_event(store, 1, "Standup", "2026-07-09T17:00:00+00:00")  # 10:00 PDT
    (cid, text, rec), = chunks()
    assert rec["timestamp"] == "2026-07-09T17:00:00+00:00"
    assert "Calendar event on 2026-07-09 (Thursday) 10:00–10:30: Standup" in text
    assert text.endswith("(apple:Work).")
    assert rec["location"] == "apple:Work"
    assert rec["meta"]["account"] == "iCloud"
    assert cid.startswith("calendar:")

def test_all_day_master_utc_midnight_to_local_date(store):
    # all-day masters store UTC midnight of the DATE; the local day would be
    # the day before in LA if read as an instant
    add_event(store, 1, "Offsite", "2026-07-09T00:00:00+00:00", all_day=1)
    (_, text, rec), = chunks()
    assert "on 2026-07-09 (Thursday), all day: Offsite" in text
    # stamped local-midnight-in-UTC (appusage convention): 07:00Z in PDT
    assert rec["timestamp"] == "2026-07-09T07:00:00+00:00"
    assert rec["meta"]["start"] == "2026-07-09"

def test_future_days_cap(store):
    soon = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(days=200)).isoformat()
    add_event(store, 1, "Soon", soon)
    add_event(store, 2, "Far", far)
    assert [c[2]["meta"]["uid"] for c in chunks()] == ["UID-1"]

# ── joins ────────────────────────────────────────────────────────────────────

def test_attendees_names_never_emails(store):
    add_event(store, 1, "Sync", "2026-07-09T17:00:00+00:00")
    store.execute("INSERT INTO Identity VALUES (1, 'Alice Smith', NULL, NULL)")
    store.execute("INSERT INTO Identity VALUES (2, 'bob@corp.com', NULL, NULL)")
    store.execute("INSERT INTO Participant VALUES (1, 1, NULL)")
    store.execute("INSERT INTO Participant VALUES (1, 2, 'bob@corp.com')")
    store.commit()
    (_, text, rec), = chunks()
    assert "with Alice Smith, bob" in text
    assert "@" not in text
    assert rec["meta"]["attendees"] == ["Alice Smith", "bob"]

def test_room_join_and_account_disabled_store_skip(store):
    store.execute("INSERT INTO Store VALUES (2, 'Google', 1)")     # disabled
    store.execute("INSERT INTO Calendar VALUES (2, 2, 'Gcal')")
    store.execute("INSERT INTO Location VALUES (1, 'HQ 4th floor')")
    store.commit()
    add_event(store, 1, "Review", "2026-07-09T17:00:00+00:00", location_id=1)
    add_event(store, 2, "Ghost", "2026-07-09T18:00:00+00:00", cal_id=2)
    (_, text, _), = chunks()                  # disabled store's event dropped
    assert "Review at HQ 4th floor" in text

def test_calendar_excludes_default_and_user_merge(store, monkeypatch):
    import config
    store.execute("INSERT INTO Calendar VALUES (3, 1, 'US Holidays')")
    store.execute("INSERT INTO Calendar VALUES (4, 1, 'Scratch')")
    store.commit()
    add_event(store, 1, "Kept", "2026-07-09T17:00:00+00:00")
    add_event(store, 2, "Default-excluded", "2026-07-09T17:00:00+00:00", cal_id=3)
    add_event(store, 3, "User-excluded", "2026-07-09T17:00:00+00:00", cal_id=4)
    monkeypatch.setattr(config, "_FILE", {"calendar": {
        "apps": ["apple"], "exclude_calendars": ["Scratch"]}})
    assert [c[2]["meta"]["uid"] for c in chunks()] == ["UID-1"]

# ── recurrence ───────────────────────────────────────────────────────────────

def test_occurrence_expansion_from_cache(store):
    add_event(store, 1, "Retro", "2026-01-05T18:00:00+00:00", recurs=1)
    for iso in ("2026-01-05T18:00:00+00:00", "2026-01-12T18:00:00+00:00"):
        s = apple_ts(iso)
        store.execute("INSERT INTO OccurrenceCache VALUES (1, ?, ?, ?)",
                      (s, s + 1800, s))
    store.commit()
    got = chunks()
    assert len(got) == 2
    assert {c[2]["timestamp"] for c in got} == {
        "2026-01-05T18:00:00+00:00", "2026-01-12T18:00:00+00:00"}
    assert len({c[0] for c in got}) == 2      # occurrence ids differ

def test_series_master_fallback_without_cache_rows(store):
    add_event(store, 1, "Payday", "2020-01-01T18:00:00+00:00", recurs=1)
    store.execute("INSERT INTO Recurrence VALUES (1, 3, 1)")
    store.commit()
    (_, text, rec), = chunks()
    assert text.startswith("Recurring calendar event (monthly) starting "
                           "2020-01-01")
    assert rec["meta"]["repeats"] == "monthly"

def test_all_day_occurrence_local_midnight_to_local_date(store):
    # cache stores LOCAL midnight for all-day occurrences (verified live):
    # 2026-07-09 00:00 PDT == 07:00Z — the UTC date is the same day here,
    # but reading it as UTC-midnight-of-date would be wrong for other zones
    add_event(store, 1, "Holiday", "2026-07-09T00:00:00+00:00",
              all_day=1, recurs=1)
    s = apple_ts("2026-07-09T07:00:00+00:00")
    store.execute("INSERT INTO OccurrenceCache VALUES (1, ?, ?, ?)",
                  (s, s + 86399, s))
    store.commit()
    (_, text, rec), = chunks()
    assert "on 2026-07-09 (Thursday), all day" in text
    assert rec["meta"]["start"] == "2026-07-09"

# ── filtering ────────────────────────────────────────────────────────────────

def test_secret_notes_drop_field_not_event(store):
    add_event(store, 1, "Zoom sync", "2026-07-09T17:00:00+00:00",
              notes="join here, password: hunter2")
    (_, text, _), = chunks()                  # event survives, notes gone
    assert "Zoom sync" in text
    assert "Notes:" not in text and "hunter2" not in text

def test_secret_title_drops_chunk(store):
    add_event(store, 1, "share api_key sk-abcdefghijklmnop1234",
              "2026-07-09T17:00:00+00:00")
    assert chunks() == []

def test_notes_truncated(store):
    add_event(store, 1, "Planning", "2026-07-09T17:00:00+00:00",
              notes="agenda " * 100)
    (_, text, _), = chunks()
    i = text.index("Notes: ")
    assert len(text[i + len("Notes: "):]) <= cal.NOTES_MAX

# ── contract: config gate, dedup, ids, failure ───────────────────────────────

def test_no_config_or_empty_apps_yields_nothing(store, monkeypatch):
    import config
    add_event(store, 1, "Standup", "2026-07-09T17:00:00+00:00")
    monkeypatch.setattr(config, "_FILE", {})
    assert chunks() == []

def test_unknown_app_raises(store, monkeypatch):
    import config
    monkeypatch.setattr(config, "_FILE", {"calendar": {"apps": ["outlook"]}})
    with pytest.raises(ValueError, match="unknown app"):
        chunks()

def test_chunk_ids_stable_across_runs(store):
    add_event(store, 1, "Standup", "2026-07-09T17:00:00+00:00")
    assert {c[0] for c in chunks()} == {c[0] for c in chunks()}

def fake_reader(events, fail=False):
    def read(excludes):
        if fail:
            raise sqlite3.OperationalError("locked")
        return events
    return read

EV = ("UID-X", apple_ts("2026-07-09T17:00:00+00:00") + APPLE_EPOCH,
      apple_ts("2026-07-09T17:30:00+00:00") + APPLE_EPOCH,
      False, "Standup", "Work", "acct", [], "", "", None)

def test_cross_app_dedup_first_configured_wins(store, monkeypatch):
    import config
    monkeypatch.setattr(cal, "_READERS",
                        {"a": fake_reader([EV]), "b": fake_reader([EV])})
    monkeypatch.setattr(config, "_FILE", {"calendar": {"apps": ["b", "a"]}})
    got = chunks()
    assert len(got) == 1 and got[0][2]["meta"]["app"] == "b"

def test_partial_failure_raises_total_failure_noops(store, monkeypatch, capsys):
    import config
    monkeypatch.setattr(config, "_FILE", {"calendar": {"apps": ["a", "b"]}})
    # one app yields, the other errors -> raise (prune must not run)
    monkeypatch.setattr(cal, "_READERS",
                        {"a": fake_reader([EV]), "b": fake_reader([], fail=True)})
    with pytest.raises(RuntimeError, match="locked"):
        chunks()
    # every app fails -> quiet no-op (store unreadable until FDA granted)
    monkeypatch.setattr(cal, "_READERS",
                        {"a": fake_reader([], fail=True),
                         "b": fake_reader([], fail=True)})
    assert chunks() == []
    assert "calendar: skipping" in capsys.readouterr().err

def test_missing_store_yields_nothing(store, monkeypatch):
    monkeypatch.setattr(cal, "APPLE_DB", "/nonexistent/Calendar.sqlitedb")
    add_event(store, 1, "Standup", "2026-07-09T17:00:00+00:00")
    assert chunks() == []
