#!/usr/bin/env python3
"""
Indexer for the history RAG -> sqlite-vec, embeddings via Ollama.
Pulls from every source in SOURCES. Idempotent: only embeds new/changed chunks.

Deps:  pip install sqlite-vec requests
Model: ollama pull nomic-embed-text
Run:   python index.py            # incremental
       python index.py --rebuild  # wipe and reindex
       python index.py --dry-run  # preview what would be indexed
"""
import json, sqlite3, sys
import sqlite_vec
import requests
from config import EMBED_MODEL, DIM, DB_PATH, OLLAMA
from sources import claude, shell

SOURCES = [claude, shell]
BATCH_SIZE = 64          # inputs per Ollama call

def all_chunks():
    for src in SOURCES:
        yield from src.iter_chunks()

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

def main():
    if "--dry-run" in sys.argv:
        n = 0
        for cid, text, rec in all_chunks():
            n += 1
            preview = text.replace("\n", " ")[:110]
            print(f"[{rec['source']:7}] {preview}")
            if n >= 40:
                print("... (showing first 40; remove --dry-run to index)")
                break
        if n == 0:
            print("No chunks survived the filter. Check field names vs inspect_sessions.py.")
        else:
            print(f"\n{n} kept so far. Looks right? Run without --dry-run.")
        return

    rebuild = "--rebuild" in sys.argv
    db = sqlite3.connect(DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    if rebuild:
        db.execute("DROP TABLE IF EXISTS chunks")
        db.execute("DROP TABLE IF EXISTS vec_chunks")
    setup(db)

    existing = {r[0] for r in db.execute("SELECT id FROM chunks")}
    new = failed = 0

    def store(cid, text, rec, vec):
        db.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?)",
                   (cid, text, rec["source"], rec.get("timestamp", ""),
                    rec.get("location", ""), json.dumps(rec.get("meta", {}))))
        db.execute("INSERT OR REPLACE INTO vec_chunks(id, embedding) VALUES (?, ?)",
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
        print(f"  indexed {new}...")

    batch = []
    try:
        for cid, text, rec in all_chunks():
            if cid in existing:
                continue
            batch.append((cid, text, rec))
            if len(batch) >= BATCH_SIZE:
                flush(batch); batch = []
        flush(batch)  # leftover
    except requests.exceptions.ConnectionError:
        print("Ollama not reachable on :11434 — is it running? Stopping.",
              file=sys.stderr)

    db.commit()
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"done. +{new} new chunks, {failed} skipped, {total} total in {DB_PATH}")

if __name__ == "__main__":
    main()
