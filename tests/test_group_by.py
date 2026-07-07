"""list_window group_by: bucket counts, local-day bucketing, domain fallback,
undated gating, limit clamp, dimension validation. TZ pinned to
America/Los_Angeles, so 2026-07-03T05:00Z is still local 2026-07-02."""
import json
import pytest
from tests.test_driver import mk_source, rec, run_index

def _b(i, ts, url):
    return (f"b{i}", f"page {i} — {url}",
            rec("browser", ts=ts, loc="safari", meta={"url": url}))

@pytest.fixture
def seeded(scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [
        mk_source("browser", [
            _b(1, "2026-07-02T17:00:00+00:00", "https://www.youtube.com/watch?v=a"),
            _b(2, "2026-07-02T18:00:00+00:00", "https://youtube.com/watch?v=b"),
            _b(3, "2026-07-03T05:00:00+00:00", "https://news.site.com/x"),  # local 07-02
            _b(4, "2026-07-03T18:00:00+00:00", "https://news.site.com/y"),
        ]),
        mk_source("claude", [
            ("c1", "a conversation turn about digests, long enough to matter",
             rec("claude", ts="2026-07-02T17:30:00+00:00", loc="/dev/proj")),
        ]),
        mk_source("shell", [
            ("s1", "git log --oneline", rec("shell", ts="", loc="hist")),
        ]),
    ])
    import server
    return server

def test_group_by_day_uses_local_days(seeded):
    r = json.loads(seeded.list_window(since="2026-07-01", group_by="day"))
    assert r["group_by"] == ["day"]
    assert r["total"] == 5
    days = {g["day"]: g["count"] for g in r["groups"]}
    assert days == {"2026-07-02": 4, "2026-07-03": 1}    # b3 lands on 07-02
    top = r["groups"][0]
    assert top["count"] == 4
    assert top["earliest"] == "2026-07-02T17:00:00+00:00"
    assert top["latest"] == "2026-07-03T05:00:00+00:00"
    assert "results" not in r

def test_group_by_domain_and_fallback(seeded):
    r = json.loads(seeded.list_window(since="2026-07-01", group_by="domain"))
    got = {g["domain"]: g["count"] for g in r["groups"]}
    # www-stripped browser hosts; claude falls back to its location
    assert got == {"youtube.com": 2, "news.site.com": 2, "/dev/proj": 1}

def test_group_by_multi_dimension(seeded):
    r = json.loads(seeded.list_window(since="2026-07-01",
                                      group_by="day,source"))
    key = {(g["day"], g["source"]): g["count"] for g in r["groups"]}
    assert key == {("2026-07-02", "browser"): 3,
                   ("2026-07-02", "claude"): 1,
                   ("2026-07-03", "browser"): 1}

def test_group_by_undated_gating(seeded):
    r = json.loads(seeded.list_window(since="2026-07-01", group_by="source"))
    assert "shell" not in {g["source"] for g in r["groups"]}
    r = json.loads(seeded.list_window(since="2026-07-01", group_by="source",
                                      include_undated=True))
    sh = [g for g in r["groups"] if g["source"] == "shell"]
    assert sh and sh[0]["count"] == 1
    assert sh[0]["earliest"] is None and sh[0]["latest"] is None

def test_group_by_limit_and_truncation_flag(seeded):
    r = json.loads(seeded.list_window(since="2026-07-01", group_by="domain",
                                      limit=2))
    assert len(r["groups"]) == 2 and r["groups_truncated"] is True
    r = json.loads(seeded.list_window(since="2026-07-01", group_by="domain"))
    assert "groups_truncated" not in r

def test_group_by_validation_and_bounds(seeded):
    r = json.loads(seeded.list_window(since="2026-07-01", group_by="week"))
    assert "unknown group_by dimension" in r["error"]
    r = json.loads(seeded.list_window(group_by="day"))
    assert "error" in r                                  # bound still required

def test_group_by_respects_filters(seeded):
    r = json.loads(seeded.list_window(since="2026-07-01", source="browser",
                                      location="safari", group_by="day"))
    assert r["total"] == 4
    assert sum(g["count"] for g in r["groups"]) == 4
