#!/usr/bin/env python3
"""Push a subset of the index to the Convex spike (deploy/convex,
wip/SPEC-convex-spike.md). A second replica target beside sync-s3.py; the
Mac's SQLite index stays the writer and Convex never sees a source that
isn't listed in [convex] sources.

Per source: a content signature over (id, timestamp, text, meta) is
compared with the last push's — unchanged means nothing moves. Otherwise
the chunk set is diffed against a local state table (chunk id -> content
hash) so only new/changed chunks are upserted and only vanished ones are
removed. Vectors go up precomputed from vec_chunks; Convex never embeds
stored content. Batches stay well under Convex's 1 MiB argument limit
(a 1024-dim float64 vector is ~8 KB; [convex] batch defaults to 60).

No-op unless [convex] url is set and the `convex` package is importable
in this venv (`uv pip install --python ~/.claude/rag-venv/bin/python convex`).

Progress and a per-source summary go to stderr as each source completes;
the JSON result (what refresh.py records) goes to stdout at the end.

Run:    ~/.claude/rag-venv/bin/python tools/sync-convex.py [--full] [--dry-run]
                                                           [--source NAME]
Config: [convex] url, deploy_key_env (default CONVEX_DEPLOY_KEY),
        sources (default tasks/obsidian/calendar), batch (60),
        state_db (~/.claude/history-rag-convex.db)
"""
import argparse, hashlib, json, os, sqlite3, struct, sys, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# ── chunk shaping (pure; unit-tested) ────────────────────────────────────────

def loc_prefix(source: str, loc: str) -> str:
    """Mirror of server._loc_prefix, kept here so the sync tool has no
    import-time dependency on the MCP server module."""
    if source == "git":
        return loc.split("@")[0] + "@"
    if source == "obsidian":
        head = loc.split("#")[0]
        return head.split("/")[0] + "/" if "/" in head else head
    return loc

def local_day(ts: str) -> tuple[str, str]:
    """UTC ISO -> (local YYYY-MM-DD, local YYYY-MM); '' for undated."""
    if not ts:
        return "", ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "", ""
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    d = dt.date().isoformat()
    return d, d[:7]

def filter_values(source: str, day: str, month: str, location: str,
                  meta: dict) -> list:
    # The RAG component requires every declared filter name on every add,
    # so non-task sources carry an empty `done` that no filter matches.
    done = ("1" if meta.get("done") else "0") if source == "tasks" else ""
    return [{"name": "day", "value": day}, {"name": "month", "value": month},
            {"name": "locpfx", "value": loc_prefix(source, location)},
            {"name": "done", "value": done}]

def content_hash(text: str, fvals: list) -> str:
    h = hashlib.sha256(text.encode())
    h.update(json.dumps(fvals, sort_keys=True).encode())
    return h.hexdigest()[:32]

def shape(row: tuple, embedding: list | None) -> dict:
    """(id, source, timestamp, location, text, meta_json) -> upsert item."""
    cid, source, ts, location, text, meta_json = row
    meta = json.loads(meta_json) if meta_json else {}
    day, month = local_day(ts)
    fvals = filter_values(source, day, month, location or "", meta)
    item = {"chunkId": cid, "source": source, "timestamp": ts or "",
            "day": day, "month": month, "location": location or "",
            "text": text, "meta": meta,
            "contentHash": content_hash(text, fvals),
            "filterValues": fvals}
    if embedding is not None:
        item["embedding"] = embedding
    return item

def unpack(blob: bytes, dim: int) -> list:
    return list(struct.unpack(f"{dim}f", blob))

def plan(current: dict, state: dict) -> tuple[list, list]:
    """current: chunk id -> content hash from the index; state: the same
    from the last push. Returns (ids to upsert, ids to remove)."""
    up = [i for i, h in current.items() if state.get(i) != h]
    rm = [i for i in state if i not in current]
    return up, rm

def signature(db, source: str) -> str:
    h = hashlib.sha256()
    for row in db.execute("SELECT id, timestamp, text, coalesce(meta, '') "
                          "FROM chunks WHERE source = ? ORDER BY id", (source,)):
        for f in row:
            h.update(str(f).encode()); h.update(b"\x1f")
    return h.hexdigest()

# ── state ────────────────────────────────────────────────────────────────────

def prev_at(state, source: str) -> str:
    row = state.execute("SELECT pushed_at FROM sigs WHERE source = ?",
                        (source,)).fetchone()
    return row[0][:16] if row and row[0] else "?"

def open_state(path: str):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pushed(
            id TEXT PRIMARY KEY, source TEXT, hash TEXT, entry_id TEXT);
        CREATE INDEX IF NOT EXISTS pushed_source ON pushed(source);
        CREATE TABLE IF NOT EXISTS sigs(source TEXT PRIMARY KEY, sig TEXT,
                                        pushed_at TEXT);""")
    return db

# ── client ───────────────────────────────────────────────────────────────────

def _have_convex() -> bool:
    try:
        import convex  # noqa: F401
        return True
    except ImportError:
        return False


def deploy_key() -> str:
    """The deploy key: the configured env var, else the same variable in
    deploy/convex/.env.local (where the Convex CLI keeps it), so one file
    serves the CLI, these tools, and the launchd applier."""
    key = os.environ.get(config.CONVEX_DEPLOY_KEY_ENV, "")
    if key:
        return key
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_file = os.path.join(root, "deploy", "convex", ".env.local")
    try:
        with open(env_file) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == config.CONVEX_DEPLOY_KEY_ENV and v:
                    return v.strip().strip("'\"")
    except OSError:
        pass
    return ""

def _client():
    from convex import ConvexClient
    key = deploy_key()
    if not key:
        raise RuntimeError(f"{config.CONVEX_DEPLOY_KEY_ENV} is not set "
                           "(env or deploy/convex/.env.local)")
    c = ConvexClient(config.CONVEX_URL)
    c.set_admin_auth(key)
    return c

def push_source(client, index_db, state, source: str, batch: int,
                dry_run: bool = False, full: bool = False) -> dict:
    rows = index_db.execute(
        "SELECT id, source, timestamp, location, text, meta FROM chunks "
        "WHERE source = ? ORDER BY id", (source,)).fetchall()
    current = {}
    shaped = {}
    for r in rows:
        it = shape(r, None)
        current[it["chunkId"]] = it["contentHash"]
        shaped[it["chunkId"]] = it
    prior = {} if full else dict(state.execute(
        "SELECT id, hash FROM pushed WHERE source = ?", (source,)).fetchall())
    up, rm = plan(current, prior)
    res = {"source": source, "chunks": len(rows), "upsert": len(up),
           "remove": len(rm)}
    if dry_run:
        return res
    t0 = time.time()
    def progress(done_n: int):
        print(f"  {source}: {done_n}/{len(up)} upserted "
              f"({time.time() - t0:.0f}s)", file=sys.stderr, flush=True)
    for i in range(0, len(up), batch):
        ids = up[i:i + batch]
        if i and (i // batch) % 20 == 0:
            progress(i)
        items = []
        for cid in ids:
            blob = index_db.execute(
                "SELECT embedding FROM vec_chunks WHERE id = ?", (cid,)).fetchone()
            if not blob:
                continue           # vector not yet embedded; next run
            it = dict(shaped[cid]); it["embedding"] = unpack(blob[0], config.DIM)
            items.append(it)
        if not items:
            continue
        out = client.action("sync:upsert", {"items": items})
        state.executemany(
            "INSERT OR REPLACE INTO pushed(id, source, hash, entry_id) "
            "VALUES (?, ?, ?, ?)",
            [(o["chunkId"], source, current[o["chunkId"]], o["entryId"])
             for o in out])
        state.commit()
    for i in range(0, len(rm), batch):
        ids = rm[i:i + batch]
        client.action("sync:remove", {"chunkIds": ids})
        state.executemany("DELETE FROM pushed WHERE id = ?", [(x,) for x in ids])
        state.commit()
    client.mutation("sync:recordRun", {
        "source": source, "startedAt": t0 * 1000, "finishedAt": time.time() * 1000,
        "upserted": len(up), "removed": len(rm)})
    res["seconds"] = round(time.time() - t0, 1)
    print(f"{source}: {len(rows)} chunks, {len(up)} upserted, {len(rm)} removed "
          f"in {res['seconds']}s", file=sys.stderr, flush=True)
    return res

def main(argv=None) -> dict:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--full", action="store_true",
                   help="ignore local state; re-push every chunk")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--source", metavar="NAME", help="just this source")
    a = p.parse_args(argv if argv is not None else [])
    if not config.CONVEX_URL:
        return {"action": "skipped", "reason": "no [convex] url"}
    if not _have_convex():
        return {"action": "skipped", "reason": "convex package not installed"}
    if not os.path.exists(config.DB_PATH):
        return {"action": "skipped", "reason": "no index"}
    import sqlite_vec
    index_db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    index_db.enable_load_extension(True)
    sqlite_vec.load(index_db)
    sources = ([a.source] if a.source else config.CONVEX_SOURCES
               or [r[0] for r in index_db.execute(
                   "SELECT DISTINCT source FROM chunks ORDER BY source")])
    # One pusher at a time: a full push takes an hour and the refresh
    # chain fires every 30 min against the same state DB.
    lock = open(config.CONVEX_STATE_DB + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"action": "skipped", "reason": "another sync is running"}
    state = open_state(config.CONVEX_STATE_DB)
    client = None if a.dry_run else _client()
    results, moved = [], False
    for src in sources:
        sig = signature(index_db, src)
        prev = state.execute("SELECT sig FROM sigs WHERE source = ?",
                             (src,)).fetchone()
        if prev and prev[0] == sig and not a.full:
            results.append({"source": src, "unchanged": True})
            print(f"{src}: unchanged since {prev_at(state, src)}", file=sys.stderr)
            continue
        r = push_source(client, index_db, state, src, config.CONVEX_BATCH,
                        dry_run=a.dry_run, full=a.full)
        results.append(r)
        if not a.dry_run:
            state.execute("INSERT OR REPLACE INTO sigs VALUES (?, ?, ?)",
                          (src, sig, datetime.now().astimezone().isoformat()))
            state.commit()
            moved = moved or bool(r["upsert"] or r["remove"])
    if client is not None and moved:
        client.action("sync:gcReplaced", {})
    return {"action": "dry-run" if a.dry_run else ("pushed" if moved else "unchanged"),
            "sources": results}

if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:]), indent=1))
