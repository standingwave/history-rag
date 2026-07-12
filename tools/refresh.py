#!/usr/bin/env python3
"""One entrypoint for the launchd refresh chain: index → prune → backup →
sync (wip/SPEC-refresh-driver.md). Each step runs regardless of earlier
failures — every step is already safe on a stale or unchanged DB — and
their outcomes land as a `steps` JSON column on the index run's row, so
the existing run-health channel covers the whole chain, not just the
index. One tick, one row: prune sub-runs pass --no-run-record.

Run:    ~/.claude/rag-venv/bin/python tools/refresh.py
Config: [refresh] prune = ["calendar"]   (sources pruned each run)
        env CLAUDE_RAG_REFRESH_PRUNE (colon-separated)
"""
import importlib.util, json, os, sqlite3, sys, time, traceback
from datetime import datetime, timezone

_TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TOOLS))
import config
import index


def _load_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_TOOLS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backup = _load_tool("backup", "backup.py")
sync_s3 = _load_tool("sync_s3", "sync-s3.py")


def prune_sources() -> list:
    """[refresh] prune, validated at startup: an unknown source, or digest
    (index.py refuses to prune it), fails fast with a config error — not a
    step that fails on every tick forever."""
    v = config.get("refresh", "prune", "CLAUDE_RAG_REFRESH_PRUNE", [])
    items = [s for s in (str(x).strip() for x in
                         (v.split(":") if isinstance(v, str) else v)) if s]
    known = {index.source_name(s) for s in index.ALL_SOURCES}
    bad = [s for s in items if s not in known or s == "digest"]
    if bad:
        sys.exit(f"config: [refresh] prune has {', '.join(bad)}; prunable "
                 f"sources: {', '.join(sorted(known - {'digest'}))}")
    return items


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _index_argv(argv: list):
    """In-process index.main() with argv override (precedent:
    tools/eval-model.py)."""
    saved = sys.argv
    sys.argv = ["index.py"] + argv
    try:
        index.main()
    finally:
        sys.argv = saved


def _step(steps: dict, name: str, fn):
    """Run one step isolated. SystemExit is caught too: the steps exit via
    sys.exit() on real failures (stamp mismatch, missing boto3), and
    SystemExit is not an Exception — uncaught, it would kill the rest of
    the chain and silently skip backup + sync."""
    t0 = time.monotonic()
    try:
        result = fn()
        steps[name] = {"ok": True, "secs": round(time.monotonic() - t0, 1)}
        return result
    except (Exception, SystemExit) as e:
        traceback.print_exc()
        steps[name] = {"ok": False, "secs": round(time.monotonic() - t0, 1),
                       "error": str(e) or type(e).__name__}
        return None


def _connect():
    db = sqlite3.connect(config.DB_PATH, timeout=30)
    index.ensure_runs(db)
    return db


def _ensure_steps_column(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
    if "steps" not in cols:
        try:
            db.execute("ALTER TABLE runs ADD COLUMN steps TEXT")
        except sqlite3.OperationalError as e:   # lost a concurrent ALTER race
            if "duplicate column" not in str(e):
                raise


def _maybe_notify(db, steps: dict):
    """Backup/sync failing on two consecutive driver ticks notifies once —
    the same debounce shape as index's double-abort rule. Rows without
    steps (manual index runs) don't count as ticks."""
    enabled = str(config.get("health", "notify", "CLAUDE_RAG_NOTIFY",
                             True)).lower() not in ("false", "0", "")
    if not enabled:
        return
    ticks = [json.loads(r[0]) for r in db.execute(
        "SELECT steps FROM runs WHERE steps IS NOT NULL "
        "ORDER BY id DESC LIMIT 3")]
    for name in ("backup", "sync"):
        seq = ["ok" if t.get(name, {}).get("ok", True) else "aborted"
               for t in ticks]
        if index._should_notify(seq):
            index._notify(f"refresh: {name} failed twice — check the log")


def _summary(steps: dict, status, embedded, secs: float) -> str:
    def part(name, detail=""):
        s = steps.get(name)
        if not s:
            return None
        if not s["ok"]:
            return f"{name} FAILED ({s['error'][:60]})"
        return f"{name} {detail}" if detail else f"{name} ok"

    parts = [part("index", status + (f" ({embedded} embedded)"
                                     if embedded is not None else "")),
             part("prune"),
             part("backup", steps.get("backup", {}).get("note", "")),
             part("sync", steps.get("sync", {}).get("note", ""))]
    return ("refresh: " + " · ".join(p for p in parts if p)
            + f" · {int(secs)}s")


def main():
    prune = prune_sources()          # fail fast before any step runs
    t0 = time.monotonic()
    print("=== refresh "
          f"{datetime.now().astimezone().isoformat(timespec='seconds')}",
          flush=True)
    db = _connect()
    before = db.execute("SELECT COALESCE(MAX(id), 0) FROM runs").fetchone()[0]
    db.close()

    steps = {}
    _step(steps, "index", lambda: _index_argv([]))
    if prune:
        def run_prunes():
            for s in prune:
                _index_argv(["--prune", "--source", s, "--no-run-record"])
        _step(steps, "prune", run_prunes)
    bres = _step(steps, "backup", backup.main)
    sres = _step(steps, "sync", sync_s3.main)

    if bres is not None:
        steps["backup"]["note"] = ("written" if "written" in bres.values()
                                   else "current")
    if sres is not None:
        action = sres["action"]
        steps["sync"]["note"] = {
            "pushed": f"pushed {sres.get('bytes', 0) / 1e6:.0f}MB",
            "unchanged": "confirmed current"}.get(action, action)
        # "The replica was confirmed current at this time" — the number
        # staleness questions need. Never stamped on the config/no-DB skips.
        if action in ("pushed", "unchanged"):
            steps["sync"]["synced_at"] = _utcnow()

    db = _connect()
    row = db.execute("SELECT id, status, embedded FROM runs WHERE id > ? "
                     "ORDER BY id DESC LIMIT 1", (before,)).fetchone()
    if row is None:                  # index hard-crashed before its INSERT
        now = _utcnow()
        rid = db.execute("INSERT INTO runs(started, finished, status) "
                         "VALUES (?, ?, 'aborted')", (now, now)).lastrowid
        row = (rid, "aborted", None)
    _ensure_steps_column(db)
    db.execute("UPDATE runs SET steps=? WHERE id=?",
               (json.dumps(steps), row[0]))
    db.commit()
    _maybe_notify(db, steps)
    db.close()
    print(_summary(steps, row[1], row[2], time.monotonic() - t0), flush=True)


if __name__ == "__main__":
    main()
