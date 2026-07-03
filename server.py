#!/usr/bin/env python3
"""
MCP server over your indexed local history (Claude Code sessions, shell history,
browser history, and — on macOS — app usage). Embeds the query via Ollama and
does vector KNN.

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
def search_history(query: str, k: int = 5, source: str = "", location: str = "",
                   since: str = "", until: str = "",
                   include_undated: bool = False,
                   max_distance: float = 0.0) -> str:
    """Semantic search over the user's own local history. Prefer this over
    guessing when a question refers to something they did, decided, ran, or used
    before. One shared index spans these sources:
      - claude:   past Claude Code conversation turns (their prompts + replies)
      - shell:    bash/zsh commands they've run (deduped; often undated)
      - appusage: daily per-app time on their Mac ("spent 2h 14m in Figma")
      - browser:  pages they've visited (Safari/Chrome/Helium; title + URL,
                  deduped per browser profile — location is "browser:profile"
                  — timestamped by last visit)
      - git:      commit messages they've authored across local repos
      - obsidian: their Obsidian vault notes, chunked by heading

    Args:
      query: natural-language description of what to recall.
      k: max results (default 5).
      source: restrict to 'claude' | 'shell' | 'appusage' | 'browser' | 'git'
        | 'obsidian' (default: all).
      location: case-sensitive prefix filter on each chunk's location, e.g.
        'chrome:First user' or 'chrome:' (browser profile), 'littlebird@'
        (git repo), 'projects/' (obsidian folder). Combine with source to
        disambiguate.
      since / until: ISO date or datetime bounds on the chunk timestamp, e.g.
        since='2026-07-02' for "today" (resolve relative phrases like "last
        week" to concrete dates yourself; history_stats shows each source's
        range). A date-only until covers that whole day. When either bound is
        set, undated rows (common for shell) are excluded unless
        include_undated=true.
      max_distance: drop results whose distance exceeds this. Distance is L2 over
        embeddings — LOWER = more relevant; strong matches run ~0.5-0.9. Leave 0
        to disable. If results come back empty, raise k or drop this/source.

    Returns JSON {query, count, results[]}, results ranked best-first. Each has
    rank (1=best), source, distance (lower=closer), text, and — when present —
    timestamp, location, and meta (e.g. shell run count, app + seconds). A
    missing timestamp just means that row isn't dated (common for shell).
    """
    # Timestamps are ISO-8601, so bounds are lexicographic string compares.
    # A bare end date sorts before that day's datetimes; expand to end-of-day.
    if until and len(until) == 10:
        until += "T23:59:59~"   # '~' sorts after any digit/offset suffix
    vec = _embed(query)
    db = _db()
    # Over-fetch, then filter in Python. Location and time filters can match a
    # tiny slice of the index, so they widen the candidate pool a lot.
    pool = max(k * (8 if source else 4), 30)
    if location or since or until:
        pool = max(pool, k * 64, 400)
    rows = db.execute("""
        SELECT v.distance, c.text, c.source, c.timestamp, c.location, c.meta
        FROM vec_chunks v JOIN chunks c ON c.id = v.id
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
    """, (sqlite_vec.serialize_float32(vec), pool)).fetchall()

    results = []
    for dist, text, src, ts, loc, meta_json in rows:
        if source and src != source:
            continue
        if location and not (loc or "").startswith(location):
            continue
        if since or until:
            if not ts:
                if not include_undated:
                    continue
            elif (since and ts < since) or (until and ts > until):
                continue
        if max_distance and dist > max_distance:
            continue
        item = {"rank": len(results) + 1, "source": src,
                "distance": round(dist, 4), "text": text}
        if ts:
            item["timestamp"] = ts
        if loc:
            item["location"] = loc
        meta = json.loads(meta_json) if meta_json else {}
        if meta:
            item["meta"] = meta
        results.append(item)
        if len(results) >= k:
            break
    out = {"query": query, "count": len(results), "results": results}
    if since or until:
        out["window"] = {"since": since or None, "until": until or None}
        if len(results) < k:
            out["note"] = (f"only {len(results)} of k={k} in window from a "
                           f"{pool}-candidate pool; raise k to search deeper")
    return json.dumps(out)

@mcp.tool()
def history_stats() -> str:
    """Show what search_history can search: per-source chunk counts and the date
    range each covers. Call this first to orient — e.g. to confirm app-usage or
    shell history is indexed, or how far back the record goes — before searching.
    Returns JSON {total_chunks, sources: {name: {chunks, earliest, latest}}}."""
    db = _db()
    sources = {}
    for src, cnt, mn, mx in db.execute(
        "SELECT source, COUNT(*), MIN(NULLIF(timestamp,'')), MAX(NULLIF(timestamp,'')) "
        "FROM chunks GROUP BY source"):
        sources[src] = {"chunks": cnt, "earliest": mn, "latest": mx}
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return json.dumps({"total_chunks": total, "sources": sources})

if __name__ == "__main__":
    mcp.run()
