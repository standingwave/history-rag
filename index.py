#!/usr/bin/env python3
"""
Indexer for the history RAG -> sqlite-vec, embeddings via Ollama.
Pulls from every source in SOURCES. Idempotent: only embeds new/changed chunks.

Deps:  pip install sqlite-vec requests
Model: ollama pull nomic-embed-text
Run:   python index.py                  # incremental, all sources
       python index.py --rebuild        # wipe and reindex from scratch
       python index.py --dry-run        # preview what would be indexed
       python index.py --source shell   # one source only (combines with any mode)
       python index.py --prune --source X  # drop chunks source X stopped yielding
"""
import argparse, json, sqlite3, sys, time
import sqlite_vec
import requests
from config import EMBED_MODEL, DIM, DB_PATH, OLLAMA
from sources import claude, shell, appusage, browser, git, obsidian

SOURCES = [claude, shell, appusage, browser, git, obsidian]
BATCH_SIZE = 64          # inputs per Ollama call

def source_name(mod) -> str:
    return mod.__name__.rsplit(".", 1)[-1]

def parse_args():
    names = ", ".join(source_name(s) for s in SOURCES)
    p = argparse.ArgumentParser(description="Refresh the history RAG index.")
    p.add_argument("--rebuild", action="store_true", help="wipe and reindex from scratch")
    p.add_argument("--dry-run", action="store_true", help="preview chunks without indexing")
    p.add_argument("--prune", action="store_true",
                   help="delete stored chunks a source no longer yields")
    p.add_argument("--source", metavar="NAME", help=f"run a single source ({names})")
    return p.parse_args()

def pick_sources(only: str | None):
    if not only:
        return SOURCES
    picked = [s for s in SOURCES if source_name(s) == only]
    if not picked:
        sys.exit(f"unknown source {only!r}; available: "
                 + ", ".join(source_name(s) for s in SOURCES))
    return picked

def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts in one call. Returns embeddings in input order."""
    r = requests.post(OLLAMA, json={"model": EMBED_MODEL, "input": texts}, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]

def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS chunks(
        id TEXT PRIMARY KEY, text TEXT, source TEXT,
        timestamp TEXT, location TEXT, meta TEXT)""")
    db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
        id TEXT PRIMARY KEY, embedding FLOAT[{DIM}])""")

def dry_run(sources):
    n = 0
    for src in sources:
        try:
            for cid, text, rec in src.iter_chunks():
                n += 1
                preview = text.replace("\n", " ")[:110]
                print(f"[{rec['source']:7}] {preview}")
                if n >= 40:
                    print("... (showing first 40; remove --dry-run to index)")
                    return
        except Exception as e:
            print(f"{source_name(src)}: source failed -> {e}", file=sys.stderr)
    if n == 0:
        print("No chunks survived the filter. Check field names vs inspect_sessions.py.")
    else:
        print(f"\n{n} kept so far. Looks right? Run without --dry-run.")

def prune_stale(db, src_cols: set, keep_ids: set) -> int:
    """Delete stored chunks belonging to `src_cols` whose id wasn't yielded this
    run. Only called for a source that completed cleanly and yielded something,
    so an empty or broken source can never wipe its own history."""
    q = ",".join("?" * len(src_cols))
    rows = db.execute(f"SELECT id FROM chunks WHERE source IN ({q})", tuple(src_cols))
    stale = [(r[0],) for r in rows if r[0] not in keep_ids]
    if stale:
        db.executemany("DELETE FROM chunks WHERE id = ?", stale)
        db.executemany("DELETE FROM vec_chunks WHERE id = ?", stale)
        db.commit()
    return len(stale)

def main():
    args = parse_args()
    # The index is an archive: it keeps chunks whose backing data has since
    # aged out (deleted session files, rotated histfiles). A blanket prune
    # would delete that memory, so pruning must name one source deliberately.
    if args.prune and not args.source:
        sys.exit("--prune requires --source: pruning deletes stored chunks the "
                 "source no longer yields, which for sources whose backing data "
                 "expires (claude, shell) means losing history the index has "
                 "outlived. Prune one source at a time, deliberately.")
    sources = pick_sources(args.source)
    if args.dry_run:
        dry_run(sources)
        return

    db = sqlite3.connect(DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    if args.rebuild:
        db.execute("DROP TABLE IF EXISTS chunks")
        db.execute("DROP TABLE IF EXISTS vec_chunks")
    setup(db)

    # id -> current text, so a chunk is skipped only when unchanged. Chunks whose
    # text changed (e.g. today's still-growing app-usage total) get re-embedded.
    existing = {r[0]: r[1] for r in db.execute("SELECT id, text FROM chunks")}
    new = failed = pruned = 0

    def store(cid, text, rec, vec):
        db.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?)",
                   (cid, text, rec["source"], rec.get("timestamp", ""),
                    rec.get("location", ""), json.dumps(rec.get("meta", {}))))
        db.execute("DELETE FROM vec_chunks WHERE id = ?", (cid,))
        db.execute("INSERT INTO vec_chunks(id, embedding) VALUES (?, ?)",
                   (cid, sqlite_vec.serialize_float32(vec)))

    def flush(batch):
        """Embed and store a batch. On batch error, retry items individually
        so one bad chunk doesn't sink the whole batch."""
        nonlocal new, failed
        if not batch:
            return
        texts = [b[1] for b in batch]
        try:
            vecs = embed_batch(texts)
            for (cid, text, rec), vec in zip(batch, vecs):
                store(cid, text, rec, vec)
                new += 1
        except requests.exceptions.ConnectionError:
            raise  # bubble up to stop the run
        except Exception:
            for cid, text, rec in batch:         # fall back one at a time
                try:
                    vec = embed_batch([text])[0]
                    store(cid, text, rec, vec)
                    new += 1
                except Exception as e:
                    failed += 1
                    print(f"  skip chunk {cid} ({len(text)} chars) -> {e}",
                          file=sys.stderr)
        db.commit()

    # Each source runs isolated: one broken source logs and the rest still
    # index. Only an unreachable Ollama aborts the whole run.
    try:
        for src in sources:
            name, t0 = source_name(src), time.monotonic()
            seen_ids, src_cols = set(), set()
            yielded, new0, failed0 = 0, new, failed
            ok, batch = True, []
            try:
                for cid, text, rec in src.iter_chunks():
                    seen_ids.add(cid)
                    src_cols.add(rec["source"])
                    yielded += 1
                    if existing.get(cid) == text:
                        continue
                    batch.append((cid, text, rec))
                    if len(batch) >= BATCH_SIZE:
                        flush(batch)
                        batch = []
                flush(batch)
            except requests.exceptions.ConnectionError:
                raise
            except Exception as e:
                ok = False
                print(f"{name}: source failed after {yielded} chunks -> {e}",
                      file=sys.stderr)
            n_pruned = 0
            if args.prune and ok and seen_ids:
                n_pruned = prune_stale(db, src_cols, seen_ids)
                pruned += n_pruned
            status = "" if ok else "  [FAILED, partial]"
            extra = f", {n_pruned} pruned" if n_pruned else ""
            print(f"{name}: {yielded} chunks, {new - new0} embedded, "
                  f"{failed - failed0} skipped{extra}, "
                  f"{time.monotonic() - t0:.1f}s{status}")
    except requests.exceptions.ConnectionError:
        print("Ollama not reachable on :11434 — is it running? Stopping.",
              file=sys.stderr)

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    extra = f", {pruned} pruned" if pruned else ""
    print(f"done. {new} embedded (new or changed), {failed} skipped{extra}, "
          f"{total} total in {DB_PATH}")

if __name__ == "__main__":
    main()
