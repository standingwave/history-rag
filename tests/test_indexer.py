"""Tier 2: index.py driver behaviors against real sqlite-vec + fake embedder.
Each case reproduces something that actually happened during development."""
import pytest
from tests.helpers import mk_source, rec, run_index, open_db
def test_incremental_reembed_and_consistency(scratch_db, fake_embed, monkeypatch):
    src = [mk_source("alpha", [("a1", "first text", rec("alpha"))])]
    run_index(monkeypatch, src)
    assert fake_embed.calls == ["first text"]

    run_index(monkeypatch, src)                      # unchanged -> skipped
    assert fake_embed.calls == ["first text"]

    db = open_db(scratch_db)
    vec_before = db.execute("SELECT embedding FROM vec_chunks WHERE id='a1'").fetchone()[0]
    run_index(monkeypatch, [mk_source("alpha", [("a1", "changed text", rec("alpha"))])])
    assert fake_embed.calls == ["first text", "changed text"]
    db = open_db(scratch_db)
    vec_after = db.execute("SELECT embedding FROM vec_chunks WHERE id='a1'").fetchone()[0]
    assert vec_before != vec_after                   # the vec0 update bug's test
    assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == \
           db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 1

def test_metadata_refresh_does_not_reembed(scratch_db, fake_embed, monkeypatch):
    run_index(monkeypatch, [mk_source("alpha",
        [("a1", "same text", rec("alpha", ts="2026-07-01T00:00:00+00:00"))])])
    n_embeds = len(fake_embed.calls)
    run_index(monkeypatch, [mk_source("alpha",
        [("a1", "same text", rec("alpha", ts="2026-07-02T00:00:00+00:00", loc="new"))])])
    assert len(fake_embed.calls) == n_embeds         # zero re-embeds
    db = open_db(scratch_db)
    ts, loc = db.execute("SELECT timestamp, location FROM chunks WHERE id='a1'").fetchone()
    assert ts == "2026-07-02T00:00:00+00:00" and loc == "new"

def test_source_isolation_and_partial_batch_dropped(scratch_db, fake_embed,
                                                    monkeypatch, capsys):
    sources = [
        mk_source("alpha", [("a1", "alpha one", rec("alpha"))]),
        mk_source("beta", [("b1", "beta pending", rec("beta"))], explode=True),
        mk_source("gamma", [("c1", "gamma one", rec("gamma"))]),
    ]
    run_index(monkeypatch, sources)
    err = capsys.readouterr().err
    assert "beta: source failed" in err
    db = open_db(scratch_db)
    ids = {r[0] for r in db.execute("SELECT id FROM chunks")}
    assert ids == {"a1", "c1"}                       # b1's partial batch dropped

def test_prune_deletes_stale_and_guards_hold(scratch_db, fake_embed, monkeypatch):
    a = mk_source("alpha", [("a1", "one", rec("alpha")), ("a2", "two", rec("alpha"))])
    run_index(monkeypatch, [a])

    # stale: alpha stops yielding a2 -> prune removes it from both tables
    a_now = mk_source("alpha", [("a1", "one", rec("alpha"))])
    run_index(monkeypatch, [a_now], argv=["--prune", "--source", "alpha"])
    db = open_db(scratch_db)
    assert {r[0] for r in db.execute("SELECT id FROM chunks")} == {"a1"}
    assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 1

    # a failed source must never prune its own rows
    a_boom = mk_source("alpha", [], explode=True)
    run_index(monkeypatch, [a_boom], argv=["--prune", "--source", "alpha"])
    db = open_db(scratch_db)
    assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1

    # an empty (no-op) source must never prune its own rows either
    a_empty = mk_source("alpha", [])
    run_index(monkeypatch, [a_empty], argv=["--prune", "--source", "alpha"])
    db = open_db(scratch_db)
    assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1

def test_prune_horizon_bounds_deletion(scratch_db, fake_embed, monkeypatch):
    # A source declaring PRUNE_WINDOW_DAYS only prunes stale chunks stamped
    # inside the window (recent or future); older and undated stale chunks
    # are archive — its backing store forgets legitimately.
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    ts_old = (now - timedelta(days=60)).isoformat()
    ts_recent = (now - timedelta(days=1)).isoformat()
    ts_future = (now + timedelta(days=30)).isoformat()

    def bounded(chunks):
        src = mk_source("alpha", chunks)
        src.PRUNE_WINDOW_DAYS = 30
        return src

    run_index(monkeypatch, [bounded([
        ("keep", "kept", rec("alpha", ts=ts_recent)),
        ("old", "old stale", rec("alpha", ts=ts_old)),
        ("recent", "recent stale", rec("alpha", ts=ts_recent)),
        ("future", "future stale", rec("alpha", ts=ts_future)),
        ("undated", "undated stale", rec("alpha")),
    ])])

    run_index(monkeypatch,
              [bounded([("keep", "kept", rec("alpha", ts=ts_recent))])],
              argv=["--prune", "--source", "alpha"])
    db = open_db(scratch_db)
    assert {r[0] for r in db.execute("SELECT id FROM chunks")} == \
           {"keep", "old", "undated"}
    assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 3

def test_vec_partition_migration(scratch_db, fake_embed, monkeypatch):
    """A pre-partition DB is rebuilt in place with `source` as a vec0
    partition key: embeddings copied byte-for-byte, nothing re-embedded,
    orphaned vectors dropped."""
    import sqlite_vec
    import config, index
    db = open_db(scratch_db)
    db.execute("""CREATE TABLE chunks(id TEXT PRIMARY KEY, text TEXT,
        source TEXT, timestamp TEXT, location TEXT, meta TEXT)""")
    db.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0("
               f"id TEXT PRIMARY KEY, embedding FLOAT[{config.DIM}])")
    blob = sqlite_vec.serialize_float32(fake_embed("old text"))
    db.execute("INSERT INTO chunks VALUES ('o1','old text','alpha','','x','{}')")
    db.execute("INSERT INTO vec_chunks(id, embedding) VALUES ('o1', ?)", (blob,))
    db.execute("INSERT INTO vec_chunks(id, embedding) VALUES ('orphan', ?)",
               (blob,))
    db.commit()

    index.setup(db)
    sql = db.execute("SELECT sql FROM sqlite_master "
                     "WHERE name='vec_chunks'").fetchone()[0]
    assert "partition key" in sql
    assert db.execute("SELECT id, source, embedding FROM vec_chunks").fetchall() \
        == [("o1", "alpha", blob)]
    assert fake_embed.calls == []                    # nothing re-embedded

    index.setup(db)                                  # idempotent on new schema
    assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 1
