"""Run health: the runs table the indexer keeps and the health field
history_stats builds from it — how failures reach the user."""
import json, sqlite3
import pytest
import requests
from tests.test_driver import mk_source, rec, run_index, open_db

def last_run(path):
    db = sqlite3.connect(path)
    row = db.execute("SELECT started, finished, status, sources FROM runs "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    return {"started": row[0], "finished": row[1], "status": row[2],
            "sources": json.loads(row[3] or "{}")}

def test_ok_run_recorded(scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [mk_source("alpha", [("a1", "text", rec("alpha"))])])
    run = last_run(scratch_db)
    assert run["status"] == "ok" and run["finished"]
    assert run["sources"]["alpha"] == {"chunks": 1, "embedded": 1,
                                       "failed": 0, "ok": True}

def test_partial_run_captures_error_text(scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [
        mk_source("alpha", [("a1", "text", rec("alpha"))]),
        mk_source("beta", [], explode=True),
    ])
    run = last_run(scratch_db)
    assert run["status"] == "partial"
    assert "boom" in run["sources"]["beta"]["error"]
    assert run["sources"]["alpha"]["ok"] is True

def test_aborted_run_recorded(scratch_db, fake_embed, monkeypatch):
    import index
    def down(texts):
        raise requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(index, "embed_batch", down)
    run_index(monkeypatch, [mk_source("alpha", [("a1", "text", rec("alpha"))])])
    assert last_run(scratch_db)["status"] == "aborted"

def test_stamp_mismatch_writes_aborted_row(scratch_db, fake_embed, monkeypatch):
    import config
    run_index(monkeypatch, [mk_source("alpha", [("a1", "text", rec("alpha"))])])
    monkeypatch.setattr(config, "EMBED_MODEL", "other-model")
    with pytest.raises(SystemExit):
        run_index(monkeypatch, [mk_source("alpha", [("a1", "text", rec("alpha"))])])
    run = last_run(scratch_db)
    assert run["status"] == "aborted"
    assert "refusing to mix" in run["sources"]["_stamp"]["error"]

def test_runs_retention(scratch_db, fake_embed, monkeypatch):
    import index
    monkeypatch.setattr(index, "RUNS_KEEP", 5)
    src = [mk_source("alpha", [("a1", "text", rec("alpha"))])]
    run_index(monkeypatch, src)
    db = open_db(scratch_db)
    db.executemany("INSERT INTO runs(started, finished, status) VALUES (?,?,'ok')",
                   [(f"2026-01-0{i}T00:00:00+00:00",) * 2 for i in range(1, 10)])
    db.commit(); db.close()
    run_index(monkeypatch, src)
    db = open_db(scratch_db)
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 5

def test_health_fresh_ok(scratch_db, fake_embed, monkeypatch):
    import server
    run_index(monkeypatch, [mk_source("alpha", [("a1", "text", rec("alpha"))])])
    h = json.loads(server.history_stats())["health"]
    assert h["status"] == "ok" and h["age_minutes"] <= 1
    assert "note" not in h and "failing_sources" not in h

def test_health_failing_source_and_stall(scratch_db, fake_embed, monkeypatch):
    import server
    run_index(monkeypatch, [mk_source("beta", [], explode=True)])
    h = json.loads(server.history_stats())["health"]
    assert h["status"] == "partial"
    assert "boom" in h["failing_sources"]["beta"]

    db = open_db(scratch_db)
    db.execute("UPDATE runs SET started='2026-01-01T00:00:00+00:00', "
               "finished='2026-01-01T00:01:00+00:00'")
    db.commit(); db.close()
    h = json.loads(server.history_stats())["health"]
    assert "stalled" in h["note"]

def test_health_absent_on_legacy_db(scratch_db, fake_embed, monkeypatch):
    import server
    run_index(monkeypatch, [mk_source("alpha", [("a1", "text", rec("alpha"))])])
    db = open_db(scratch_db)
    db.execute("DROP TABLE runs")
    db.commit(); db.close()
    assert "health" not in json.loads(server.history_stats())

def test_should_notify_fires_once_per_incident():
    import index
    assert index._should_notify(["aborted", "aborted"])
    assert index._should_notify(["aborted", "aborted", "ok"])
    assert not index._should_notify(["aborted"])                    # first flake
    assert not index._should_notify(["aborted", "aborted", "aborted"])  # already pinged
    assert not index._should_notify(["aborted", "ok"])
    assert not index._should_notify(["ok", "aborted", "aborted"])

def test_notify_fires_on_second_abort_and_respects_config(
        scratch_db, fake_embed, monkeypatch):
    import index
    pings = []
    monkeypatch.setattr(index, "_notify", pings.append)
    def down(texts):
        raise requests.exceptions.ConnectionError("refused")

    src = [mk_source("alpha", [("a1", "text", rec("alpha")),
                               ("a2", "more", rec("alpha"))])]
    monkeypatch.setenv("CLAUDE_RAG_NOTIFY", "true")
    monkeypatch.setattr(index, "embed_batch", down)
    run_index(monkeypatch, src)                  # first abort: no ping
    assert pings == []
    run_index(monkeypatch, src)                  # second abort: ping
    assert len(pings) == 1
    run_index(monkeypatch, src)                  # third abort: still one ping
    assert len(pings) == 1

    monkeypatch.setenv("CLAUDE_RAG_NOTIFY", "false")
    db = open_db(scratch_db)
    db.execute("DELETE FROM runs")
    db.commit(); db.close()
    run_index(monkeypatch, src)
    run_index(monkeypatch, src)                  # disabled: silent
    assert len(pings) == 1
