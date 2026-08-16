"""Replica sync: the change signature moves exactly when replica-visible
content moves, unchanged content skips the upload, an unconfigured bucket
makes the whole tool a no-op (file-less installs untouched), and a transfer
that hits a network blip retries instead of costing a whole refresh cycle."""
import os, sqlite3, sys, types

import pytest

import config
from tests.helpers import load_script

sync_s3 = load_script("tools/sync-s3.py")


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


class _NotFound(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "404"}}


class _FakeS3:
    def __init__(self, log, present=True, fail_times=0):
        self.log = log
        self.present = present
        self.fail_times = fail_times                 # transfers that blow up
        self.attempts = 0
        self.client_kwargs = {}

    def head_object(self, Bucket, Key):
        if not self.present:
            raise _NotFound()
        return {}

    def upload_file(self, path, bucket, key):
        assert os.path.exists(path)                  # snapshot really exists
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("Could not connect to the endpoint URL")
        self.log.append((bucket, key, os.path.getsize(path)))
        self.present = True                          # pushed = object exists


def _install_fake_boto3(monkeypatch, log, present=True, fail_times=0):
    """Fake boto3 *and* botocore. Neither is a dev dependency — CI installs
    requirements-dev.txt only — so the real Config is unavailable there.
    Returns the list of clients handed out, for asserting how they were
    built and how hard they tried."""
    made = []

    def client(service, **kw):
        c = _FakeS3(log, present, fail_times)
        c.client_kwargs = kw
        made.append(c)
        return c

    fake = types.ModuleType("boto3")
    fake.client = client
    monkeypatch.setitem(sys.modules, "boto3", fake)

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **kw: types.SimpleNamespace(**kw)
    botocore.config = botocore_config
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    return made


def test_signature_tracks_replica_visible_content():
    db = _make_index(config.DB_PATH)
    before = sync_s3.signature(db)
    assert before == sync_s3.signature(db)           # deterministic
    db.execute("UPDATE chunks SET meta='{}' WHERE id='a'")
    assert sync_s3.signature(db) != before           # meta feeds expand
    db.close()


def test_no_bucket_is_a_noop(monkeypatch, capsys):
    monkeypatch.setattr(config, "SYNC_BUCKET", "")
    assert sync_s3.main() == {"action": "unconfigured"}
    assert "skipping" in capsys.readouterr().out


def test_sync_uploads_then_skips_until_change(monkeypatch, capsys):
    _make_index(config.DB_PATH).close()
    marker = config.DB_PATH + ".synced"
    if os.path.exists(marker):
        os.remove(marker)
    monkeypatch.setattr(config, "SYNC_BUCKET", "bkt")
    log = []
    _install_fake_boto3(monkeypatch, log)

    out = sync_s3.main()                             # first run pushes
    assert log == [("bkt", config.SYNC_KEY, os.path.getsize(config.DB_PATH))]
    assert not os.path.exists(config.DB_PATH + ".sync-snapshot")
    assert out["action"] == "pushed" and out["bytes"] > 0

    assert sync_s3.main()["action"] == "unchanged"   # unchanged -> no upload
    assert len(log) == 1
    assert "unchanged" in capsys.readouterr().out

    db = sqlite3.connect(config.DB_PATH)
    db.execute("INSERT INTO chunks VALUES ('b', 'new', 'shell', "
               "'2026-01-02T00:00:00+00:00', 'loc', NULL)")
    db.commit()
    db.close()
    assert sync_s3.main()["action"] == "pushed"      # changed -> pushes again
    assert len(log) == 2


def _ready_to_push(monkeypatch):
    """A fresh index and no marker, so the next main() genuinely uploads."""
    _make_index(config.DB_PATH).close()
    for suffix in (".synced", ".sync-snapshot"):
        if os.path.exists(config.DB_PATH + suffix):
            os.remove(config.DB_PATH + suffix)
    monkeypatch.setattr(config, "SYNC_BUCKET", "bkt")


def test_client_is_built_with_retry_and_timeout_config(monkeypatch):
    """A bare client inherits legacy retry mode, which gives up in seconds
    against a downed resolver — the shape of every recorded sync failure."""
    _ready_to_push(monkeypatch)
    made = _install_fake_boto3(monkeypatch, [])
    sync_s3.main()

    cfg = made[0].client_kwargs["config"]
    assert cfg.retries == {"mode": "standard", "max_attempts": 5}
    assert cfg.connect_timeout == sync_s3._CONNECT_TIMEOUT
    assert cfg.read_timeout == sync_s3._READ_TIMEOUT


def test_retry_count_is_configurable(monkeypatch):
    _ready_to_push(monkeypatch)
    monkeypatch.setenv("CLAUDE_RAG_SYNC_RETRIES", "9")
    made = _install_fake_boto3(monkeypatch, [])
    sync_s3.main()
    assert made[0].client_kwargs["config"].retries["max_attempts"] == 9


def test_transient_transfer_failure_is_retried(monkeypatch, capsys):
    """A blip that outlasts the client's own retries costs seconds, not a
    whole refresh interval."""
    _ready_to_push(monkeypatch)
    log = []
    made = _install_fake_boto3(monkeypatch, log, fail_times=2)
    monkeypatch.setattr(sync_s3, "_RETRY_WAIT", 0)

    assert sync_s3.main()["action"] == "pushed"
    assert made[0].attempts == 3                     # failed twice, then won
    assert len(log) == 1
    assert "retrying" in capsys.readouterr().out


def test_upload_gives_up_after_the_last_attempt(monkeypatch):
    """Exhaustion must leave the marker unwritten — that is what makes the
    next tick re-push instead of believing the replica is current."""
    _ready_to_push(monkeypatch)
    made = _install_fake_boto3(monkeypatch, [], fail_times=99)
    monkeypatch.setattr(sync_s3, "_RETRY_WAIT", 0)

    with pytest.raises(RuntimeError):
        sync_s3.main()

    assert made[0].attempts == sync_s3._PUSH_ATTEMPTS
    assert not os.path.exists(config.DB_PATH + ".synced")
    assert not os.path.exists(config.DB_PATH + ".sync-snapshot")


def test_unchanged_but_object_gone_repushes(monkeypatch):
    """Skip-unchanged means 'replica confirmed current' — a HEAD miss
    (deleted object, recreated bucket) re-pushes instead of skipping."""
    _make_index(config.DB_PATH).close()
    monkeypatch.setattr(config, "SYNC_BUCKET", "bkt")
    log = []
    _install_fake_boto3(monkeypatch, log, present=False)
    with open(config.DB_PATH + ".synced", "w") as f:
        db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        f.write(sync_s3.signature(db))
        db.close()
    assert sync_s3.main()["action"] == "pushed"
    assert len(log) == 1
