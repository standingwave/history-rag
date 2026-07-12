"""Shared test helpers: script loading for non-package tools (dashed
filenames), the fabricated-source index harness, and appusage store
builders."""
import importlib.util, pathlib, sqlite3, types

import sqlite_vec

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_script(relpath: str, name: str = ""):
    """Import a repo script that lives outside a package (tools/*.py,
    deploy/lambda/app.py) as a module."""
    p = _ROOT / relpath
    spec = importlib.util.spec_from_file_location(
        name or p.stem.replace("-", "_"), p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── fabricated-source index harness (Tier 2) ────────────────────────────────

def mk_source(name, chunks, explode=False):
    mod = types.SimpleNamespace()
    mod.__name__ = f"sources.{name}"
    def iter_chunks():
        yield from chunks
        if explode:
            raise RuntimeError("boom")
    mod.iter_chunks = iter_chunks
    return mod


def rec(src, ts="", loc="x", meta=None):
    return {"source": src, "timestamp": ts, "location": loc, "meta": meta or {}}


def run_index(monkeypatch, sources, argv=()):
    import index
    monkeypatch.setattr(index, "SOURCES", sources)
    monkeypatch.setattr(index, "ALL_SOURCES", sources)
    monkeypatch.setattr("sys.argv", ["index.py", *argv])
    index.main()


def open_db(path):
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    return db


# ── appusage store builders ──────────────────────────────────────────────────

def mem_store():
    from appusage import store
    db = sqlite3.connect(":memory:")
    store.setup(db)
    return db


def seg(db, app, start, end, bundle=None):
    db.execute("INSERT INTO segments(app, bundle_id, start_ts, end_ts, closed) "
               "VALUES (?,?,?,?,1)", (app, bundle, start, end))
