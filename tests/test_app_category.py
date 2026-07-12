"""App categories: LSApplicationCategoryType resolution through the
app_meta cache (misses via mdfind+plistlib, NULLs cached, 7-day recheck),
the day rollup with the honest 'other' bucket, and surfacing in per-app
meta, the day-shape sentence, and the report line."""
import datetime, plistlib, sqlite3, subprocess, time, types

from appusage import store, report
from sources import appusage as appusage_src
from tests.helpers import mem_store

B = datetime.datetime(2025, 1, 6, 9, 0).timestamp()    # Mon 09:00 local


def _fake_app(tmp_path, name, category):
    """A real .app skeleton with a real Info.plist."""
    app = tmp_path / f"{name}.app" / "Contents"
    app.mkdir(parents=True)
    info = {"CFBundleName": name}
    if category is not None:
        info["LSApplicationCategoryType"] = category
    with open(app / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    return str(app.parent)


def _mdfind(monkeypatch, results, calls):
    """Fake mdfind: bundle id -> app path ('' = no hit). Records calls."""
    def run(cmd, **kw):
        assert cmd[0] == "mdfind"
        calls.append(cmd[1])
        bid = cmd[1].split("'")[1]
        return types.SimpleNamespace(returncode=0,
                                     stdout=results.get(bid, "") + "\n")
    monkeypatch.setattr(subprocess, "run", run)


# ── resolver + cache ─────────────────────────────────────────────────────────

def test_resolves_and_caches_with_prefix_stripped(tmp_path, monkeypatch):
    path = _fake_app(tmp_path, "Super", "public.app-category.developer-tools")
    calls = []
    _mdfind(monkeypatch, {"com.x.super": path}, calls)
    db = mem_store()
    assert store.categories(db, {"com.x.super"}) == {"com.x.super":
                                                     "developer-tools"}
    assert len(calls) == 1
    # cache hit: no subprocess at all
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                            "cache hit must not fork")))
    assert store.categories(db, {"com.x.super"}) == {"com.x.super":
                                                     "developer-tools"}


def test_missing_key_and_missing_app_cache_null(tmp_path, monkeypatch):
    path = _fake_app(tmp_path, "Helium", None)      # no category key
    calls = []
    _mdfind(monkeypatch, {"com.x.helium": path}, calls)   # com.x.gone: no hit
    db = mem_store()
    assert store.categories(db, {"com.x.helium", "com.x.gone"}) == {
        "com.x.helium": None, "com.x.gone": None}
    rows = dict(db.execute("SELECT bundle_id, category FROM app_meta"))
    assert rows == {"com.x.helium": None, "com.x.gone": None}
    # cached NULLs don't re-resolve inside the window
    store.categories(db, {"com.x.helium", "com.x.gone"})
    assert len(calls) == 2


def test_null_rechecked_only_past_window(tmp_path, monkeypatch):
    db = mem_store()
    fresh, stale = time.time(), time.time() - store.CATEGORY_RECHECK - 1
    db.execute("INSERT INTO app_meta VALUES ('com.x.fresh', NULL, ?)", (fresh,))
    db.execute("INSERT INTO app_meta VALUES ('com.x.stale', NULL, ?)", (stale,))
    path = _fake_app(tmp_path, "Late", "public.app-category.video")
    calls = []
    _mdfind(monkeypatch, {"com.x.stale": path}, calls)
    out = store.categories(db, {"com.x.fresh", "com.x.stale"})
    assert out == {"com.x.fresh": None, "com.x.stale": "video"}
    assert calls == ["kMDItemCFBundleIdentifier == 'com.x.stale'"]


def test_resolver_failure_degrades_to_null(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no mdfind")))
    db = mem_store()
    assert store.categories(db, {"com.x.y"}) == {"com.x.y": None}


# ── rollup ───────────────────────────────────────────────────────────────────

APPS = {"Super":  {"seconds": 5400.0, "bundle_id": "com.x.super"},
        "Zoom":   {"seconds": 720.0, "bundle_id": "com.x.zoom"},
        "Helium": {"seconds": 7200.0, "bundle_id": "com.x.helium"},
        "Legacy": {"seconds": 300.0, "bundle_id": None}}
CATS = {"com.x.super": "developer-tools", "com.x.zoom": "video",
        "com.x.helium": None}


def test_rollup_sums_known_unknown_unbundled():
    assert store.category_rollup(APPS, CATS) == {
        "developer-tools": 5400.0, "video": 720.0, "other": 7500.0}


def test_rollup_none_when_nothing_resolves():
    apps = {k: v for k, v in APPS.items() if k in ("Helium", "Legacy")}
    assert store.category_rollup(apps, CATS) is None


# ── surfacing ────────────────────────────────────────────────────────────────

def _seed_day(db):
    db.execute("INSERT INTO segments(app, bundle_id, start_ts, end_ts, closed)"
               " VALUES ('Super', 'com.x.super', ?, ?, 1)", (B, B + 5400))
    db.execute("INSERT INTO segments(app, bundle_id, start_ts, end_ts, closed)"
               " VALUES ('Helium', 'com.x.helium', ?, ?, 1)",
               (B + 5460, B + 12660))
    db.commit()


def test_chunk_meta_and_dayshape_sentence(tmp_path, monkeypatch):
    monkeypatch.setattr(appusage_src, "APPUSAGE_DB", __file__)  # exists-check
    monkeypatch.setattr(store, "connect", lambda: db)
    db = mem_store()
    _seed_day(db)
    path = _fake_app(tmp_path, "Super", "public.app-category.developer-tools")
    _mdfind(monkeypatch, {"com.x.super": path}, [])
    chunks = {rec["meta"].get("app", "day"): (text, rec["meta"])
              for _, text, rec in appusage_src.iter_chunks()}
    assert chunks["Super"][1]["category"] == "developer-tools"
    assert "category" not in chunks["Helium"][1]        # unresolved: absent
    assert "Time by category: other 2h 0m, developer-tools 1h 30m." \
        in chunks["day"][0]                             # seconds desc, other kept


def test_all_unknown_day_omits_sentence(monkeypatch):
    monkeypatch.setattr(appusage_src, "APPUSAGE_DB", __file__)
    monkeypatch.setattr(store, "connect", lambda: db)
    db = mem_store()
    _seed_day(db)
    _mdfind(monkeypatch, {}, [])                        # nothing resolves
    texts = [text for _, text, rec in appusage_src.iter_chunks()
             if "app" not in rec["meta"]]
    assert texts and "Time by category" not in texts[0]


def test_report_category_line():
    assert report.category_line(store.category_rollup(APPS, CATS)) == \
        "other 2h 5m · developer-tools 1h 30m · video 12m"
