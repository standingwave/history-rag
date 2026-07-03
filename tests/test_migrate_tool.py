"""migrate-model tool: archive-only chunks survive (the pinned TESTING.md
case), eval vectors are reused only on (id, text) match, and the stamp
refusal fires until config catches up."""
import importlib.util, json, pathlib, sqlite3
import pytest
import sqlite_vec
import config
from tests.test_driver import mk_source, rec, run_index, open_db

def _load():
    p = pathlib.Path(__file__).resolve().parent.parent / "tools" / "migrate-model.py"
    spec = importlib.util.spec_from_file_location("migrate_model", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

@pytest.fixture
def prod_with_archive(scratch_db, fake_embed, monkeypatch):
    """A production DB with one source-backed chunk and one archive-only
    chunk (present in the DB, absent from any source)."""
    run_index(monkeypatch, [mk_source("alpha", [("a1", "live text", rec("alpha"))])])
    db = open_db(scratch_db)
    db.execute("INSERT INTO chunks VALUES ('arch1', 'archive-only text', "
               "'alpha', '', 'gone', '{}')")
    db.execute("INSERT INTO vec_chunks(id, embedding) VALUES ('arch1', ?)",
               (sqlite_vec.serialize_float32(fake_embed("archive-only text")),))
    db.commit()
    db.close()
    return scratch_db

def _patch_embed(monkeypatch, mig, calls):
    def fake(model, texts):
        calls.extend(texts)
        import hashlib
        def one(t):
            h = hashlib.sha256((model + t).encode()).digest()
            v = [(b - 128) / 128.0 for b in h] * (config.DIM // 32 + 1)
            return v[:config.DIM]
        return [one(t) for t in texts]
    monkeypatch.setattr(mig, "embed_batch", fake)

def test_archive_chunk_survives_and_stamp_flips(prod_with_archive, monkeypatch):
    mig = _load()
    calls = []
    _patch_embed(monkeypatch, mig, calls)
    result = mig.run_migration("new-model", config.DIM, swap=True)
    assert result == prod_with_archive                 # swapped into place
    db = open_db(prod_with_archive)
    ids = {r[0] for r in db.execute("SELECT id FROM chunks")}
    assert ids == {"a1", "arch1"}                      # archive survived
    assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 2
    stamp = dict(db.execute("SELECT key, value FROM index_meta"))
    assert stamp["model"] == "new-model"
    assert sorted(calls[:2]) == ["archive-only text", "live text"]
    # rollback copy exists with the old stamp
    bak = open_db(prod_with_archive + ".bak")
    assert dict(bak.execute("SELECT key, value FROM index_meta"))["model"] == \
        config.EMBED_MODEL
    # refusal fires until config is updated
    import server
    with pytest.raises(config.StampMismatch):
        server.history_stats()
    monkeypatch.setattr(config, "EMBED_MODEL", "new-model")
    assert json.loads(server.history_stats())["embedding"]["model"] == "new-model"

def test_eval_vectors_reused_only_on_text_match(prod_with_archive, monkeypatch):
    mig = _load()
    # Fabricate an eval candidate: a1 matches text, arch1 has STALE text.
    cand_file = mig.eval_path(prod_with_archive, "new-model")
    cdb = open_db(cand_file)
    cdb.execute("CREATE TABLE chunks(id TEXT PRIMARY KEY, text TEXT)")
    cdb.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0("
                f"id TEXT PRIMARY KEY, embedding FLOAT[{config.DIM}])")
    cdb.execute("CREATE TABLE index_meta(key TEXT PRIMARY KEY, value TEXT)")
    cdb.executemany("INSERT INTO index_meta VALUES (?,?)",
                    [("model", "new-model"), ("dim", str(config.DIM))])
    marker = [0.5] * config.DIM
    cdb.execute("INSERT INTO chunks VALUES ('a1', 'live text')")
    cdb.execute("INSERT INTO vec_chunks(id, embedding) VALUES ('a1', ?)",
                (sqlite_vec.serialize_float32(marker),))
    cdb.execute("INSERT INTO chunks VALUES ('arch1', 'OUTDATED text')")
    cdb.execute("INSERT INTO vec_chunks(id, embedding) VALUES ('arch1', ?)",
                (sqlite_vec.serialize_float32(marker),))
    cdb.commit(); cdb.close()

    calls = []
    _patch_embed(monkeypatch, mig, calls)
    mig.run_migration("new-model", config.DIM, swap=False)
    # a1 reused, arch1 re-embedded; the other 3 calls are verification probes
    chunk_embeds = [c for c in calls if c in ("live text", "archive-only text",
                                              "OUTDATED text")]
    assert chunk_embeds == ["archive-only text"]
    ndb = open_db(mig.new_path(prod_with_archive, "new-model"))
    blob = ndb.execute("SELECT embedding FROM vec_chunks WHERE id='a1'").fetchone()[0]
    assert blob == sqlite_vec.serialize_float32(marker)   # the copied vector

def test_refuses_when_already_on_target(prod_with_archive, monkeypatch):
    mig = _load()
    with pytest.raises(SystemExit) as e:
        mig.run_migration(config.EMBED_MODEL, config.DIM)
    assert "already" in str(e.value)
