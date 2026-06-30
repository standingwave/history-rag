#!/usr/bin/env python3
"""
MCP server: exposes `search_history` over your indexed history (Claude Code
sessions + shell history). Embeds the query via Ollama, does vec KNN.

Deps:  pip install "mcp[cli]" sqlite-vec requests
Register (one time):
  claude mcp add history -- python /ABS/PATH/server.py
"""
import sqlite3, json
import sqlite_vec, requests
from mcp.server.fastmcp import FastMCP
from config import EMBED_MODEL, DB_PATH, OLLAMA

mcp = FastMCP("claude-history")

def _db():
    db = sqlite3.connect(DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db

def _embed(text: str):
    r = requests.post(OLLAMA, json={"model": EMBED_MODEL, "input": text}, timeout=60)
    r.raise_for_status()
    return r.json()["embeddings"][0]

@mcp.tool()
def search_history(query: str, k: int = 5, source: str = "", project_hash: str = "") -> str:
    """Search your past history (Claude Code sessions + shell commands) by meaning.

    Args:
        query: what to look for.
        k: number of results (default 5).
        source: optional — restrict to one source ('claude' or 'shell').
        project_hash: optional — restrict to one Claude project's sessions.
    Returns: JSON list of matches with text + source + metadata + distance.
    """
    vec = _embed(query)
    db = _db()
    rows = db.execute("""
        SELECT v.distance, c.text, c.source, c.timestamp, c.location, c.meta
        FROM vec_chunks v JOIN chunks c ON c.id = v.id
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
    """, (sqlite_vec.serialize_float32(vec), max(k * 4, 20))).fetchall()

    out = []
    for dist, text, src, ts, loc, meta_json in rows:
        if source and src != source:
            continue
        meta = json.loads(meta_json) if meta_json else {}
        if project_hash and meta.get("project_hash") != project_hash:
            continue
        out.append({
            "distance": round(dist, 4),
            "source": src,
            "timestamp": ts,
            "location": loc,
            "text": text,
            "meta": meta,
        })
        if len(out) >= k:
            break
    return json.dumps(out, indent=2)

if __name__ == "__main__":
    mcp.run()
