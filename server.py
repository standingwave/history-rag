#!/usr/bin/env python3
"""
MCP server over your indexed local history (Claude Code sessions, shell history,
browser history, and — on macOS — app usage). Embeds the query via Ollama and
does vector KNN.

Deps:  pip install "mcp[cli]" sqlite-vec requests
Register (one time):
  claude mcp add history -- python /ABS/PATH/server.py
"""
import sqlite3, json, os, subprocess
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
import sqlite_vec, requests
from mcp.server.fastmcp import FastMCP
import config

mcp = FastMCP("claude-history")

# Windowed subsets up to this size are ranked exhaustively by true distance;
# beyond it we fall back to KNN-pool sampling. Purely a latency knob.
EXACT_WINDOW_MAX = 4000

# ── connection, embedding, and query helpers ────────────────────────────────

def _db():
    # A fresh connection per tool call: ~1ms, and it always sees the
    # indexer's latest commits with no thread-affinity questions. Config is
    # read by attribute so tests can re-point it without reload gymnastics.
    db = sqlite3.connect(config.DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    # Querying with a different model than built the vectors returns quietly
    # broken rankings — refuse instead (raises; surfaces as a tool error).
    config.check_stamp(db)
    return db

def _embed(text: str):
    if config.EMBED_BACKEND == "nomic-api":
        r = requests.post(config.NOMIC_API_URL,
                          headers={"Authorization":
                                   f"Bearer {config.NOMIC_API_KEY}"},
                          json={"model": config.NOMIC_API_MODEL,
                                "task_type": config.NOMIC_TASK_TYPE,
                                "dimensionality": config.DIM,
                                "texts": [text]}, timeout=60)
        r.raise_for_status()
        return r.json()["embeddings"][0]
    if config.EMBED_BACKEND == "mixedbread-api":
        r = requests.post(config.MXBAI_API_URL,
                          headers={"Authorization":
                                   f"Bearer {config.MXBAI_API_KEY}"},
                          json={"model": config.MXBAI_API_MODEL,
                                "input": [config.MXBAI_QUERY_PROMPT + text],
                                "dimensions": config.DIM,
                                "normalized": True,
                                "encoding_format": "float"}, timeout=60)
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    if config.EMBED_BACKEND == "ollama":
        r = requests.post(config.OLLAMA, json={"model": config.EMBED_MODEL,
                                               "input": text}, timeout=60)
        r.raise_for_status()
        return r.json()["embeddings"][0]
    raise ValueError(f"unknown embed backend {config.EMBED_BACKEND!r} "
                     f"(want 'ollama', 'nomic-api', or 'mixedbread-api')")

def _bound_to_utc(bound: str, end_of_day: bool = False) -> str:
    """Normalize a since/until bound to a UTC ISO string for lexicographic
    comparison against the index's UTC timestamps. A date-only bound means the
    machine's *local* day; a naive datetime is local too; an offset-carrying
    datetime is converted."""
    if len(bound) == 10:                       # bare date -> local day
        dt = datetime.fromisoformat(bound)     # naive local midnight
        if end_of_day:                         # last microsecond of local day
            dt = dt + timedelta(days=1)
            return (dt.astimezone(timezone.utc)
                    - timedelta(microseconds=1)).isoformat()
        return dt.astimezone(timezone.utc).isoformat()
    dt = datetime.fromisoformat(bound.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()                   # attach local zone
    return dt.astimezone(timezone.utc).isoformat()

def _parse_bounds(since: str, until: str):
    """UTC-normalize a since/until pair -> (since, until, error-JSON-or-None)."""
    try:
        return (_bound_to_utc(since) if since else "",
                _bound_to_utc(until, end_of_day=True) if until else "", None)
    except ValueError:
        return "", "", json.dumps(
            {"error": f"bad since/until (want ISO date or datetime): "
                      f"since={since!r} until={until!r}"})

def _window_where(since, until, source, location, include_undated):
    """WHERE clause + params for a time/source/location slice of chunks.
    Bounds must already be UTC-normalized."""
    conds, params = [], []
    if since:
        conds.append("timestamp >= ?"); params.append(since)
    if until:
        conds.append("timestamp <= ?"); params.append(until)
    sql = " AND ".join(conds) or "1=1"
    if conds:
        sql = f"(timestamp = '' OR ({sql}))" if include_undated \
            else f"{sql} AND timestamp != ''"
    if source:
        sql += " AND source = ?"; params.append(source)
    if location:
        sql += " AND substr(location, 1, ?) = ?"
        params += [len(location), location]
    return sql, params

def _loc_prefix(source: str, loc: str) -> str:
    """Collapse a chunk location to a reusable location-filter prefix."""
    if source == "git":                       # repo@sha -> repo@
        return loc.split("@")[0] + "@"
    if source == "obsidian":                  # dir/note.md#H -> dir/
        head = loc.split("#")[0]
        return head.split("/")[0] + "/" if "/" in head else head
    return loc

# ── per-source expanders ─────────────────────────────────────────────────────
# Uniform signature: (db, chunk, n) -> (context, context_source). `chunk` is
# the dict expand() returns to the caller. Register new sources in _EXPANDERS.

def _expand_claude(db, chunk, n):
    from sources import claude as claude_src
    meta = chunk["meta"]
    sid, lineno = meta.get("session_id", ""), meta.get("lineno", -1)
    fp = os.path.join(claude_src.ROOT, meta.get("project_hash", ""),
                      f"{sid}.jsonl")
    if os.path.isfile(fp):
        before, target, after = deque(maxlen=n), None, []
        for ln, role, text, ts, _cwd in claude_src.iter_turns(fp):
            turn = {"lineno": ln, "role": role, "timestamp": ts,
                    "text": text[:2000]}
            if ln < lineno:
                before.append(turn)
            elif ln == lineno:
                target = {**turn, "target": True}
            else:
                after.append(turn)
                if len(after) >= n:
                    break
        return {"turns": [*before, *([target] if target else []), *after]}, "live"
    # session transcript aged out -> neighbors from the index
    rows = db.execute(
        """SELECT text, timestamp, meta FROM chunks WHERE source = 'claude'
           AND json_extract(meta, '$.session_id') = ?
           ORDER BY json_extract(meta, '$.lineno')""", (sid,)).fetchall()
    turns = []
    for text, ts, mj in rows:
        m = json.loads(mj) if mj else {}
        turns.append({"lineno": m.get("lineno"), "role": m.get("role"),
                      "timestamp": ts, "text": text})
    idx = next((i for i, t in enumerate(turns) if t["lineno"] == lineno), None)
    if idx is not None:
        turns[idx]["target"] = True
        turns = turns[max(0, idx - n):idx + n + 1]
    return {"turns": turns}, "index"

def _expand_git(db, chunk, n):
    meta = chunk["meta"]
    repo, sha = meta.get("repo", ""), meta.get("sha", "")
    if repo and sha and os.path.isdir(repo):
        try:
            out = subprocess.run(["git", "-C", repo, "show", "--stat", sha],
                                 capture_output=True, text=True, timeout=30)
            if out.returncode == 0:
                return {"show": out.stdout[:4000]}, "live"
            return {"note": "git show failed: "
                    + out.stderr.strip()[:200]}, "index"
        except (OSError, subprocess.SubprocessError) as e:
            # OSError covers a missing git binary (FileNotFoundError)
            return {"note": f"git show failed: {e}"}, "index"
    return {"note": "repo or commit no longer available"}, "index"

def _expand_browser(db, chunk, n):
    cid, loc, ts = chunk["id"], chunk["location"], chunk["timestamp"]
    if not ts:
        return None, None
    local = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    day0 = datetime(local.year, local.month, local.day)
    lo = day0.astimezone(timezone.utc).isoformat()
    hi = ((day0 + timedelta(days=1)).astimezone(timezone.utc)
          - timedelta(microseconds=1)).isoformat()
    rows = db.execute(
        """SELECT id, timestamp, text FROM chunks WHERE source = 'browser'
           AND location = ? AND timestamp >= ? AND timestamp <= ?
           ORDER BY timestamp""", (loc, lo, hi)).fetchall()
    idx = next((i for i, r in enumerate(rows) if r[0] == cid), 0)
    visits = [{"id": i, "timestamp": t, "text": x[:160], **({"target": True}
               if i == cid else {})}
              for i, t, x in rows[max(0, idx - n):idx + n + 1]]
    return {"day": day0.date().isoformat(), "profile": loc,
            "visits": visits}, "index"

def _expand_obsidian(db, chunk, n):
    meta = chunk["meta"]
    import config
    for v in config.get_paths("obsidian", "vaults", "CLAUDE_RAG_OBSIDIAN_VAULTS"):
        if os.path.basename(v.rstrip("/")) == meta.get("vault", ""):
            p = os.path.join(v, meta.get("path", ""))
            if os.path.isfile(p):
                with open(p, errors="replace") as f:
                    return {"note_text": f.read()[:8000]}, "live"
    rows = db.execute(
        """SELECT id, location, text FROM chunks WHERE source = 'obsidian'
           AND json_extract(meta, '$.vault') = ?
           AND json_extract(meta, '$.path') = ? ORDER BY location""",
        (meta.get("vault", ""), meta.get("path", ""))).fetchall()
    return {"sections": [{"id": i, "location": l, "text": t}
                         for i, l, t in rows]}, "index"

def _expand_calendar(db, chunk, n):
    """The day's agenda: every calendar chunk on the hit's local day, across
    calendars and apps, ordered by start. All-day chunks are stamped local
    midnight, so they lead the list. Index-backed (chunks ARE the agenda);
    n is unused — a day's agenda is the unit of meaning."""
    cid, ts = chunk["id"], chunk["timestamp"]
    if not ts:
        return None, None
    local = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    day0 = datetime(local.year, local.month, local.day)
    lo = day0.astimezone(timezone.utc).isoformat()
    hi = ((day0 + timedelta(days=1)).astimezone(timezone.utc)
          - timedelta(microseconds=1)).isoformat()
    rows = db.execute(
        """SELECT id, timestamp, text FROM chunks WHERE source = 'calendar'
           AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp""",
        (lo, hi)).fetchall()
    agenda = [{"id": i, "timestamp": t, "text": x[:300], **({"target": True}
               if i == cid else {})}
              for i, t, x in rows]
    return {"day": day0.date().isoformat(), "agenda": agenda}, "index"

def _expand_appusage(db, chunk, n):
    try:
        from appusage import store
        db2 = store.connect()
        store.setup(db2)
        day = store.daily_durations(db2).get(chunk["meta"].get("date", ""), {})
        apps = {a: int(s) for a, s in
                sorted(day.items(), key=lambda kv: -kv[1])}
        return {"date": chunk["meta"].get("date"),
                "seconds_by_app": apps}, "live"
    except Exception:
        return None, None

def _expand_shell(db, chunk, n):
    from sources.shell import atuin_context
    ctx = atuin_context(chunk["text"], n)
    return (ctx, "live") if ctx else (None, None)

_EXPANDERS = {
    "claude": _expand_claude,
    "git": _expand_git,
    "browser": _expand_browser,
    "obsidian": _expand_obsidian,
    "appusage": _expand_appusage,
    "shell": _expand_shell,
    "calendar": _expand_calendar,
}

# ── tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_history(query: str, k: int = 5, source: str = "", location: str = "",
                   since: str = "", until: str = "",
                   include_undated: bool = False,
                   max_distance: float = 0.0) -> str:
    """Semantic search over the user's own local history. Prefer this over
    guessing when a question refers to something they did, decided, ran, or used
    before. One shared index spans these sources:
      - claude:   past Claude Code conversation turns (their prompts + replies)
      - shell:    bash/zsh commands they've run (deduped; dated + cwd-located
                  where atuin or EXTENDED_HISTORY recorded the run)
      - appusage: daily per-app time on their Mac ("spent 2h 14m in Figma")
      - browser:  pages they've visited (Safari/Chrome/Helium; title + URL,
                  deduped per browser profile — location is "browser:profile"
                  — timestamped by last visit). Search-engine queries (Google,
                  DuckDuckGo, YouTube search) are indexed as chunks reading
                  `Searched <site> for "<terms>"` — for "what did I search"
                  questions, query with that phrasing to catch every engine.
      - git:      commit messages they've authored across local repos
      - obsidian: their Obsidian vault notes, chunked by heading
      - calendar: their calendar events — meetings and appointments with
                  attendee names, all past plus ~90 days ahead (so "what's
                  coming up Thursday" works; this source's timestamps can be
                  in the future). Location is "app:calendar name", e.g.
                  "apple:Work".
      - digest:   precomputed daily rollups, one chunk per (local day,
                  stream): browser-profile visits/searches, claude sessions,
                  shell runs. For "what did I do <day/week>" questions, list
                  these first (list_window source='digest') instead of paging
                  raw chunks; expand() returns the full rollup meta.

    Args:
      query: natural-language description of what to recall.
      k: max results (default 5).
      source: restrict to 'claude' | 'shell' | 'appusage' | 'browser' | 'git'
        | 'obsidian' | 'calendar' | 'digest' (default: all).
      location: case-sensitive prefix filter on each chunk's location, e.g.
        'chrome:First user' or 'chrome:' (browser profile), 'littlebird@'
        (git repo), 'projects/' (obsidian folder). Combine with source to
        disambiguate.
      since / until: time window. Bare dates ('2026-07-02') mean the user's
        LOCAL day — the server converts to UTC, so resolve relative phrases
        ("last week") to local dates and pass them as-is. Datetimes are
        accepted too (offset-carrying or UTC 'Z'; naive = local). The applied
        UTC window is echoed in the response. When either bound is set,
        undated rows (common for shell) are excluded unless
        include_undated=true.
      max_distance: drop results whose distance exceeds this. Distance is L2 over
        embeddings — LOWER = more relevant; strong matches run ~0.5-0.9. Leave 0
        to disable. If results come back empty, raise k or drop this/source.

    Returns JSON {query, count, results[]}, results ranked best-first. Each has
    rank (1=best), id, source, distance (lower=closer), text, and — when
    present — timestamp, location, and meta. A missing timestamp just means
    that row isn't dated (common for shell). Results are pointers: pass an id
    to expand() for the full chunk plus its surrounding context. For an
    exhaustive chronological listing of a time window, use list_window —
    this tool ranks by relevance, not completeness.

    When presenting results: a brief lead-in summary, then the results, then
    stop — the results speak for themselves. If ~/.claude/history-rag-instructions.md
    exists, read it before answering: the user keeps their recall coverage and
    presentation preferences there.
    """
    since, until, err = _parse_bounds(since, until)
    if err:
        return err
    vec = _embed(query)
    db = _db()
    qblob = sqlite_vec.serialize_float32(vec)

    # Any filter can select a slice too small for KNN sampling to reach (147
    # chunks of one day, or git's ~500 among 34k, won't crack a global
    # top-pool for most queries). When the filtered subset is small, rank ALL
    # of it by true distance instead — exhaustive, no sampling loss.
    rows, exact = None, False
    if since or until or source or location:
        where, params = _window_where(since, until, source, location,
                                      include_undated)
        n_window = db.execute(f"SELECT COUNT(*) FROM chunks WHERE {where}",
                              params).fetchone()[0]
        if n_window <= EXACT_WINDOW_MAX:
            exact = True
            rows = db.execute(f"""
                SELECT vec_distance_l2(v.embedding, ?) AS distance, c.id,
                       c.text, c.source, c.timestamp, c.location, c.meta
                FROM vec_chunks v JOIN chunks c ON c.id = v.id
                WHERE {where} ORDER BY distance
            """, (qblob, *params)).fetchall()

    if rows is None:
        # Over-fetch, then filter in Python. Location and time filters can
        # match a small slice, so they widen the candidate pool a lot.
        pool = max(k * (8 if source else 4), 30)
        if source or location or since or until:
            pool = max(pool, k * 64, 400)
        rows = db.execute("""
            SELECT v.distance, c.id, c.text, c.source, c.timestamp, c.location, c.meta
            FROM vec_chunks v JOIN chunks c ON c.id = v.id
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
        """, (qblob, pool)).fetchall()

    results = []
    for dist, cid, text, src, ts, loc, meta_json in rows:
        if not exact:                 # the exact path already filtered in SQL
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
        item = {"rank": len(results) + 1, "id": cid, "source": src,
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
    if exact:
        out["exact"] = True       # every chunk matching the filters was ranked
    if since or until:
        out["window"] = {"since": since or None, "until": until or None}
    if (since or until or source or location) and not exact and len(results) < k:
        out["note"] = (f"only {len(results)} of k={k} matched from a sampled "
                       f"candidate pool; raise k to search deeper")
    return json.dumps(out)

@mcp.tool()
def history_stats(locations: bool = False) -> str:
    """Show what search_history can search: per-source chunk counts and the date
    range each covers. Call this first to orient — e.g. to confirm app-usage or
    shell history is indexed, or how far back the record goes — before searching.
    Pass locations=true to also get each source's top location prefixes with
    counts (browser profiles, git repos, obsidian folders, claude project
    dirs) — these are valid values for the search/list `location` filter.
    Returns JSON {total_chunks, embedding: {model, dim}, db: {bytes,
    freelist_bytes}, health, sources: {name: {chunks, earliest, latest
    [, locations]}}}. `db.freelist_bytes` is reclaimable churn inside the
    file — report size as "X MB (Y reclaimable)" when it's substantial.

    `health` reports the last index run: {last_run, age_minutes, status,
    failing_sources?, note?}. IMPORTANT: if health contains `note` or
    `failing_sources`, tell the user about it in one line BEFORE answering
    their question — the index may be stale or partially blind, and this is
    the only place that failure becomes visible."""
    db = _db()
    sources = {}
    for src, cnt, mn, mx in db.execute(
        "SELECT source, COUNT(*), MIN(NULLIF(timestamp,'')), MAX(NULLIF(timestamp,'')) "
        "FROM chunks GROUP BY source"):
        sources[src] = {"chunks": cnt, "earliest": mn, "latest": mx}
    if locations:
        counts = {src: Counter() for src in sources}
        for src, loc, cnt in db.execute(
                "SELECT source, location, COUNT(*) FROM chunks "
                "GROUP BY source, location"):
            counts[src][_loc_prefix(src, loc or "")] += cnt
        for src, c in counts.items():
            sources[src]["locations"] = dict(c.most_common(20))
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    out = {"total_chunks": total, "sources": sources}
    stamp = config.check_stamp(db)
    if stamp:
        out["embedding"] = {"model": stamp["model"], "dim": int(stamp["dim"])}
    psize = db.execute("PRAGMA page_size").fetchone()[0]
    out["db"] = {
        "bytes": db.execute("PRAGMA page_count").fetchone()[0] * psize,
        "freelist_bytes": db.execute("PRAGMA freelist_count").fetchone()[0] * psize,
    }
    health = _run_health(db)
    if health:
        replica = _replica_health(db)
        if replica:
            health["replica"] = replica
        out["health"] = health
    return json.dumps(out)

STALL_MINUTES = 120           # ~4 missed 30-min cycles
REPLICA_STALL_MINUTES = 90    # ~3 missed refresh ticks without a sync stamp

def _replica_health(db):
    """Replica sync recency from the newest refresh-driver tick (rows with
    a steps JSON — see tools/refresh.py). None when no [sync] bucket is
    configured or no tick has recorded yet; a legacy runs table without
    the steps column reads as no ticks."""
    if not config.SYNC_BUCKET:
        return None
    try:
        row = db.execute("SELECT steps FROM runs WHERE steps IS NOT NULL "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    sync = (json.loads(row[0]) or {}).get("sync") or {}
    out = {}
    if sync.get("synced_at"):
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(sync["synced_at"])).total_seconds() / 60
        out["synced_age_minutes"] = int(age)
    if not sync.get("ok", True):
        out["note"] = "replica sync failing — serving old data remotely"
    elif out.get("synced_age_minutes", 0) > REPLICA_STALL_MINUTES:
        out["note"] = (f"replica stale — last confirmed sync "
                       f"{out['synced_age_minutes']} minutes ago")
    return out or None

def _run_health(db):
    """Health of the last index run, from the runs table the indexer keeps.
    None on a legacy DB without one (schema-stable for old callers)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                      "AND name='runs'").fetchone():
        return None
    row = db.execute("SELECT started, finished, status, sources FROM runs "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    started, finished, status, sources_json = row
    ref = datetime.fromisoformat(finished or started)
    age = (datetime.now(timezone.utc) - ref).total_seconds() / 60
    health = {"last_run": finished or started, "age_minutes": int(age),
              "status": status if finished else "unfinished"}
    failing = {name: info.get("error", "failed")
               for name, info in json.loads(sources_json or "{}").items()
               if not info.get("ok", True)}
    if failing:
        health["failing_sources"] = failing
    if not finished or age > STALL_MINUTES:
        health["note"] = ("index refresh appears stalled — last run "
                          f"{int(age)} minutes ago")
    elif status == "aborted":
        health["note"] = "last index run aborted (is Ollama running?)"
    return health

_GROUP_DIMS = ("day", "source", "location", "domain")

def _group_value(dim, src, ts, loc, meta_json):
    if dim == "day":            # the chunk's LOCAL calendar day, like bounds
        if not ts:
            return ""
        return (datetime.fromisoformat(ts.replace("Z", "+00:00"))
                .astimezone().date().isoformat())
    if dim == "source":
        return src
    if dim == "domain":         # browser: site; everything else: location
        if src == "browser" and meta_json:
            from urllib.parse import urlsplit
            url = (json.loads(meta_json) or {}).get("url", "")
            host = (urlsplit(url).hostname or "") if url else ""
            if host:
                return host.removeprefix("www.")
        return loc or ""
    return loc or ""            # "location"

@mcp.tool()
def list_window(since: str = "", until: str = "", source: str = "",
                location: str = "", limit: int = 50, offset: int = 0,
                include_undated: bool = False, group_by: str = "") -> str:
    """Exhaustive chronological listing (newest first) of everything in a time
    window — no semantic ranking, no sampling. The right tool for "everything
    from <day/week>"; use search_history when relevance matters more than
    completeness. Bounds work like search_history's since/until (bare dates =
    the user's local day); at least one bound is required. Results are compact
    pointers {id, source, timestamp, location, text (truncated)} — pass an id
    to expand() to read one in full. `total` is the full match count; page
    with offset (limit caps at 200).

    group_by: comma-separated dimensions from day | source | location |
    domain — returns aggregate `groups` (sorted by count desc, each with
    count/earliest/latest) instead of `results`. One call answers "which days
    were active", "which browser profiles/sites dominated" (source='browser',
    group_by='domain,day'), "which projects saw work". `day` is the user's
    local calendar day; `domain` is the site for browser chunks and falls
    back to location elsewhere. limit caps the group list; a cut list sets
    groups_truncated.

    For day/week activity summaries, read the precomputed daily digests
    first: source='digest' (one chunk per day per stream: browser profile /
    claude / shell; full rollup in meta via expand). Drill into raw chunks
    only where a digest points."""
    if not since and not until:
        return json.dumps({"error": "list_window requires since and/or until"})
    since, until, err = _parse_bounds(since, until)
    if err:
        return err
    dims = [d.strip() for d in group_by.split(",") if d.strip()]
    bad = [d for d in dims if d not in _GROUP_DIMS]
    if bad:
        return json.dumps({"error": f"unknown group_by dimension(s) {bad}; "
                           f"valid: {', '.join(_GROUP_DIMS)}"})
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where, params = _window_where(since, until, source, location,
                                  include_undated)
    db = _db()
    total = db.execute(f"SELECT COUNT(*) FROM chunks WHERE {where}",
                       params).fetchone()[0]
    window = {"since": since or None, "until": until or None}

    if dims:
        buckets = {}
        for src, ts, loc, meta_json in db.execute(
                f"SELECT source, timestamp, location, meta FROM chunks "
                f"WHERE {where}", params):
            key = tuple(_group_value(d, src, ts, loc, meta_json) for d in dims)
            b = buckets.get(key)
            if b is None:
                buckets[key] = [1, ts, ts]
            else:
                b[0] += 1
                if ts and (not b[1] or ts < b[1]):
                    b[1] = ts
                if ts > b[2]:
                    b[2] = ts
        ordered = sorted(buckets.items(), key=lambda kv: (-kv[1][0], kv[0]))
        groups = [{**dict(zip(dims, key)), "count": n,
                   "earliest": lo or None, "latest": hi or None}
                  for key, (n, lo, hi) in ordered[:limit]]
        out = {"group_by": dims, "total": total, "window": window,
               "groups": groups}
        if len(ordered) > limit:
            out["groups_truncated"] = True
        return json.dumps(out)

    rows = db.execute(
        f"""SELECT id, source, timestamp, location, text FROM chunks
            WHERE {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset)).fetchall()
    results = [{"id": i, "source": src, "timestamp": ts, "location": loc,
                "text": text[:160]} for i, src, ts, loc, text in rows]
    return json.dumps({"count": len(results), "total": total,
                       "window": window, "results": results})

@mcp.tool()
def expand(id: str, context: int = 5) -> str:
    """The reading view for one search_history / list_window result: the full
    chunk plus source-aware surroundings, fetched live from the backing store
    when it still exists (context_source: "live") else reconstructed from the
    index ("index"). Per source: claude -> the ±context conversation turns
    around the hit; git -> the full commit message + file stats; browser ->
    that profile's other visits the same local day; obsidian -> the whole
    note; appusage -> the day's full per-app seconds; calendar -> that
    local day's full agenda across calendars; shell -> the commands
    around its latest run when atuin recorded it (with cwd + exit codes),
    else no context. context caps at 25."""
    db = _db()
    row = db.execute("SELECT id, source, timestamp, location, text, meta "
                     "FROM chunks WHERE id = ?", (id,)).fetchone()
    if not row:
        return json.dumps({"error": f"no chunk with id {id!r}"})
    cid, source, ts, loc, text, meta_json = row
    chunk = {"id": cid, "source": source, "timestamp": ts, "location": loc,
             "text": text, "meta": json.loads(meta_json) if meta_json else {}}
    n = max(0, min(int(context), 25))
    handler = _EXPANDERS.get(source)
    try:
        ctx, ctx_src = handler(db, chunk, n) if handler else (None, None)
    except Exception as e:
        # Context is best-effort garnish; the chunk itself must always come
        # back (a replica without the backing stores, a broken store, a
        # missing binary — none of these may fail the read).
        ctx, ctx_src = {"note": f"context unavailable: {e}"}, None
    return json.dumps({"chunk": chunk, "context": ctx,
                       "context_source": ctx_src})

if __name__ == "__main__":
    mcp.run()
