"""Convex sync tool: chunk shaping (local day/month, filter values, content
hash), the push plan diff, vector unpacking, batching against a fake
client, per-source signature skip, and the unconfigured no-op."""
import json, sqlite3, struct
import pytest
from tests.helpers import load_script

sc = load_script("tools/sync-convex.py", "sync_convex")

# ── shaping ──────────────────────────────────────────────────────────────────

def test_local_day_and_month_from_utc(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    import time; time.tzset()
    # 03:30 UTC on the 28th is still the 27th in LA
    assert sc.local_day("2026-08-28T03:30:00+00:00") == ("2026-08-27", "2026-08")
    assert sc.local_day("") == ("", "")
    assert sc.local_day("garbage") == ("", "")

def test_filter_values_carry_done_only_for_tasks():
    fv = sc.filter_values("tasks", "2026-08-28", "2026-08", "2026-08-28.md#3",
                          {"done": True})
    assert {"name": "done", "value": "1"} in fv
    assert {"name": "locpfx", "value": "2026-08-28.md#3"} in fv
    fv = sc.filter_values("obsidian", "2026-08-28", "2026-08",
                          "1-Projects/x.md#Plan", {})
    assert {"name": "locpfx", "value": "1-Projects/"} in fv
    assert {"name": "done", "value": ""} in fv      # every name, every add

def test_content_hash_changes_with_filters_not_just_text():
    a = sc.content_hash("t", [{"name": "done", "value": "0"}])
    b = sc.content_hash("t", [{"name": "done", "value": "1"}])
    assert a != b and a == sc.content_hash("t", [{"name": "done", "value": "0"}])

def test_shape_row():
    it = sc.shape(("tasks:abc", "tasks", "2026-08-28T07:00:00+00:00",
                   "2026-08-28.md#0", "Task: x", json.dumps({"done": False})),
                  [0.1, 0.2])
    assert it["chunkId"] == "tasks:abc" and it["day"] and it["month"] == it["day"][:7]
    assert it["embedding"] == [0.1, 0.2] and it["meta"] == {"done": False}

def test_unpack_float32():
    blob = struct.pack("3f", 1.0, 0.5, -0.25)
    assert sc.unpack(blob, 3) == [1.0, 0.5, -0.25]

def test_plan_diff():
    up, rm = sc.plan({"a": "h1", "b": "h2", "c": "h3"}, {"a": "h1", "b": "old", "z": "h9"})
    assert up == ["b", "c"] and rm == ["z"]

# ── push against a fake client ───────────────────────────────────────────────

class FakeClient:
    def __init__(self):
        self.calls = []
    def action(self, name, args):
        self.calls.append((name, args))
        if name == "sync:upsert":
            return [{"chunkId": i["chunkId"], "entryId": "e-" + i["chunkId"]}
                    for i in args["items"]]
        return 0
    def mutation(self, name, args):
        self.calls.append((name, args))

def _index(path, rows, dim=2):
    import sqlite_vec
    db = sqlite3.connect(path)
    db.enable_load_extension(True); sqlite_vec.load(db)
    db.execute("CREATE TABLE chunks(id TEXT PRIMARY KEY, text TEXT, source TEXT, "
               "timestamp TEXT, location TEXT, meta TEXT)")
    db.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(id TEXT PRIMARY KEY, "
               f"source TEXT partition key, embedding FLOAT[{dim}])")
    for cid, src, ts, loc, text, meta in rows:
        db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                   (cid, text, src, ts, loc, json.dumps(meta)))
        db.execute("INSERT INTO vec_chunks(id, source, embedding) VALUES (?,?,?)",
                   (cid, src, struct.pack(f"{dim}f", 0.5, 0.5)))
    db.commit()
    return db

@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(sc.config, "DIM", 2)
    monkeypatch.setattr(sc.config, "CONVEX_URL", "https://x.convex.cloud")
    monkeypatch.setattr(sc.config, "CONVEX_SOURCES", ["tasks"])
    monkeypatch.setattr(sc.config, "CONVEX_BATCH", 2)
    monkeypatch.setattr(sc.config, "CONVEX_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(sc.config, "DB_PATH", str(tmp_path / "ix.db"))
    monkeypatch.setattr(sc, "_have_convex", lambda: True)
    return tmp_path

ROWS = [("tasks:1", "tasks", "2026-08-28T07:00:00+00:00", "2026-08-28.md#0", "Task: a", {"done": False}),
        ("tasks:2", "tasks", "2026-08-28T07:00:00+00:00", "2026-08-28.md#1", "Task: b", {"done": True}),
        ("tasks:3", "tasks", "2026-08-27T07:00:00+00:00", "2026-08-27.md#0", "Task: c", {"done": True}),
        ("obs:1", "obsidian", "2026-08-01T07:00:00+00:00", "n.md", "Note\nx", {})]

def test_push_batches_then_incremental(cfg, monkeypatch):
    db = _index(cfg / "ix.db", ROWS)
    fake = FakeClient()
    monkeypatch.setattr(sc, "_client", lambda: fake)
    r = sc.main([])
    assert r["action"] == "pushed"
    ups = [c for c in fake.calls if c[0] == "sync:upsert"]
    assert [len(c[1]["items"]) for c in ups] == [2, 1]           # batch of 2, then 1
    assert all(i["source"] == "tasks" for c in ups for i in c[1]["items"])
    assert ups[0][1]["items"][0]["embedding"] == [0.5, 0.5]
    assert [c[0] for c in fake.calls][-2:] == ["sync:recordRun", "sync:gcReplaced"]
    # second run: signature unchanged -> nothing
    fake.calls.clear()
    assert sc.main([])["action"] == "unchanged" and fake.calls == []
    # tick a box: one upsert, no removes
    db.execute("UPDATE chunks SET meta = ? WHERE id = 'tasks:1'", (json.dumps({"done": True}),))
    db.commit()
    fake.calls.clear()
    r = sc.main([])
    ups = [c for c in fake.calls if c[0] == "sync:upsert"]
    assert len(ups) == 1 and [i["chunkId"] for i in ups[0][1]["items"]] == ["tasks:1"]
    assert not any(c[0] == "sync:remove" for c in fake.calls)
    # delete a chunk: one remove
    db.execute("DELETE FROM chunks WHERE id = 'tasks:3'"); db.commit()
    fake.calls.clear()
    sc.main([])
    rms = [c for c in fake.calls if c[0] == "sync:remove"]
    assert rms == [("sync:remove", {"chunkIds": ["tasks:3"]})]

def test_dry_run_and_source_flag(cfg, monkeypatch):
    _index(cfg / "ix.db", ROWS)
    monkeypatch.setattr(sc, "_client", lambda: (_ for _ in ()).throw(AssertionError("no client in dry-run")))
    r = sc.main(["--dry-run", "--source", "obsidian"])
    assert r["action"] == "dry-run"
    assert r["sources"] == [{"source": "obsidian", "chunks": 1, "upsert": 1, "remove": 0}]

def test_unconfigured_is_a_noop(monkeypatch):
    monkeypatch.setattr(sc.config, "CONVEX_URL", "")
    assert sc.main([])["action"] == "skipped"

def test_missing_vector_is_deferred(cfg, monkeypatch):
    db = _index(cfg / "ix.db", ROWS)
    db.execute("DELETE FROM vec_chunks WHERE id = 'tasks:2'"); db.commit()
    fake = FakeClient()
    monkeypatch.setattr(sc, "_client", lambda: fake)
    sc.main([])
    pushed = [i["chunkId"] for c in fake.calls if c[0] == "sync:upsert" for i in c[1]["items"]]
    assert "tasks:2" not in pushed and "tasks:1" in pushed


def test_deploy_key_falls_back_to_env_local(monkeypatch, tmp_path):
    monkeypatch.delenv("CONVEX_DEPLOY_KEY", raising=False)
    monkeypatch.setattr(sc.config, "CONVEX_DEPLOY_KEY_ENV", "CONVEX_DEPLOY_KEY")
    fake_tools = tmp_path / "tools"; fake_tools.mkdir()
    (tmp_path / "deploy" / "convex").mkdir(parents=True)
    (tmp_path / "deploy" / "convex" / ".env.local").write_text(
        "CONVEX_DEPLOYMENT=dev:x\nCONVEX_DEPLOY_KEY='dev:x|abc'\n")
    monkeypatch.setattr(sc, "__file__", str(fake_tools / "sync-convex.py"))
    assert sc.deploy_key() == "dev:x|abc"
    monkeypatch.setenv("CONVEX_DEPLOY_KEY", "from-env")
    assert sc.deploy_key() == "from-env"
    (tmp_path / "deploy" / "convex" / ".env.local").unlink()
    monkeypatch.delenv("CONVEX_DEPLOY_KEY")
    assert sc.deploy_key() == ""


def test_progress_summary_on_stderr(cfg, monkeypatch, capsys):
    _index(cfg / "ix.db", ROWS)
    fake = FakeClient()
    monkeypatch.setattr(sc, "_client", lambda: fake)
    sc.main([])
    err = capsys.readouterr().err
    assert "tasks: 3 chunks, 3 upserted, 0 removed in" in err
    sc.main([])
    assert "tasks: unchanged since 20" in capsys.readouterr().err
