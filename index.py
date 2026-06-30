#!/usr/bin/env python3
"""
Indexer for Claude Code session history -> sqlite-vec, embeddings via Ollama.
Global scope (all projects). Idempotent: only embeds new/changed chunks.

Deps:  pip install sqlite-vec requests
Model: ollama pull nomic-embed-text
Run:   python index.py            # incremental
       python index.py --rebuild  # wipe and reindex
"""
import json, glob, os, hashlib, sqlite3, sys, time
import sqlite_vec
import requests
from config import EMBED_MODEL, DIM, DB_PATH, OLLAMA

ROOT = os.path.expanduser("~/.claude/projects")
MIN_CHARS = 40           # skip trivial messages
MAX_CHARS = 2000         # truncate giant messages (stay under embed token limit)
BATCH_SIZE = 64          # inputs per Ollama call

def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts in one call. Returns embeddings in input order."""
    r = requests.post(OLLAMA, json={"model": EMBED_MODEL, "input": texts}, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]

def text_from_content(content, role) -> str:
    """Extract only human-authored prompt text / assistant reply text.

    Drops: tool_result blocks (tool output recorded under user role),
    tool_use blocks, thinking blocks, and command/stdout wrappers.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    # If a user message contains ANY tool_result block, it's tool output,
    # not a real prompt -> reject the whole message.
    if role == "user":
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return ""
    parts = []
    for b in content:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        # tool_use / thinking / tool_result -> skipped
    return "\n".join(parts)

# Markers of synthetic / non-conversational user lines to drop entirely.
_JUNK_SUBSTRINGS = (
    "<command-name>", "<local-command-stdout>", "<command-message>",
    "<command-args>", "[Request interrupted", "Caveat: The messages below",
)

def iter_messages():
    """Yield (chunk_id, text, metadata) for genuine user prompts + assistant text."""
    for fp in glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True):
        # project_hash = first path segment under ROOT, regardless of whether
        # sessions sit directly in the project dir or in a sessions/ subdir.
        rel = os.path.relpath(fp, ROOT)
        project_hash = rel.split(os.sep)[0]
        session_id = os.path.splitext(os.path.basename(fp))[0]
        with open(fp) as f:
            for lineno, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Drop system events, meta lines, and subagent/sidechain chatter.
                if obj.get("type") == "system":
                    continue
                if obj.get("isMeta") or obj.get("isSidechain"):
                    continue
                msg = obj.get("message", {})
                role = msg.get("role") or obj.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = text_from_content(msg.get("content", obj.get("content", "")), role)
                text = text.strip()
                if len(text) < MIN_CHARS:
                    continue
                if any(j in text for j in _JUNK_SUBSTRINGS):
                    continue
                meta = {
                    "session_id": session_id,
                    "project_hash": project_hash,
                    "cwd": obj.get("cwd", ""),
                    "role": role,
                    "timestamp": obj.get("timestamp", ""),
                    "lineno": lineno,
                }
                cid = hashlib.sha256(
                    f"{session_id}:{lineno}:{text[:200]}".encode()
                ).hexdigest()[:32]
                yield cid, text[:MAX_CHARS], meta

def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS chunks(
        id TEXT PRIMARY KEY, text TEXT, session_id TEXT, project_hash TEXT,
        cwd TEXT, role TEXT, timestamp TEXT, lineno INTEGER)""")
    db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
        id TEXT PRIMARY KEY, embedding FLOAT[{DIM}])""")

def main():
    if "--dry-run" in sys.argv:
        n = 0
        for cid, text, meta in iter_messages():
            n += 1
            preview = text.replace("\n", " ")[:120]
            print(f"[{meta['role']:9}] {preview}")
            if n >= 40:
                print("... (showing first 40; remove --dry-run to index)")
                break
        if n == 0:
            print("No messages survived the filter. Check field names vs inspect.py.")
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

    def store(cid, text, meta, vec):
        db.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?)",
                   (cid, text, meta["session_id"], meta["project_hash"],
                    meta["cwd"], meta["role"], meta["timestamp"], meta["lineno"]))
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
            for (cid, text, meta), vec in zip(batch, vecs):
                store(cid, text, meta, vec)
                new += 1
        except requests.exceptions.ConnectionError:
            raise  # bubble up to stop the run
        except Exception:
            for cid, text, meta in batch:        # fall back one at a time
                try:
                    vec = embed_batch([text])[0]
                    store(cid, text, meta, vec)
                    new += 1
                except Exception as e:
                    failed += 1
                    print(f"  skip chunk {meta['session_id']}:{meta['lineno']} "
                          f"({len(text)} chars) -> {e}", file=sys.stderr)
        db.commit()
        print(f"  indexed {new}...")

    batch = []
    try:
        for cid, text, meta in iter_messages():
            if cid in existing:
                continue
            batch.append((cid, text, meta))
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
