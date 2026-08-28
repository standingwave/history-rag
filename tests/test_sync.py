"""Replica sync: the change signature moves exactly when replica-visible
content moves, unchanged content skips the upload, an unconfigured bucket
makes the whole tool a no-op (file-less installs untouched), a transfer that
hits a network blip retries instead of costing a whole refresh cycle, and a
differential push assembles an object that is byte-for-byte the snapshot —
or fails, rather than leaving the replica quietly wrong."""
import hashlib, json, os, sqlite3, sys, tempfile, types

import pytest

import config
from tests.helpers import load_script

sync_s3 = load_script("tools/sync-s3.py")


def _make_index(path, filler=0):
    # This function empties the chunks table of whatever it is pointed at, and
    # conftest's redirection only applies to code run under pytest from this
    # directory. Anything else reaching it is aimed at a real index.
    assert path.startswith(tempfile.gettempdir()), f"refusing to rewrite {path}"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, "
               "text TEXT, source TEXT, timestamp TEXT, location TEXT, "
               "meta TEXT)")
    db.execute("DELETE FROM chunks")
    db.execute("INSERT INTO chunks VALUES ('a', 'hello', 'shell', "
               "'2026-01-01T00:00:00+00:00', 'loc', NULL)")
    db.executemany("INSERT INTO chunks VALUES (?, ?, 'shell', "
                   "'2026-01-01T00:00:00+00:00', 'loc', NULL)",
                   [(f"f{i}", f"filler row {i} " * 12) for i in range(filler)])
    db.commit()
    return db


class _NotFound(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "404"}}


class _FakeS3:
    """Enough of S3 to hold a real object. Parts are stored, a completed
    multipart upload is the concatenation of them, and ETags are computed
    the way S3 computes them — so a part copied from the wrong offset really
    does produce the wrong ETag, and the bytes really are wrong."""

    def __init__(self, log, present=True, fail_times=0):
        self.log = log
        self.present = present
        self.fail_times = fail_times                 # transfers that blow up
        self.attempts = 0
        self.client_kwargs = {}
        self.body = b""
        self.etag = "seeded-by-someone-else"
        self.uploads = {}                            # id -> {part number: bytes}
        self.copied, self.sent, self.aborted = [], [], []
        self.explode = ""                            # method that fails

    def _maybe_explode(self, name):
        if self.explode == name:
            raise RuntimeError(f"{name} lost the connection")

    def head_object(self, Bucket, Key):
        if not self.present:
            raise _NotFound()
        return {"ETag": f'"{self.etag}"'}

    def upload_file(self, path, bucket, key):
        assert os.path.exists(path)                  # snapshot really exists
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("Could not connect to the endpoint URL")
        with open(path, "rb") as f:
            self.body = f.read()
        self.etag = hashlib.md5(self.body).hexdigest()
        self.log.append((bucket, key, len(self.body)))
        self.present = True                          # pushed = object exists

    def create_multipart_upload(self, Bucket, Key):
        self._maybe_explode("create_multipart_upload")
        upload_id = str(len(self.uploads) + 1)
        self.uploads[upload_id] = {}
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self._maybe_explode("upload_part")
        self.uploads[UploadId][PartNumber] = Body
        self.sent.append(PartNumber)
        return {"ETag": f'"{hashlib.md5(Body).hexdigest()}"'}

    def upload_part_copy(self, Bucket, Key, UploadId, PartNumber,
                         CopySource, CopySourceRange):
        self._maybe_explode("upload_part_copy")
        first, last = CopySourceRange.removeprefix("bytes=").split("-")
        chunk = self.body[int(first):int(last) + 1]   # the live object's bytes
        self.uploads[UploadId][PartNumber] = chunk
        self.copied.append(PartNumber)
        return {"CopyPartResult": {"ETag": f'"{hashlib.md5(chunk).hexdigest()}"'}}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self._maybe_explode("complete_multipart_upload")
        chunks = [self.uploads[UploadId][p["PartNumber"]]
                  for p in MultipartUpload["Parts"]]
        self.body = b"".join(chunks)
        joined = b"".join(hashlib.md5(c).digest() for c in chunks)
        self.etag = f"{hashlib.md5(joined).hexdigest()}-{len(chunks)}"
        self.log.append((Bucket, Key, len(self.body)))
        self.present = True
        return {"ETag": f'"{self.etag}"'}

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.aborted.append(UploadId)
        self.uploads.pop(UploadId, None)


def _install_fake_boto3(monkeypatch, log, present=True, fail_times=0):
    """Fake boto3 *and* botocore. Neither is a dev dependency — CI installs
    requirements-dev.txt only — so the real Config is unavailable there.
    Returns the one bucket every client hands back, so what one run pushed is
    what the next run finds there — and so a test can assert how the client
    was built and how hard it tried."""
    s3 = _FakeS3(log, present, fail_times)

    def client(service, **kw):
        s3.client_kwargs = kw
        return s3

    fake = types.ModuleType("boto3")
    fake.client = client
    monkeypatch.setitem(sys.modules, "boto3", fake)

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **kw: types.SimpleNamespace(**kw)
    botocore.config = botocore_config
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    return s3


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


def _ready_to_push(monkeypatch, filler=0):
    """A fresh index and no state from an earlier push, so the next main()
    genuinely uploads."""
    _make_index(config.DB_PATH, filler).close()
    for suffix in (".synced", ".sync-snapshot", ".sync-manifest"):
        if os.path.exists(config.DB_PATH + suffix):
            os.remove(config.DB_PATH + suffix)
    monkeypatch.setattr(config, "SYNC_BUCKET", "bkt")


def test_client_is_built_with_retry_and_timeout_config(monkeypatch):
    """A bare client inherits legacy retry mode, which gives up in seconds
    against a downed resolver — the shape of every recorded sync failure."""
    _ready_to_push(monkeypatch)
    s3 = _install_fake_boto3(monkeypatch, [])
    sync_s3.main()

    cfg = s3.client_kwargs["config"]
    assert cfg.retries == {"mode": "standard", "max_attempts": 5}
    assert cfg.connect_timeout == sync_s3._CONNECT_TIMEOUT
    assert cfg.read_timeout == sync_s3._READ_TIMEOUT


def test_retry_count_is_configurable(monkeypatch):
    _ready_to_push(monkeypatch)
    monkeypatch.setenv("CLAUDE_RAG_SYNC_RETRIES", "9")
    s3 = _install_fake_boto3(monkeypatch, [])
    sync_s3.main()
    assert s3.client_kwargs["config"].retries["max_attempts"] == 9


def test_transient_transfer_failure_is_retried(monkeypatch, capsys):
    """A blip that outlasts the client's own retries costs seconds, not a
    whole refresh interval."""
    _ready_to_push(monkeypatch)
    log = []
    s3 = _install_fake_boto3(monkeypatch, log, fail_times=2)
    monkeypatch.setattr(sync_s3, "_RETRY_WAIT", 0)

    assert sync_s3.main()["action"] == "pushed"
    assert s3.attempts == 3                          # failed twice, then won
    assert len(log) == 1
    assert "retrying" in capsys.readouterr().out


def test_upload_gives_up_after_the_last_attempt(monkeypatch):
    """Exhaustion must leave the marker unwritten — that is what makes the
    next tick re-push instead of believing the replica is current."""
    _ready_to_push(monkeypatch)
    s3 = _install_fake_boto3(monkeypatch, [], fail_times=99)
    monkeypatch.setattr(sync_s3, "_RETRY_WAIT", 0)

    with pytest.raises(RuntimeError):
        sync_s3.main()

    assert s3.attempts == sync_s3._PUSH_ATTEMPTS
    assert not os.path.exists(config.DB_PATH + ".synced")
    assert not os.path.exists(config.DB_PATH + ".sync-snapshot")


def _differential_mode(monkeypatch, part_size=1024):
    """Parts far below S3's 5MB floor — config rejects those, but the upload
    code doesn't care, and it keeps the fixture DB small."""
    monkeypatch.setattr(config, "SYNC_DIFFERENTIAL", True)
    monkeypatch.setattr(config, "SYNC_PART_SIZE", part_size)


def _add_rows(n=1, tag="b"):
    """A content change, so the next main() has something to push."""
    db = sqlite3.connect(config.DB_PATH)
    db.executemany("INSERT INTO chunks VALUES (?, ?, 'shell', "
                   "'2026-01-02T00:00:00+00:00', 'loc', NULL)",
                   [(f"{tag}{i}", f"added row {i} " * 12) for i in range(n)])
    db.commit()
    db.close()


def _ids_in(body, tmp_path):
    """The chunk ids in whatever the bucket ended up holding. The question a
    part-copying upload has to answer is not "did it run" but "is the object
    still a database, and does it have the new rows"."""
    p = tmp_path / "replica.db"
    p.write_bytes(body)
    db = sqlite3.connect(p)
    ids = {r[0] for r in db.execute("SELECT id FROM chunks")}
    db.close()
    return ids


def _manifest():
    with open(sync_s3._manifest_path()) as f:
        return json.load(f)


def _marker_is_current():
    """Whether the .synced marker matches the index as it stands — which is
    what decides whether the next tick pushes again."""
    db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    sig = sync_s3.signature(db)
    db.close()
    path = config.DB_PATH + ".synced"
    return os.path.exists(path) and open(path).read().strip() == sig


def test_differential_copies_unchanged_parts_and_ships_only_the_rest(
        monkeypatch, tmp_path):
    """The point of the whole exercise: a small change moves a few parts, the
    rest are copied inside S3, and the object is a working database."""
    _ready_to_push(monkeypatch, filler=200)
    s3 = _install_fake_boto3(monkeypatch, [])
    _differential_mode(monkeypatch)

    assert sync_s3.main()["mode"] == "whole"         # nothing to diff against
    assert _manifest()["etag"] == s3.etag

    _add_rows()
    out = sync_s3.main()

    assert out["mode"] == "differential"
    assert 0 < out["changed"] < out["parts"]         # some copied, some sent
    assert sorted(s3.copied + s3.sent) == list(range(1, out["parts"] + 1))
    assert len(s3.sent) == out["changed"]
    assert out["bytes"] < os.path.getsize(config.DB_PATH)
    assert {"a", "b0"} <= _ids_in(s3.body, tmp_path)
    assert _manifest()["etag"] == s3.etag            # describes what's up there


def test_a_grown_file_uploads_its_new_tail(monkeypatch):
    """New pages land past the old end of the file, where there is nothing to
    copy from — those parts must be sent, and only those."""
    _ready_to_push(monkeypatch, filler=200)
    s3 = _install_fake_boto3(monkeypatch, [])
    _differential_mode(monkeypatch)
    sync_s3.main()
    was = len(_manifest()["parts"])

    _add_rows(400)
    out = sync_s3.main()

    assert out["parts"] > was                        # the file really grew
    assert set(range(was + 1, out["parts"] + 1)) <= set(s3.sent)
    assert s3.copied                                 # the untouched head rode along


def test_a_foreign_write_to_the_key_forces_a_whole_file_push(monkeypatch):
    """The manifest describes one specific object. If the bucket holds a
    different one, its parts are not ours to copy."""
    _ready_to_push(monkeypatch, filler=200)
    s3 = _install_fake_boto3(monkeypatch, [])
    _differential_mode(monkeypatch)
    sync_s3.main()

    _add_rows()
    s3.etag = "written-by-something-else"
    out = sync_s3.main()

    assert out["mode"] == "whole"
    assert not s3.copied
    assert _manifest()["etag"] == s3.etag            # and the manifest catches up


def test_no_manifest_or_a_new_part_size_pushes_whole_file(monkeypatch):
    _ready_to_push(monkeypatch, filler=200)
    s3 = _install_fake_boto3(monkeypatch, [])
    _differential_mode(monkeypatch)
    sync_s3.main()

    os.remove(sync_s3._manifest_path())
    _add_rows(tag="b")
    assert sync_s3.main()["mode"] == "whole"
    assert not s3.copied

    _differential_mode(monkeypatch, part_size=2048)  # old hashes now meaningless
    _add_rows(tag="c")
    assert sync_s3.main()["mode"] == "whole"
    assert not s3.copied


def test_a_broken_differential_push_falls_back_and_still_lands(
        monkeypatch, capsys):
    """The differential path is allowed to be slower or dumber than the one
    it replaces. It is not allowed to lose the push."""
    _ready_to_push(monkeypatch, filler=200)
    s3 = _install_fake_boto3(monkeypatch, [])
    _differential_mode(monkeypatch)
    sync_s3.main()

    _add_rows(tag="b")
    s3.explode = "upload_part"
    out = sync_s3.main()

    assert out == {"action": "pushed", "bytes": out["bytes"], "mode": "whole",
                   "parts": out["parts"], "changed": out["parts"]}
    assert s3.aborted                                # no parts left behind
    assert "sending whole file" in capsys.readouterr().out
    assert _marker_is_current()


def test_a_mismapped_part_fails_the_sync_instead_of_corrupting_the_replica(
        monkeypatch, tmp_path):
    """The failure this design exists to make impossible: a part copied from
    the wrong offset yields a file that still opens as a database and answers
    queries wrongly, and nothing downstream would ever notice. The ETag we
    compute locally does."""
    _ready_to_push(monkeypatch, filler=200)
    s3 = _install_fake_boto3(monkeypatch, [])
    _differential_mode(monkeypatch)
    sync_s3.main()
    good = _manifest()["etag"]

    _add_rows()
    real, slipped = sync_s3._send_part, []

    def off_by_one(s3_, bucket, upload_id, snap, part_size, index, size, reuse):
        if reuse and index and not slipped:
            slipped.append(index)
            index -= 1                               # the bug, planted
        return real(s3_, bucket, upload_id, snap, part_size, index, size, reuse)

    monkeypatch.setattr(sync_s3, "_send_part", off_by_one)
    monkeypatch.setattr(sync_s3, "_COPY_WORKERS", 1)
    monkeypatch.setattr(sync_s3, "_RETRY_WAIT", 0)

    with pytest.raises(sync_s3.VerifyError):
        sync_s3.main()

    assert slipped                                   # the bug did fire
    assert len(s3.uploads) == 1                      # not retried: same bug twice
    assert not _marker_is_current()                  # so the next tick re-pushes
    assert not os.path.exists(config.DB_PATH + ".sync-snapshot")
    assert _manifest()["etag"] == good               # still the last good object

    monkeypatch.setattr(sync_s3, "_send_part", real)
    assert sync_s3.main()["mode"] == "whole"         # next tick repairs it
    assert {"a", "b0"} <= _ids_in(s3.body, tmp_path)


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
