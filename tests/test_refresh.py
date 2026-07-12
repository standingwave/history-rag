"""Refresh driver: step isolation (including SystemExit), one runs row per
tick with the steps JSON on it, prune config validation and argv, synced_at
stamping rules, the notify debounce, the summary line, and the replica
health field the server derives from steps."""
import datetime, importlib.util, json, pathlib, re, sqlite3, sys

import pytest

import config, index

_spec = importlib.util.spec_from_file_location(
    "refresh", pathlib.Path(__file__).resolve().parent.parent
    / "tools" / "refresh.py")
refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh)


@pytest.fixture(autouse=True)
def _no_prune_by_default(monkeypatch):
    monkeypatch.setenv("CLAUDE_RAG_REFRESH_PRUNE", "")


def fake_index(monkeypatch, calls=None, status="ok", embedded=3, crash=None):
    """index.main stand-in: records argv, writes its runs row like the real
    one — unless told to crash, or invoked with --no-run-record."""
    calls = calls if calls is not None else []

    def main():
        calls.append(sys.argv[1:])
        if crash == "systemexit":
            sys.exit("stamp mismatch — refusing to mix models")
        if crash == "hard":
            raise RuntimeError("hard crash before the runs INSERT")
        if "--no-run-record" in sys.argv:
            return
        db = sqlite3.connect(config.DB_PATH)
        index.ensure_runs(db)
        now = "2026-07-12T00:00:00+00:00"
        db.execute("INSERT INTO runs(started, finished, status, embedded) "
                   "VALUES (?,?,?,?)", (now, now, status, embedded))
        db.commit()
        db.close()

    monkeypatch.setattr(index, "main", main)
    return calls


def stub_steps(monkeypatch, backup_ret=None, sync_ret=None,
               backup_exc=None, sync_exc=None):
    def b():
        if backup_exc:
            raise backup_exc
        return backup_ret or {"history-rag": "current", "appusage": "current"}

    def s():
        if sync_exc:
            raise sync_exc
        return sync_ret or {"action": "unconfigured"}

    monkeypatch.setattr(refresh.backup, "main", b)
    monkeypatch.setattr(refresh.sync_s3, "main", s)


def rows(path):
    db = sqlite3.connect(path)
    out = db.execute("SELECT id, status, steps FROM runs ORDER BY id").fetchall()
    db.close()
    return out


def test_backup_failure_isolated(scratch_db, monkeypatch, capsys):
    fake_index(monkeypatch, embedded=2)
    stub_steps(monkeypatch, backup_exc=RuntimeError("disk full"),
               sync_ret={"action": "pushed", "bytes": 174_000_000})
    refresh.main()
    r = rows(scratch_db)
    assert len(r) == 1
    steps = json.loads(r[0][2])
    assert steps["backup"] == {"ok": False, "secs": steps["backup"]["secs"],
                               "error": "disk full"}
    assert steps["sync"]["ok"] and steps["sync"]["note"] == "pushed 174MB"
    summary = capsys.readouterr().out.strip().splitlines()[-1]
    assert "backup FAILED (disk full)" in summary
    assert "sync pushed 174MB" in summary


def test_systemexit_isolated_and_rowless_tick_gets_a_row(
        scratch_db, monkeypatch):
    fake_index(monkeypatch, crash="systemexit")     # exits before its INSERT
    stub_steps(monkeypatch)
    refresh.main()
    r = rows(scratch_db)
    assert len(r) == 1 and r[0][1] == "aborted"     # minimal row created
    steps = json.loads(r[0][2])
    assert steps["index"]["ok"] is False
    assert "stamp mismatch" in steps["index"]["error"]
    assert steps["backup"]["ok"] and steps["sync"]["ok"]   # chain continued


def test_hard_crash_isolated(scratch_db, monkeypatch):
    fake_index(monkeypatch, crash="hard")
    stub_steps(monkeypatch)
    refresh.main()
    (rid, status, steps_json), = rows(scratch_db)
    assert status == "aborted"
    assert json.loads(steps_json)["sync"]["ok"]


def test_prune_argv_and_one_row_per_tick(scratch_db, monkeypatch):
    calls = fake_index(monkeypatch)
    stub_steps(monkeypatch)
    monkeypatch.setenv("CLAUDE_RAG_REFRESH_PRUNE", "calendar")
    refresh.main()
    assert calls == [[], ["--prune", "--source", "calendar",
                          "--no-run-record"]]
    r = rows(scratch_db)
    assert len(r) == 1                              # prune added no rows
    assert json.loads(r[0][2])["prune"]["ok"]


def test_empty_prune_config_skips_the_step(scratch_db, monkeypatch):
    fake_index(monkeypatch)
    stub_steps(monkeypatch)
    refresh.main()
    assert "prune" not in json.loads(rows(scratch_db)[0][2])


@pytest.mark.parametrize("bad", ["digest", "nonesuch", "calendar:digest"])
def test_prune_list_validated_before_any_step(scratch_db, monkeypatch, bad):
    calls = fake_index(monkeypatch)
    stub_steps(monkeypatch)
    monkeypatch.setenv("CLAUDE_RAG_REFRESH_PRUNE", bad)
    with pytest.raises(SystemExit) as e:
        refresh.main()
    assert "[refresh] prune" in str(e.value.code)
    assert calls == []                              # nothing ran


def test_synced_at_stamped_only_when_replica_confirmed(scratch_db, monkeypatch):
    fake_index(monkeypatch)
    for action, stamped in (("pushed", True), ("unchanged", True),
                            ("unconfigured", False), ("no-index", False)):
        stub_steps(monkeypatch, sync_ret={"action": action, "bytes": 1})
        refresh.main()
        sync = json.loads(rows(scratch_db)[-1][2])["sync"]
        assert ("synced_at" in sync) is stamped, action
    assert sync["note"] == "no-index"


def test_notify_debounce_two_consecutive_sync_failures(scratch_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_RAG_NOTIFY", "true")
    notes = []
    monkeypatch.setattr(index, "_notify", notes.append)
    fake_index(monkeypatch)
    stub_steps(monkeypatch, sync_exc=RuntimeError("boom"))
    refresh.main()                                  # first failure: quiet
    assert notes == []
    refresh.main()                                  # second: ping once
    assert len(notes) == 1 and "sync" in notes[0]
    refresh.main()                                  # third: no re-ping
    assert len(notes) == 1


def test_summary_line_format(scratch_db, monkeypatch, capsys):
    fake_index(monkeypatch, embedded=12)
    stub_steps(monkeypatch,
               sync_ret={"action": "pushed", "bytes": 174_000_000})
    monkeypatch.setenv("CLAUDE_RAG_REFRESH_PRUNE", "calendar")
    refresh.main()
    summary = capsys.readouterr().out.strip().splitlines()[-1]
    assert re.fullmatch(
        r"refresh: index ok \(12 embedded\) · prune ok · backup current "
        r"· sync pushed 174MB · \d+s", summary)


# ── replica health (server side) ─────────────────────────────────────────────

def _tick_row(path, sync_step):
    db = sqlite3.connect(path)
    index.ensure_runs(db)
    refresh._ensure_steps_column(db)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    db.execute("INSERT INTO runs(started, finished, status, steps) "
               "VALUES (?,?,'ok',?)", (now, now,
                                       json.dumps({"sync": sync_step})))
    db.commit()
    return db


def test_replica_health_ages_and_notes(scratch_db, monkeypatch):
    import server
    monkeypatch.setattr(config, "SYNC_BUCKET", "bkt")
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(minutes=200)).isoformat()
    db = _tick_row(scratch_db, {"ok": True, "synced_at": old})
    r = server._replica_health(db)
    assert r["synced_age_minutes"] >= 199 and "stale" in r["note"]
    db.close()


def test_replica_health_fresh_failing_and_absent(scratch_db, monkeypatch):
    import server
    monkeypatch.setattr(config, "SYNC_BUCKET", "bkt")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    db = _tick_row(scratch_db, {"ok": True, "synced_at": now})
    assert "note" not in server._replica_health(db)          # fresh: quiet
    db.execute("UPDATE runs SET steps=?",
               (json.dumps({"sync": {"ok": False, "error": "x"}}),))
    assert "failing" in server._replica_health(db)["note"]
    monkeypatch.setattr(config, "SYNC_BUCKET", "")
    assert server._replica_health(db) is None                # no replica
    db.close()


def test_replica_health_none_on_legacy_runs_table(scratch_db, monkeypatch):
    import server
    monkeypatch.setattr(config, "SYNC_BUCKET", "bkt")
    db = sqlite3.connect(scratch_db)
    index.ensure_runs(db)                          # no steps column
    assert server._replica_health(db) is None
    db.close()
