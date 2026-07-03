"""Switch the index to a new embedding model — archive-safe, by copy-and-swap.

Reads chunk TEXT from the production DB (the durable asset — never from
sources, so archive-only chunks survive), embeds into a NEW file with the
target model, verifies, then swaps:
    history-rag.db -> history-rag.db.bak   (rollback + point-in-time backup)
    history-rag.new-<model>.db -> history-rag.db
If an eval candidate DB for the target model exists (built by
tools/eval-model.py), its vectors are reused for matching (id, text) pairs —
text must match, not just id, since chunks can change after an eval.

This tool does NOT write your config: it prints the [core] lines to put in
~/.claude/history-rag.toml after the swap. Until you do, the stamp check
makes indexer and server refuse the new DB — loud, not corrupt.

Run:  ~/.claude/rag-venv/bin/python tools/migrate-model.py --model X --dim N
Resume after an interruption: run it again (already-embedded ids are kept).
"""
import argparse, os, sqlite3, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests, sqlite_vec
import config

BATCH = 64

def new_path(prod: str, model: str) -> str:
    d, base = os.path.split(prod)
    stem = base[:-3] if base.endswith(".db") else base
    return os.path.join(d, f"{stem}.new-{model}.db")

def eval_path(prod: str, model: str) -> str:
    d, base = os.path.split(prod)
    stem = base[:-3] if base.endswith(".db") else base
    return os.path.join(d, f"{stem}.eval-{model}.db")

def open_db(path: str):
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db

def embed_batch(model: str, texts):
    r = requests.post(config.OLLAMA, json={"model": model, "input": texts},
                      timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]

def read_stamp(db):
    has = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                     "AND name='index_meta'").fetchone()
    return dict(db.execute("SELECT key, value FROM index_meta")) if has else {}

def run_migration(model: str, dim: int, swap: bool = True) -> str:
    """Build the new-model DB next to production and (optionally) swap it in.
    Returns the path now holding the new DB."""
    prod = config.DB_PATH
    target = new_path(prod, model)
    pdb = open_db(prod)
    prod_stamp = read_stamp(pdb)
    if prod_stamp.get("model") == model and prod_stamp.get("dim") == str(dim):
        sys.exit(f"production index is already {model}/{dim}")

    ndb = open_db(target)
    ndb.execute("CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, "
                "text TEXT, source TEXT, timestamp TEXT, location TEXT, meta TEXT)")
    ndb.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
                f"id TEXT PRIMARY KEY, embedding FLOAT[{dim}])")
    ndb.execute("CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY, value TEXT)")
    existing_stamp = read_stamp(ndb)
    if existing_stamp.get("model") not in (None, model):
        sys.exit(f"{target} exists but was built with "
                 f"{existing_stamp.get('model')} — remove it first")
    ndb.executemany("INSERT OR REPLACE INTO index_meta VALUES (?,?)",
                    [("model", model), ("dim", str(dim)),
                     ("created", datetime.now(timezone.utc).isoformat())])

    # Chunk rows copy verbatim from prod (text is model-independent).
    rows = pdb.execute("SELECT id, text, source, timestamp, location, meta "
                       "FROM chunks").fetchall()
    ndb.executemany("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?)", rows)
    ndb.commit()

    done = {r[0] for r in ndb.execute("SELECT id FROM vec_chunks")}

    # Reuse the eval build's vectors where (id, text) still matches.
    cand_file = eval_path(prod, model)
    reused = 0
    if os.path.exists(cand_file):
        cdb = open_db(cand_file)
        cstamp = read_stamp(cdb)
        if cstamp.get("model") == model and cstamp.get("dim") == str(dim):
            cand = {r[0]: r[1] for r in cdb.execute("SELECT id, text FROM chunks")}
            for cid, text, *_ in rows:
                if cid in done or cand.get(cid) != text:
                    continue
                blob = cdb.execute("SELECT embedding FROM vec_chunks WHERE id=?",
                                   (cid,)).fetchone()
                if blob:
                    ndb.execute("INSERT INTO vec_chunks(id, embedding) VALUES (?,?)",
                                (cid, blob[0]))
                    done.add(cid)
                    reused += 1
            ndb.commit()
        else:
            print(f"candidate at {cand_file} has stamp {cstamp} — not reusing",
                  flush=True)

    todo = [(cid, text) for cid, text, *_ in rows if cid not in done]
    print(f"{len(rows)} chunks: {reused} vectors reused from eval, "
          f"{len(done) - reused} already present, {len(todo)} to embed",
          flush=True)
    t0 = time.monotonic()
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        vecs = embed_batch(model, [t for _, t in batch])
        ndb.executemany("INSERT INTO vec_chunks(id, embedding) VALUES (?,?)",
                        [(cid, sqlite_vec.serialize_float32(v))
                         for (cid, _), v in zip(batch, vecs)])
        ndb.commit()
        if (i // BATCH) % 5 == 0:
            print(f"  embedded {min(i + BATCH, len(todo))}/{len(todo)} "
                  f"({time.monotonic() - t0:.0f}s)", flush=True)

    # Verify before touching production.
    n_prod = pdb.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_new = ndb.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_vec = ndb.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
    if not (n_prod == n_new == n_vec):
        sys.exit(f"verification failed: prod chunks {n_prod}, new chunks "
                 f"{n_new}, new vectors {n_vec} — NOT swapping")
    for probe in ("a test query", "recent work", "something I read"):
        vec = embed_batch(model, [probe])[0]
        hit = ndb.execute(
            "SELECT id FROM vec_chunks WHERE embedding MATCH ? AND k = 1",
            (sqlite_vec.serialize_float32(vec),)).fetchone()
        if not hit:
            sys.exit(f"verification failed: probe {probe!r} returned nothing")
    print(f"verified: {n_new} chunks, {n_vec} vectors, probes OK", flush=True)
    pdb.close()
    ndb.close()

    if not swap:
        return target
    bak = prod + ".bak"
    os.replace(prod, bak)
    os.replace(target, prod)
    print(f"swapped. rollback copy: {bak}", flush=True)
    print(f"\nNOW update ~/.claude/history-rag.toml:\n"
          f"[core]\nmodel = \"{model}\"\ndim = {dim}\n\n"
          f"Until then, indexer and server refuse the new DB (stamp check) — "
          f"and reconnect the MCP server (/mcp) after updating.", flush=True)
    return prod

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--dim", type=int, required=True)
    ap.add_argument("--no-swap", action="store_true",
                    help="build and verify only; leave production in place")
    args = ap.parse_args()
    run_migration(args.model, args.dim, swap=not args.no_swap)

if __name__ == "__main__":
    main()
