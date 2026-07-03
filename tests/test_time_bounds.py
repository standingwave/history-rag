"""Time-window logic. This exact logic once misattributed a whole evening
(the Castle | Disney+ bug): local days must convert to UTC correctly,
including DST differences. TZ is pinned to America/Los_Angeles in conftest."""
from server import _bound_to_utc, _parse_bounds, _window_where, _loc_prefix

def test_date_only_since_is_local_midnight_utc():
    assert _bound_to_utc("2026-07-02") == "2026-07-02T07:00:00+00:00"   # PDT
    assert _bound_to_utc("2026-01-15") == "2026-01-15T08:00:00+00:00"   # PST

def test_date_only_until_covers_whole_local_day():
    assert _bound_to_utc("2026-07-02", end_of_day=True) == \
        "2026-07-03T06:59:59.999999+00:00"

def test_castle_regression_evening_visit_in_local_day():
    """17:11 PDT on Jul 1 is Jul 2 in UTC but must fall inside local Jul 1."""
    since = _bound_to_utc("2026-07-01")
    until = _bound_to_utc("2026-07-01", end_of_day=True)
    visit = "2026-07-02T00:11:00+00:00"
    assert since <= visit <= until

def test_datetime_bounds():
    assert _bound_to_utc("2026-07-02T12:00:00") == \
        "2026-07-02T19:00:00+00:00"                      # naive = local
    assert _bound_to_utc("2026-07-02T12:00:00Z") == \
        "2026-07-02T12:00:00+00:00"                      # Z passthrough
    assert _bound_to_utc("2026-07-02T12:00:00-04:00") == \
        "2026-07-02T16:00:00+00:00"                      # offset converted

def test_parse_bounds_error_shape():
    s, u, err = _parse_bounds("not-a-date", "")
    assert err and "bad since/until" in err
    s, u, err = _parse_bounds("2026-07-02", "2026-07-02")
    assert err is None and s < u

def test_window_where_composition():
    sql, params = _window_where("S", "U", "git", "littlebird@", False)
    assert "timestamp >= ?" in sql and "timestamp <= ?" in sql
    assert "timestamp != ''" in sql
    assert "source = ?" in sql and "substr(location, 1, ?)" in sql
    assert params == ["S", "U", "git", 11, "littlebird@"]

def test_window_where_include_undated():
    sql, _ = _window_where("S", "", "", "", True)
    assert "timestamp = '' OR" in sql

def test_window_where_no_bounds_no_undated_clause():
    sql, params = _window_where("", "", "shell", "", False)
    assert sql.startswith("1=1") and "timestamp !=" not in sql
    assert params == ["shell"]

def test_loc_prefix_collapsing():
    assert _loc_prefix("git", "littlebird@84af2c65") == "littlebird@"
    assert _loc_prefix("obsidian", "projects/foo.md#Plan") == "projects/"
    assert _loc_prefix("obsidian", "note.md#H") == "note.md"
    assert _loc_prefix("browser", "chrome:First user") == "chrome:First user"
