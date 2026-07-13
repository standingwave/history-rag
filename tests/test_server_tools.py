"""Tier 2: server tool envelopes and expand context shapes, on a seeded
scratch index with the fake embedder. Timestamps are UTC; TZ is pinned to
America/Los_Angeles, so local day 2026-07-02 = [07-02T07:00Z, 07-03T07:00Z)."""
import json, os, subprocess
import pytest
from tests.helpers import mk_source, rec, run_index

D = "2026-07-02T"   # inside local 2026-07-02

def seed(monkeypatch, chunks_by_source):
    sources = [mk_source(name, chunks) for name, chunks in chunks_by_source.items()]
    run_index(monkeypatch, sources)

@pytest.fixture
def seeded(scratch_db, fake_embed, monkeypatch):
    seed(monkeypatch, {
        "browser": [
            (f"b{i}", f"page number {i} about topic — https://x.com/{i}",
             rec("browser", ts=f"{D}1{i}:00:00+00:00", loc="safari"))
            for i in range(4)
        ],
        "claude": [
            ("cl1", "a question about the indexing approach we chose",
             rec("claude", ts=f"{D}15:00:00+00:00", loc="/proj",
                 meta={"session_id": "sess1", "project_hash": "nope", "lineno": 5})),
        ],
        "shell": [
            ("sh1", "git log --oneline", rec("shell", ts="", loc="hist")),
        ],
    })
    return scratch_db

def test_search_envelope_and_ids(seeded):
    import server
    r = json.loads(server.search_history("page about topic", k=2))
    assert r["count"] == 2 and "window" not in r
    top = r["results"][0]
    assert set(top) >= {"rank", "id", "source", "distance", "text"}
    assert top["rank"] == 1

def test_exact_vs_pool_branch(seeded, monkeypatch):
    import server
    r = json.loads(server.search_history("page", k=2, since="2026-07-02"))
    assert r.get("exact") is True
    assert r["window"]["since"] == "2026-07-02T07:00:00+00:00"

    monkeypatch.setattr(server, "EXACT_WINDOW_MAX", 1)   # force pool path
    r = json.loads(server.search_history("page", k=50, since="2026-07-02"))
    assert "exact" not in r
    assert "note" in r                                   # short of k, sampled

def test_source_only_filter_ranks_exhaustively(seeded):
    """A small source must be fully ranked even without a time window —
    the git-scoped zero-results regression."""
    import server
    r = json.loads(server.search_history("question about indexing", k=2,
                                         source="claude"))
    assert r.get("exact") is True and "window" not in r
    assert r["count"] == 1 and r["results"][0]["id"] == "cl1"

def test_undated_rows_and_window(seeded):
    import server
    r = json.loads(server.search_history("git log", k=5, source="shell",
                                         since="2026-07-02"))
    assert r["count"] == 0                               # undated excluded
    r = json.loads(server.search_history("git log", k=5, source="shell",
                                         since="2026-07-02", include_undated=True))
    assert r["count"] == 1

def test_list_window_paging_and_truncation(seeded, monkeypatch):
    import server
    r = json.loads(server.list_window())
    assert "error" in r                                  # bound required
    r = json.loads(server.list_window(since="2026-07-02", limit=2))
    assert r["total"] == 5 and r["count"] == 2           # 4 browser + 1 claude
    r2 = json.loads(server.list_window(since="2026-07-02", limit=2, offset=4))
    assert r2["count"] == 1
    seed(monkeypatch, {"browser": [
        ("big", "x" * 500, rec("browser", ts=f"{D}18:00:00+00:00", loc="safari"))]})
    r = json.loads(server.list_window(since="2026-07-02", source="browser"))
    big = [x for x in r["results"] if x["id"] == "big"][0]
    assert len(big["text"]) == 161 and big["text"].endswith("…")

def test_expand_claude_index_fallback(seeded):
    import server
    x = json.loads(server.expand("cl1"))
    assert x["context_source"] == "index"
    turns = x["context"]["turns"]
    assert any(t.get("target") for t in turns)

def test_expand_claude_live_from_fixture_session(seeded, tmp_path, monkeypatch):
    import server
    from sources import claude as claude_src
    root = tmp_path / "projects"
    (root / "nope").mkdir(parents=True)
    lines = []
    for i, text in enumerate([
        "an earlier turn with enough characters to pass the minimum filter",
        "another earlier turn that is also long enough to be kept here",
        "a question about the indexing approach we chose, long enough to keep",
        "a reply after the target turn, padded to pass the length filter too",
    ]):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(json.dumps({"message": {"role": role, "content": text},
                                 "timestamp": f"{D}1{i}:00:00Z", "cwd": "/proj"}))
    (root / "nope" / "sess1.jsonl").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(claude_src, "ROOT", str(root))
    # chunk cl1 says lineno 5; the fixture file's matching turn is line 2
    import config, sqlite3
    db = sqlite3.connect(config.DB_PATH)
    db.execute("UPDATE chunks SET meta = json_set(meta, '$.lineno', 2) WHERE id='cl1'")
    db.commit()
    x = json.loads(server.expand("cl1", context=1))
    assert x["context_source"] == "live"
    turns = x["context"]["turns"]
    assert [t.get("target", False) for t in turns] == [False, True, False]

def test_expand_git_live_from_throwaway_repo(scratch_db, fake_embed,
                                             monkeypatch, tmp_path):
    import server
    repo = tmp_path / "repo"
    repo.mkdir()
    def g(*args):
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, check=True)
    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "T")
    (repo / "f.txt").write_text("hello\n")
    g("add", "f.txt")
    g("commit", "-qm", "add the greeting file")
    sha = g("rev-parse", "HEAD").stdout.strip()
    seed(monkeypatch, {"git": [("g1", "add the greeting file",
        rec("git", ts=f"{D}12:00:00+00:00", loc=f"repo@{sha[:8]}",
            meta={"repo": str(repo), "sha": sha, "count": 1}))]})
    x = json.loads(server.expand("g1"))
    assert x["context_source"] == "live"
    assert "add the greeting file" in x["context"]["show"]
    assert "f.txt" in x["context"]["show"]

def test_expand_browser_day_view(seeded):
    import server
    x = json.loads(server.expand("b1", context=1))
    assert x["context_source"] == "index"
    visits = x["context"]["visits"]
    assert [v.get("target", False) for v in visits] == [False, True, False]
    assert x["context"]["day"] == "2026-07-02"

def test_expand_calendar_day_agenda(scratch_db, fake_embed, monkeypatch):
    import server
    seed(monkeypatch, {"calendar": [
        # all-day chunk at local midnight (07:00Z in PDT) leads the agenda
        ("c1", "Calendar event on 2026-07-02 (Thursday), all day: Offsite (apple:Work).",
         rec("calendar", ts=f"{D}07:00:00+00:00", loc="apple:Work")),
        ("c2", "Calendar event on 2026-07-02 (Thursday) 10:00–10:30: Standup (apple:Work).",
         rec("calendar", ts=f"{D}17:00:00+00:00", loc="apple:Work")),
        ("c3", "Calendar event on 2026-07-02 (Thursday) 12:00–13:00: Lunch (apple:Home).",
         rec("calendar", ts=f"{D}19:00:00+00:00", loc="apple:Home")),
        ("c4", "Calendar event on 2026-07-03 (Friday) 10:00–10:30: Standup (apple:Work).",
         rec("calendar", ts="2026-07-03T17:00:00+00:00", loc="apple:Work")),
    ]})
    x = json.loads(server.expand("c2"))
    assert x["context_source"] == "index"
    ctx = x["context"]
    assert ctx["day"] == "2026-07-02"
    assert [e["id"] for e in ctx["agenda"]] == ["c1", "c2", "c3"]
    assert [e["id"] for e in ctx["agenda"] if e.get("target")] == ["c2"]

def test_expand_shell_no_atuin_is_null(seeded):
    import server
    x = json.loads(server.expand("sh1"))
    assert x["context"] is None and x["context_source"] is None

def test_expand_unknown_id(seeded):
    import server
    assert "error" in json.loads(server.expand("nope:0"))

def test_stats_locations_collapse(seeded):
    import server
    s = json.loads(server.history_stats(locations=True))
    assert s["sources"]["browser"]["locations"] == {"safari": 4}
    assert s["total_chunks"] == 6

def test_stats_reports_db_size(seeded):
    import server
    db = json.loads(server.history_stats())["db"]
    assert db["bytes"] > 0
    assert 0 <= db["freelist_bytes"] < db["bytes"]

def test_expand_survives_a_broken_context_handler(scratch_db, fake_embed,
                                                  monkeypatch):
    """Context is garnish: a raising expander (missing module on the
    replica, broken store, absent binary) degrades to a note — the chunk
    itself must always come back."""
    import server
    seed(monkeypatch, {"claude": [("a1", "the text", rec("claude"))]})

    def boom(db, chunk, n):
        raise ModuleNotFoundError("No module named 'sources'")

    monkeypatch.setitem(server._EXPANDERS, "claude", boom)
    out = json.loads(server.expand("a1"))
    assert out["chunk"]["text"] == "the text"
    assert "context unavailable" in out["context"]["note"]
    assert out["context_source"] is None

def test_digest_expand_surfaces_the_rollup(scratch_db, fake_embed,
                                           monkeypatch):
    import server
    meta = {"date": "2026-07-02", "digest_of": "browser",
            "visits": 41, "domains": {"github.com": 12},
            "searches": ["sqlite vec"]}
    seed(monkeypatch, {"digest": [("d1", "Browsing digest…",
                                   rec("digest", meta=meta))]})
    out = json.loads(server.expand("d1"))
    assert out["context_source"] == "index"
    assert out["context"]["day"] == "2026-07-02"
    assert out["context"]["rollup"]["domains"] == {"github.com": 12}
    assert "date" not in out["context"]["rollup"]        # lifted out


def test_claude_truncation_marker_only_when_cut(scratch_db, fake_embed,
                                                monkeypatch, tmp_path):
    import server
    from sources import claude as claude_src
    long_text = "x" * 2500
    lines = [json.dumps({"type": "user", "message": {
                 "role": "user", "content": t}, "timestamp": "2026-07-02T10:00:00Z"})
             for t in ("a perfectly ordinary short turn, over forty chars",
                       long_text)]
    (tmp_path / "ph").mkdir()
    (tmp_path / "ph" / "s1.jsonl").write_text("\n".join(lines))
    monkeypatch.setattr(claude_src, "ROOT", str(tmp_path))
    meta = {"session_id": "s1", "project_hash": "ph", "lineno": 1,
            "role": "user"}
    seed(monkeypatch, {"claude": [("c1", "short turn",
                                   rec("claude", meta=meta))]})
    turns = json.loads(server.expand("c1"))["context"]["turns"]
    by_len = {("long" if len(t["text"]) > 100 else "short"): t["text"]
              for t in turns}
    assert not by_len["short"].endswith("[truncated]")  # no marker
    assert by_len["long"].endswith("… [truncated]")
    assert len(by_len["long"]) == 2000 + len("… [truncated]")

def test_list_window_meta_optin_and_ellipsis(scratch_db, fake_embed,
                                             monkeypatch):
    import server
    seed(monkeypatch, {"shell": [
        ("s1", "x" * 200, rec("shell", ts=f"{D}10:00:00+00:00",
                              meta={"count": 3})),
        ("s2", "short", rec("shell", ts=f"{D}11:00:00+00:00"))]})
    out = json.loads(server.list_window(since="2026-07-02",
                                        until="2026-07-02"))
    assert all("meta" not in r for r in out["results"])   # default: compact
    long = next(r for r in out["results"] if len(r["text"]) > 100)
    assert long["text"].endswith("…") and len(long["text"]) == 161

    out = json.loads(server.list_window(since="2026-07-02",
                                        until="2026-07-02",
                                        include_meta=True))
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["s1"]["meta"] == {"count": 3}
    assert "meta" not in by_id["s2"]                      # empty meta omitted

def test_list_window_summaries_lead_their_day(scratch_db, fake_embed,
                                              monkeypatch):
    import server
    seed(monkeypatch, {
        "shell": [("raw2", "late command run today",
                   rec("shell", ts=f"{D}22:00:00+00:00")),
                  ("prev", "yesterday's command",
                   rec("shell", ts="2026-07-01T22:00:00+00:00"))],
        "digest": [("dig", "Browsing digest…",
                    rec("digest", ts=f"{D}07:00:00+00:00",
                        meta={"date": "2026-07-02", "visits": 3}))],
        "appusage": [("shape", "On 2026-07-02, active…",
                      rec("appusage", ts=f"{D}07:00:00+00:00",
                          meta={"first": "08:00", "active_seconds": 60})),
                     ("perapp", "spent 2m in Figma",
                      rec("appusage", ts=f"{D}07:00:00+00:00",
                          meta={"app": "Figma", "seconds": 120}))]})
    out = json.loads(server.list_window(since="2026-07-01",
                                        until="2026-07-02"))
    ids = [r["id"] for r in out["results"]]
    # newest local day first; within it: day-shape, digest, then raw DESC
    assert ids[:3] == ["shape", "dig", "raw2"]
    assert ids[-1] == "prev"                      # older day after
    assert "perapp" in ids[3:]                    # per-app is detail, not summary

def test_list_window_summaries_tier(scratch_db, fake_embed, monkeypatch):
    seed(monkeypatch, {
        "shell": [("raw", "a command", rec("shell", ts=f"{D}22:00:00+00:00"))],
        "digest": [("dig", "Shell digest…",
                    rec("digest", ts=f"{D}07:00:00+00:00",
                        meta={"date": "2026-07-02", "runs": 3}))],
        "appusage": [("shape", "On 2026-07-02…",
                      rec("appusage", ts=f"{D}07:00:00+00:00",
                          meta={"first": "08:00", "active_seconds": 60})),
                     ("perapp", "spent 2m in Figma",
                      rec("appusage", ts=f"{D}07:00:00+00:00",
                          meta={"app": "Figma", "seconds": 120}))]})
    import server
    out = json.loads(server.list_window(since="2026-07-02",
                                        until="2026-07-02", summaries=True))
    assert {r["id"] for r in out["results"]} == {"dig", "shape"}
    assert out["total"] == 2                      # count respects the tier
