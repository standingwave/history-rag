"""Tier 2: server tool envelopes and expand context shapes, on a seeded
scratch index with the fake embedder. Timestamps are UTC; TZ is pinned to
America/Los_Angeles, so local day 2026-07-02 = [07-02T07:00Z, 07-03T07:00Z)."""
import json, os, subprocess
import pytest
from tests.test_driver import mk_source, rec, run_index

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
    assert len(big["text"]) == 160

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
