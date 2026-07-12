"""Daily digest source: per-stream rollup content, day attribution, the
recompute/backfill window, and pipeline determinism (no spurious re-embeds).
TZ is pinned to America/Los_Angeles; fixture days use 2026-07-01/02 (local)."""
import json, sqlite3
from datetime import date, datetime, timedelta
import pytest
from sources import browser, digest, shell
from tests.helpers import mk_source, rec, run_index, open_db

def _local_epoch(iso_naive: str) -> float:
    return datetime.fromisoformat(iso_naive).timestamp()

@pytest.fixture
def no_real_stores(monkeypatch):
    """Digest reads live machine stores; point every one at nothing so tests
    never touch real browser/shell history."""
    monkeypatch.setenv("CLAUDE_RAG_BROWSERS", "none=/nonexistent/History")
    monkeypatch.setattr(shell, "_history_files", lambda: ([], []))
    monkeypatch.setattr(shell, "_read_atuin", lambda: iter([]))

# ── browser stream ───────────────────────────────────────────────────────────

def _chromium_db(path, visits):
    """visits: [(url, title, local_iso)] -> a minimal Chromium History DB."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, "
               "title TEXT, visit_count INTEGER, last_visit_time INTEGER)")
    db.execute("CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, "
               "visit_time INTEGER)")
    ids = {}
    for url, title, local_iso in visits:
        if url not in ids:
            db.execute("INSERT INTO urls(url, title, visit_count, "
                       "last_visit_time) VALUES (?,?,1,0)", (url, title))
            ids[url] = db.execute("SELECT MAX(id) FROM urls").fetchone()[0]
        wk = int((_local_epoch(local_iso) + browser._WEBKIT_TO_UNIX) * 1e6)
        db.execute("INSERT INTO visits(url, visit_time) VALUES (?,?)",
                   (ids[url], wk))
    db.commit()
    db.close()

@pytest.fixture
def browser_fixture(monkeypatch, tmp_path):
    prof = tmp_path / "prof"
    prof.mkdir()
    _chromium_db(str(prof / "History"), [
        ("https://www.youtube.com/watch?v=abc", "A video", "2026-07-02T10:00:00"),
        ("https://www.youtube.com/watch?v=abc", "A video", "2026-07-02T11:00:00"),
        ("https://news.site.com/story", "Big story", "2026-07-02T12:00:00"),
        ("https://www.google.com/search?q=impending+doom", "q - Google",
         "2026-07-02T13:00:00"),
        # 23:30 local on 07-01 is 06:30 UTC on 07-02 — must land on 07-01
        ("https://news.site.com/story", "Big story", "2026-07-01T23:30:00"),
    ])
    monkeypatch.setenv("CLAUDE_RAG_BROWSERS", f"testb={prof / 'History'}")
    browser._keep_table = {**browser._DEFAULT_KEEP_PARAMS,
                           "google.com/search": ["q"]}

def test_browser_digest_content_and_day_attribution(browser_fixture):
    chunks = {c[2]["meta"]["date"]: c
              for c in digest._browser_chunks(["2026-07-01", "2026-07-02"])}
    assert set(chunks) == {"2026-07-01", "2026-07-02"}

    cid, text, r = chunks["2026-07-02"]
    assert cid.startswith("digest:")
    assert r["source"] == "digest" and r["location"] == "testb:prof"
    assert text.startswith("Browser digest for 2026-07-02 (Thursday), "
                           "testb:prof: 4 visits across 3 sites.")
    assert "youtube.com (2)" in text
    assert 'Searched google.com for "impending doom"' in text
    assert '"A video"' in text and '"Big story"' in text
    assert r["meta"]["domains"]["youtube.com"] == 2
    assert r["meta"]["searches"] == [{"engine": "google.com",
                                      "terms": "impending doom"}]
    # local-midnight-UTC timestamp (appusage convention): 07:00Z in PDT
    assert r["timestamp"] == "2026-07-02T07:00:00+00:00"

    _, text1, r1 = chunks["2026-07-01"]   # the 23:30 boundary visit
    assert "1 visits across 1 sites" in text1
    assert r1["meta"]["domains"] == {"news.site.com": 1}

def _safari_db(path, visits):
    """visits: [(url, title, local_iso)] -> a minimal Safari History.db."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE history_items(id INTEGER PRIMARY KEY, url TEXT)")
    db.execute("CREATE TABLE history_visits(id INTEGER PRIMARY KEY, "
               "history_item INTEGER, visit_time REAL, title TEXT)")
    ids = {}
    for url, title, local_iso in visits:
        if url not in ids:
            db.execute("INSERT INTO history_items(url) VALUES (?)", (url,))
            ids[url] = db.execute("SELECT MAX(id) FROM history_items").fetchone()[0]
        cf = _local_epoch(local_iso) - browser._CFABSOLUTE_TO_UNIX
        db.execute("INSERT INTO history_visits(history_item, visit_time, title) "
                   "VALUES (?,?,?)", (ids[url], cf, title))
    db.commit()
    db.close()

def test_safari_visit_reader_schema_and_epochs(monkeypatch, tmp_path):
    """The Safari-schema branch of iter_visits: table sniff, CFAbsolute epoch
    conversion, the SQL-side since bound, and the profile-less location."""
    saf = tmp_path / "Safari"
    saf.mkdir()
    _safari_db(str(saf / "History.db"), [
        ("https://news.site.com/story", "Big story", "2026-07-02T12:00:00"),
        ("https://news.site.com/story", "Big story", "2026-07-01T23:30:00"),
        ("https://other.example.com/x", "Other", "2026-06-20T10:00:00"),
    ])
    monkeypatch.setenv("CLAUDE_RAG_BROWSERS", f"safari={saf / 'History.db'}")

    got = list(browser.iter_visits(_local_epoch("2026-07-01T00:00:00")))
    assert got == [   # June visit excluded in SQL; parent dir "Safari" -> no profile
        ("safari", _local_epoch("2026-07-02T12:00:00"),
         "https://news.site.com/story", "Big story"),
        ("safari", _local_epoch("2026-07-01T23:30:00"),
         "https://news.site.com/story", "Big story"),
    ]

    chunks = {c[2]["meta"]["date"]: c
              for c in digest._browser_chunks(["2026-07-01", "2026-07-02"])}
    assert set(chunks) == {"2026-07-01", "2026-07-02"}   # one visit each day
    assert chunks["2026-07-02"][2]["location"] == "safari"
    assert chunks["2026-07-02"][2]["meta"]["domains"] == {"news.site.com": 1}

def test_browser_digest_deterministic(browser_fixture):
    days = ["2026-07-01", "2026-07-02"]
    assert list(digest._browser_chunks(days)) == list(digest._browser_chunks(days))

def test_browser_digest_text_cap(monkeypatch, tmp_path):
    prof = tmp_path / "p2"
    prof.mkdir()
    _chromium_db(str(prof / "History"),
                 [(f"https://domain-{i:03}.example.com/x", f"Title {i} " * 20,
                   "2026-07-02T10:00:00") for i in range(300)])
    monkeypatch.setenv("CLAUDE_RAG_BROWSERS", f"big={prof / 'History'}")
    (_, text, r), = digest._browser_chunks(["2026-07-02"])
    assert len(text) <= digest.MAX_TEXT
    assert len(r["meta"]["domains"]) == digest.META_DOMAINS

# ── claude stream (reads the index) ──────────────────────────────────────────

@pytest.fixture
def seeded_claude(scratch_db, fake_embed, monkeypatch):
    def turn(cid, text, ts, sid, lineno, role):
        return (cid, text, rec("claude", ts=ts, loc="/dev/proj",
                               meta={"session_id": sid, "lineno": lineno,
                                     "role": role}))
    run_index(monkeypatch, [mk_source("claude", [
        turn("c1", "how should the digest source resume after gaps",
             "2026-07-02T17:00:00Z", "s1", 1, "user"),
        turn("c2", "a reply describing the resume strategy in detail",
             "2026-07-02T17:05:00Z", "s1", 2, "assistant"),
        turn("c3", "unrelated question about group_by bucketing rules",
             "2026-07-02T18:00:00Z", "s2", 1, "user"),
        turn("c4", "a next-day follow-up prompt in the same session",
             "2026-07-03T17:00:00Z", "s1", 9, "user"),
    ])])
    return scratch_db

def test_claude_digest_sessions_topics_and_days(seeded_claude):
    chunks = {c[2]["meta"]["date"]: c
              for c in digest._claude_chunks(["2026-07-02", "2026-07-03"])}
    _, text, r = chunks["2026-07-02"]
    assert text.startswith("Claude digest for 2026-07-02 (Thursday): "
                           "2 sessions, 3 turns, in /dev/proj.")
    assert '"how should the digest source resume after gaps"' in text
    assert '"unrelated question about group_by bucketing rules"' in text
    assert r["location"] == "claude"
    assert r["meta"]["total_turns"] == 3
    assert [s["turns"] for s in r["meta"]["sessions"]] == [2, 1]

    # the same session appears again on the day of its later turns
    _, text3, r3 = chunks["2026-07-03"]
    assert "1 session, 1 turns" in text3
    assert '"a next-day follow-up prompt in the same session"' in text3

def test_claude_digest_no_index_is_noop(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "absent.db"))
    assert list(digest._claude_chunks(["2026-07-02"])) == []

# ── shell stream ─────────────────────────────────────────────────────────────

def test_shell_digest_counts_runs_by_day(monkeypatch):
    e1 = _local_epoch("2026-07-02T09:00:00")
    monkeypatch.setattr(shell, "_read_atuin", lambda: iter([
        (e1, "python index.py", "/Users/u/dev/claude", 0),
        (e1 + 60, "python index.py", "/Users/u/dev/claude", 0),
        (e1 + 120, "pytest -q", "/Users/u/dev/claude", 0),
        (_local_epoch("2026-07-03T09:00:00"), "git push", "/Users/u/dev/x", 0),
    ]))
    monkeypatch.setattr(shell, "_history_files", lambda: ([], []))
    chunks = {c[2]["meta"]["date"]: c
              for c in digest._shell_chunks(["2026-07-02", "2026-07-03"])}
    _, text, r = chunks["2026-07-02"]
    assert text.startswith("Shell digest for 2026-07-02 (Thursday): "
                           "3 commands, mostly in /Users/u/dev/claude (3).")
    assert '"python index.py" (x2)' in text
    assert r["meta"]["runs"] == 3
    assert "1 command," in chunks["2026-07-03"][1]

def test_iter_dated_runs_atuin_dedup_and_epoch_filter(monkeypatch, tmp_path):
    live = tmp_path / "zh"
    live.write_text(": 1751400000:0;git status --short\n"
                    ": 1751400060:0;make test\n"
                    ": 1000:0;ancient command here\n")
    monkeypatch.setattr(shell, "_history_files", lambda: ([str(live)], []))
    monkeypatch.setattr(shell, "_read_atuin", lambda: iter([
        (1751400030.0, "git status --short", "/Users/u/dev", 0),
    ]))
    got = list(shell.iter_dated_runs(1751000000))
    # atuin's run kept (with cwd); its command's histfile copy skipped;
    # the pre-window entry dropped
    assert got == [(1751400030.0, "git status --short", "/Users/u/dev"),
                   (1751400060, "make test", "")]

# ── window selection and pipeline behavior ───────────────────────────────────

def test_days_backfill_resume_and_recompute(scratch_db, fake_embed, monkeypatch):
    # fresh index -> bounded backfill, ending today
    days = digest._days_to_digest(3, 90)
    assert len(days) == 90 and days[-1] == date.today().isoformat()

    # a stored digest 5 days back -> resume the gap from the day after it
    run_index(monkeypatch, [mk_source("alpha", [("a1", "seed", rec("alpha"))])])
    db = open_db(scratch_db)
    stored = (date.today() - timedelta(days=5)).isoformat()
    db.execute("INSERT INTO chunks VALUES ('digest:x','t','digest','','shell',?)",
               (json.dumps({"date": stored}),))
    db.commit()
    days = digest._days_to_digest(3, 90)
    assert len(days) == 5 and days[-1] == date.today().isoformat()

    # caught up -> just the recompute window
    db.execute("UPDATE chunks SET meta=? WHERE id='digest:x'",
               (json.dumps({"date": date.today().isoformat()}),))
    db.commit()
    assert len(digest._days_to_digest(3, 90)) == 3

def test_pipeline_settles_with_no_reembeds(scratch_db, fake_embed, monkeypatch,
                                           no_real_stores, tmp_path):
    prof = tmp_path / "prof"
    prof.mkdir()
    recent = (date.today() - timedelta(days=1)).isoformat()
    _chromium_db(str(prof / "History"),
                 [("https://a.example.com/x", "A page", f"{recent}T10:00:00")])
    monkeypatch.setenv("CLAUDE_RAG_BROWSERS", f"testb={prof / 'History'}")

    run_index(monkeypatch, [digest])
    db = open_db(scratch_db)
    texts = [r[0] for r in db.execute(
        "SELECT text FROM chunks WHERE source='digest'")]
    assert len(texts) == 1 and f"Browser digest for {recent}" in texts[0]

    n = len(fake_embed.calls)
    run_index(monkeypatch, [digest])     # same backing data -> zero re-embeds
    assert len(fake_embed.calls) == n

def test_prune_digest_refused(monkeypatch, capsys):
    import index
    monkeypatch.setattr("sys.argv",
                        ["index.py", "--prune", "--source", "digest"])
    with pytest.raises(SystemExit, match="settled digest"):
        index.main()
