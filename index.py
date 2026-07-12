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
import argparse, json, sqlite3, subprocess, sys, time
from datetime import datetime, timedelta, timezone
import sqlite_vec
import requests
import config
from sources import claude, shell, appusage, browser, git, obsidian, calendar, digest

# digest runs last: it reads this run's freshly committed claude chunks.
ALL_SOURCES = [claude, shell, appusage, browser, git, obsidian, calendar, digest]

def _enabled():
    if config.ENABLED_SOURCES is None:   # no [sources].enabled -> all
        return ALL_SOURCES
    by_name = {m.__name__.rsplit(".", 1)[-1]: m for m in ALL_SOURCES}
    unknown = [n for n in config.ENABLED_SOURCES if n not in by_name]
    if unknown:
        sys.exit(f"config: unknown source(s) in [sources].enabled: "
                 f"{', '.join(unknown)}; known: {', '.join(by_name)}")
    return [by_name[n] for n in config.ENABLED_SOURCES]

SOURCES = _enabled()
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
    p.add_argument("--no-run-record", action="store_true",
                   help="(refresh driver) don't write a runs row — the "
                        "driver records sub-runs in its own steps record")
    return p.parse_args()

def pick_sources(only: str | None):
    if not only:
        return SOURCES
    picked = [s for s in SOURCES if source_name(s) == only]
    if not picked:
        if any(source_name(s) == only for s in ALL_SOURCES):
            sys.exit(f"source {only!r} is disabled by [sources].enabled "
                     f"in the config file")
        sys.exit(f"unknown source {only!r}; available: "
                 + ", ".join(source_name(s) for s in SOURCES))
    return picked

def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts in one call. Returns embeddings in input order."""
    r = requests.post(config.OLLAMA, json={"model": config.EMBED_MODEL, "input": texts}, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]

RUNS_KEEP = 200          # newest run records retained

def setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS chunks(
        id TEXT PRIMARY KEY, text TEXT, source TEXT,
        timestamp TEXT, location TEXT, meta TEXT)""")
    db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
        id TEXT PRIMARY KEY, embedding FLOAT[{config.DIM}])""")
    ensure_runs(db)

def ensure_runs(db):
    """Run health lives in the DB (durable, queryable, backed up): the MCP
    server surfaces the newest row via history_stats so failures reach the
    user through the tools they already use, not an unread /tmp log. Split
    out of setup() so the refresh driver can use it on a plain connection
    (no vec extension)."""
    db.execute("""CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started TEXT NOT NULL, finished TEXT,
        status TEXT NOT NULL,
        embedded INTEGER, refreshed INTEGER, pruned INTEGER, failed INTEGER,
        sources TEXT)""")

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def _should_notify(statuses) -> bool:
    """Fire exactly once per incident: on the SECOND consecutive aborted run
    (newest first). One flaky tick doesn't ping; a third abort doesn't
    re-ping."""
    return (len(statuses) >= 2 and statuses[0] == "aborted"
            and statuses[1] == "aborted"
            and (len(statuses) < 3 or statuses[2] != "aborted"))

def _notify(message: str):
    if sys.platform != "darwin":
        return
    try:
        safe = message.replace('"', "'")
        subprocess.run(["osascript", "-e",
                        f'display notification "{safe}" with title "history-rag"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass                      # failing to notify must never break a run

def _maybe_notify(db, message: str):
    """The push channel for silent-freeze failures ([health] notify,
    default on). Debounced via the runs table."""
    enabled = str(config.get("health", "notify", "CLAUDE_RAG_NOTIFY",
                             True)).lower() not in ("false", "0", "")
    if not enabled:
        return
    statuses = [r[0] for r in db.execute(
        "SELECT status FROM runs ORDER BY id DESC LIMIT 3")]
    if _should_notify(statuses):
        _notify(message)

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
        print("No chunks survived the filter. If the claude source is the "
              "surprise, tools/inspect-sessions.py shows the raw JSONL shape.")
    else:
        print(f"\n{n} kept so far. Looks right? Run without --dry-run.")

def prune_stale(db, src_cols: set, keep_ids: set, min_ts: str = "") -> int:
    """Delete stored chunks belonging to `src_cols` whose id wasn't yielded this
    run. Only called for a source that completed cleanly and yielded something,
    so an empty or broken source can never wipe its own history.

    `min_ts` (UTC ISO) bounds deletion to chunks stamped at or after it. A
    source whose backing store forgets legitimately (a calendar account's
    bounded sync window, a rolling occurrence cache) declares
    PRUNE_WINDOW_DAYS: not-yielded-anymore then only means stale within that
    window — anything older, or undated, is archive the index has outlived."""
    q = ",".join("?" * len(src_cols))
    sql = f"SELECT id FROM chunks WHERE source IN ({q})"
    params = list(src_cols)
    if min_ts:
        sql += " AND timestamp >= ?"
        params.append(min_ts)
    rows = db.execute(sql, params)
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
    if args.prune and args.source == "digest":
        sys.exit("--prune --source digest would delete every settled digest: "
                 "the source only yields recent days by design, so every older "
                 "day counts as 'no longer yielded'. A wrong recent digest "
                 "fixes itself on the next run; anything older is archive.")
    if args.rebuild and args.source:
        sys.exit("--rebuild wipes the WHOLE index then reindexes; combined "
                 "with --source it would destroy every other source's data, "
                 "including archived chunks whose backing data is gone. "
                 "Rebuild without --source, or run the source incrementally.")
    sources = pick_sources(args.source)
    if args.dry_run:
        dry_run(sources)
        return

    db = sqlite3.connect(config.DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    if args.rebuild:
        db.execute("DROP TABLE IF EXISTS chunks")
        db.execute("DROP TABLE IF EXISTS vec_chunks")
        db.execute("DROP TABLE IF EXISTS index_meta")
        db.execute("DROP TABLE IF EXISTS runs")
    setup(db)
    record = not args.no_run_record
    try:
        # Refuse to write wrong-model vectors; stamp fresh/legacy DBs.
        config.check_stamp(db, stamp_if_missing=True)
    except config.StampMismatch as e:
        # Record the refusal so history_stats surfaces it as run health.
        if record:
            now = _utcnow()
            db.execute("INSERT INTO runs(started, finished, status, sources) "
                       "VALUES (?,?,'aborted',?)",
                       (now, now, json.dumps({"_stamp": {"ok": False,
                                                         "error": str(e)}})))
            db.commit()
            _maybe_notify(db, "embedding model/config mismatch — indexing is stopped")
        sys.exit(str(e))

    print(f"=== run {datetime.now().astimezone().isoformat(timespec='seconds')}")
    run_id = None
    if record:
        run_id = db.execute("INSERT INTO runs(started, status) VALUES (?, 'aborted')",
                            (_utcnow(),)).lastrowid
        db.commit()

    # id -> (text, timestamp). Changed text re-embeds (e.g. today's still-
    # growing app-usage total); same text with a changed timestamp gets a
    # metadata-only update — the embedding is of the text, so no re-embed.
    existing = {r[0]: (r[1], r[2])
                for r in db.execute("SELECT id, text, timestamp FROM chunks")}
    new = failed = pruned = refreshed = 0

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
    srcinfo, aborted = {}, False
    try:
        for src in sources:
            name, t0 = source_name(src), time.monotonic()
            seen_ids, src_cols = set(), set()
            yielded, new0, failed0, refreshed0 = 0, new, failed, refreshed
            ok, err_text, batch = True, "", []
            try:
                for cid, text, rec in src.iter_chunks():
                    seen_ids.add(cid)
                    src_cols.add(rec["source"])
                    yielded += 1
                    prev = existing.get(cid)
                    if prev and prev[0] == text:
                        if prev[1] != rec.get("timestamp", ""):
                            db.execute(
                                "UPDATE chunks SET timestamp=?, location=?, meta=? WHERE id=?",
                                (rec.get("timestamp", ""), rec.get("location", ""),
                                 json.dumps(rec.get("meta", {})), cid))
                            refreshed += 1
                        continue
                    batch.append((cid, text, rec))
                    if len(batch) >= BATCH_SIZE:
                        flush(batch)
                        batch = []
                flush(batch)
            except requests.exceptions.ConnectionError:
                raise
            except Exception as e:
                ok, err_text = False, str(e)
                print(f"{name}: source failed after {yielded} chunks -> {e}",
                      file=sys.stderr)
            n_pruned = 0
            if args.prune and ok and seen_ids:
                window = getattr(src, "PRUNE_WINDOW_DAYS", None)
                min_ts = ((datetime.now(timezone.utc)
                           - timedelta(days=window)).isoformat()
                          if window else "")
                n_pruned = prune_stale(db, src_cols, seen_ids, min_ts)
                pruned += n_pruned
            status = "" if ok else "  [FAILED, partial]"
            extra = f", {n_pruned} pruned" if n_pruned else ""
            if refreshed - refreshed0:
                extra += f", {refreshed - refreshed0} refreshed"
            print(f"{name}: {yielded} chunks, {new - new0} embedded, "
                  f"{failed - failed0} skipped{extra}, "
                  f"{time.monotonic() - t0:.1f}s{status}")
            info = {"chunks": yielded, "embedded": new - new0,
                    "failed": failed - failed0, "ok": ok}
            if refreshed - refreshed0:
                info["refreshed"] = refreshed - refreshed0
            if n_pruned:
                info["pruned"] = n_pruned
            if err_text:
                info["error"] = f"{name}: {err_text}"
            srcinfo[name] = info
    except requests.exceptions.ConnectionError:
        aborted = True
        print("Ollama not reachable on :11434 — is it running? Stopping.",
              file=sys.stderr)

    run_status = ("aborted" if aborted else
                  "partial" if any(not i["ok"] for i in srcinfo.values())
                  else "ok")
    if record:
        db.execute("UPDATE runs SET finished=?, status=?, embedded=?, refreshed=?, "
                   "pruned=?, failed=?, sources=? WHERE id=?",
                   (_utcnow(), run_status, new, refreshed, pruned, failed,
                    json.dumps(srcinfo), run_id))
        db.execute("DELETE FROM runs WHERE id NOT IN "
                   "(SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (RUNS_KEEP,))
        db.commit()
        if run_status == "aborted":
            _maybe_notify(db, "index refresh aborted twice — is Ollama running?")
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    extra = f", {pruned} pruned" if pruned else ""
    print(f"done. {new} embedded (new or changed), {failed} skipped{extra}, "
          f"{total} total in {config.DB_PATH}")

if __name__ == "__main__":
    main()
