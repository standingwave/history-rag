"""Replica sync: the change signature moves exactly when replica-visible
content moves, unchanged content skips the upload, and an unconfigured
bucket makes the whole tool a no-op (file-less installs untouched)."""
import importlib.util, os, pathlib, sqlite3, sys, types

import config

_spec = importlib.util.spec_from_file_location(
    "sync_s3", pathlib.Path(__file__).resolve().parent.parent
    / "tools" / "sync-s3.py")
sync_s3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_s3)


def _make_index(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, "
               "text TEXT, source TEXT, timestamp TEXT, location TEXT, "
               "meta TEXT)")
    db.execute("DELETE FROM chunks")
    db.execute("INSERT INTO chunks VALUES ('a', 'hello', 'shell', "
               "'2026-01-01T00:00:00+00:00', 'loc', NULL)")
    db.commit()
    return db


class _FakeS3:
    def __init__(self, log):
        self.log = log

    def upload_file(self, path, bucket, key):
        assert os.path.exists(path)                  # snapshot really exists
        self.log.append((bucket, key, os.path.getsize(path)))


def _install_fake_boto3(monkeypatch, log):
    fake = types.ModuleType("boto3")
    fake.client = lambda service, **kw: _FakeS3(log)
    monkeypatch.setitem(sys.modules, "boto3", fake)


def test_signature_tracks_replica_visible_content():
    db = _make_index(config.DB_PATH)
    before = sync_s3.signature(db)
    assert before == sync_s3.signature(db)           # deterministic
    db.execute("UPDATE chunks SET meta='{}' WHERE id='a'")
    assert sync_s3.signature(db) != before           # meta feeds expand
    db.close()


def test_no_bucket_is_a_noop(monkeypatch, capsys):
    monkeypatch.setattr(config, "SYNC_BUCKET", "")
    sync_s3.main()
    assert "skipping" in capsys.readouterr().out


def test_sync_uploads_then_skips_until_change(monkeypatch, capsys):
    _make_index(config.DB_PATH).close()
    marker = config.DB_PATH + ".synced"
    if os.path.exists(marker):
        os.remove(marker)
    monkeypatch.setattr(config, "SYNC_BUCKET", "bkt")
    log = []
    _install_fake_boto3(monkeypatch, log)

    sync_s3.main()                                   # first run pushes
    assert log == [("bkt", config.SYNC_KEY, os.path.getsize(config.DB_PATH))]
    assert not os.path.exists(config.DB_PATH + ".sync-snapshot")

    sync_s3.main()                                   # unchanged -> no upload
    assert len(log) == 1
    assert "unchanged" in capsys.readouterr().out

    db = sqlite3.connect(config.DB_PATH)
    db.execute("INSERT INTO chunks VALUES ('b', 'new', 'shell', "
               "'2026-01-02T00:00:00+00:00', 'loc', NULL)")
    db.commit()
    db.close()
    sync_s3.main()                                   # changed -> pushes again
    assert len(log) == 2
