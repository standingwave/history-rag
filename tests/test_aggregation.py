"""App-usage aggregation: midnight attribution, duration formatting, and the
source's MIN_SECONDS floor + local-midnight-UTC stamping."""
import datetime, sqlite3
from appusage import store
from sources import appusage as appusage_src

def test_daily_durations_midnight_attribution():
    db = sqlite3.connect(":memory:")
    store.setup(db)
    start = datetime.datetime(2025, 1, 6, 23, 59).timestamp()   # local
    db.execute("INSERT INTO segments(app, start_ts, end_ts, closed) VALUES (?,?,?,1)",
               ("Figma", start, start + 31 * 60))               # spans midnight
    days = store.daily_durations(db)
    assert list(days) == ["2025-01-06"]                         # start day owns it
    assert days["2025-01-06"]["Figma"] == 31 * 60
    # zero/negative-length segments are ignored
    db.execute("INSERT INTO segments(app, start_ts, end_ts, closed) VALUES (?,?,?,1)",
               ("Ghost", start, start))
    assert "Ghost" not in store.daily_durations(db)["2025-01-06"]

def test_fmt_duration():
    assert store.fmt_duration(45) == "0m"
    assert store.fmt_duration(5 * 60) == "5m"
    assert store.fmt_duration(2 * 3600 + 4 * 60) == "2h 4m"

def test_source_min_seconds_floor_and_utc_midnight_stamp():
    db = store.connect()                     # frozen at the conftest tmp path
    store.setup(db)
    day = datetime.datetime(2025, 1, 6, 10, 0).timestamp()      # local, PST
    db.execute("INSERT INTO segments(app, start_ts, end_ts, closed) VALUES (?,?,?,1)",
               ("TinyApp", day, day + 30))                      # under the floor
    db.execute("INSERT INTO segments(app, start_ts, end_ts, closed) VALUES (?,?,?,1)",
               ("BigApp", day, day + 124))
    db.commit()
    try:
        chunks = [(t, r) for _, t, r in appusage_src.iter_chunks()
                  if r["meta"]["date"] == "2025-01-06"]
        assert len(chunks) == 1                                 # TinyApp filtered
        text, rec = chunks[0]
        assert "2m in BigApp" in text and "(Monday)" in text
        assert rec["timestamp"] == "2025-01-06T08:00:00+00:00"  # PST midnight
    finally:
        db.execute("DELETE FROM segments")
        db.commit()
