#!/usr/bin/env python3
"""
MCP server: exposes `search_history` over your indexed Claude Code chat history.
Global scope, returns full metadata. Embeds the query via Ollama, does vec KNN.

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
def search_history(query: str, k: int = 5, project_hash: str = "") -> str:
    """Search past Claude Code conversations (all projects) by meaning.

    Args:
        query: what to look for.
        k: number of results (default 5).
        project_hash: optional — restrict to one project's sessions.
    Returns: JSON list of matches with text + full metadata + distance.
    """
    vec = _embed(query)
    db = _db()
    rows = db.execute("""
        SELECT v.distance, c.text, c.session_id, c.project_hash, c.cwd,
               c.role, c.timestamp, c.lineno
        FROM vec_chunks v JOIN chunks c ON c.id = v.id
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
    """, (sqlite_vec.serialize_float32(vec), max(k * 4, 20))).fetchall()

    out = []
    for dist, text, sid, ph, cwd, role, ts, ln in rows:
        if project_hash and ph != project_hash:
            continue
        out.append({
            "distance": round(dist, 4),
            "role": role,
            "timestamp": ts,
            "session_id": sid,
            "project_hash": ph,
            "cwd": cwd,
            "lineno": ln,
            "text": text,
        })
        if len(out) >= k:
            break
    return json.dumps(out, indent=2)

if __name__ == "__main__":
    mcp.run()
