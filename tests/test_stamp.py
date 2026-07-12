"""Embedding-model provenance stamp: the index records what built it and
both entry points refuse a mismatch (which would otherwise be silent
corruption for same-dimension model swaps)."""
import json, sqlite3
import pytest
import config
from tests.helpers import mk_source, rec, run_index

CHUNK = [("a1", "some text", rec("alpha"))]

def read_stamp(path):
    db = sqlite3.connect(path)
    return dict(db.execute("SELECT key, value FROM index_meta"))

def test_fresh_index_is_stamped(scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [mk_source("alpha", CHUNK)])
    stamp = read_stamp(scratch_db)
    assert stamp["model"] == config.EMBED_MODEL
    assert stamp["dim"] == str(config.DIM)
    assert "created" in stamp

def test_mismatch_refused_by_indexer_and_server(scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [mk_source("alpha", CHUNK)])
    monkeypatch.setattr(config, "EMBED_MODEL", "other-model")
    with pytest.raises(SystemExit) as e:
        run_index(monkeypatch, [mk_source("alpha", CHUNK)])
    assert "refusing to mix" in str(e.value)
    import server
    with pytest.raises(config.StampMismatch):
        server.history_stats()

def test_dim_mismatch_alone_is_refused(scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [mk_source("alpha", CHUNK)])
    monkeypatch.setattr(config, "DIM", 1024)
    with pytest.raises(SystemExit):
        run_index(monkeypatch, [mk_source("alpha", CHUNK)])

def test_legacy_db_tolerated_by_server_and_stamped_by_indexer(
        scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [mk_source("alpha", CHUNK)])
    db = sqlite3.connect(scratch_db)
    db.execute("DROP TABLE index_meta")                   # simulate pre-stamp DB
    db.commit(); db.close()
    import server
    assert json.loads(server.history_stats())["total_chunks"] == 1   # tolerated
    assert "embedding" not in json.loads(server.history_stats())
    run_index(monkeypatch, [mk_source("alpha", CHUNK)])   # indexer stamps it
    assert read_stamp(scratch_db)["model"] == config.EMBED_MODEL

def test_rebuild_is_the_escape_hatch_and_restamps(scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [mk_source("alpha", CHUNK)])
    monkeypatch.setattr(config, "EMBED_MODEL", "new-model")
    run_index(monkeypatch, [mk_source("alpha", CHUNK)], argv=["--rebuild"])
    assert read_stamp(scratch_db)["model"] == "new-model"

def test_stats_surfaces_embedding(scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [mk_source("alpha", CHUNK)])
    import server
    s = json.loads(server.history_stats())
    assert s["embedding"] == {"model": config.EMBED_MODEL, "dim": config.DIM}
