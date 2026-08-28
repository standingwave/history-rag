"""Push the index to S3 for the Lambda replica (deploy/lambda).

Runs at the end of the launchd chain (index && backup && sync) and no-ops
unless [sync] bucket is configured, so installs without a remote replica are
untouched. Uploads its own snapshot taken via SQLite's online backup API —
never the live file, which a concurrent indexer could be mid-write — and
skips the upload entirely when a content signature over the chunks table
matches the last push, so an idle machine (overnight, weekends) moves zero
bytes. The daily tools/backup.py copies are unrelated: those are disaster
recovery, this is replication.

With [sync] differential on, a push that does happen ships only the parts
whose bytes changed and has S3 copy the rest from the object already in the
bucket. The object it produces is byte-identical to a whole-file upload, so
the replica's read path is unaffected.

Run:    ~/.claude/rag-venv/bin/python tools/sync-s3.py
Config: [sync] bucket, key (default history-rag.db), retries (default 5),
        differential (default false), part_size_mb (default 8, min 5);
        env CLAUDE_RAG_SYNC_BUCKET / CLAUDE_RAG_SYNC_KEY /
        CLAUDE_RAG_SYNC_RETRIES / CLAUDE_RAG_SYNC_DIFFERENTIAL /
        CLAUDE_RAG_SYNC_PART_SIZE_MB.
        AWS credentials resolve through the standard boto3 chain
        (AWS_PROFILE / ~/.aws / env vars).
"""
import concurrent.futures, hashlib, json, os, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Transfer settings. A whole-file push moves ~1.2GB over minutes, long enough
# for a brief loss of network to land inside it; more often the network is
# already gone when the very first request goes out.
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 60
_PUSH_ATTEMPTS = 3          # the first push plus two retries
_RETRY_WAIT = 60            # seconds between them
_COPY_WORKERS = 10          # part copies in flight at once

class VerifyError(RuntimeError):
    """The uploaded object is not the snapshot we meant to upload."""

def signature(db) -> str:
    """Hash of everything a replica can serve: chunk id, timestamp, text,
    meta (vectors are derived from text, so text change implies vector
    change; meta feeds expand). Ordered scan => deterministic."""
    h = hashlib.sha256()
    for row in db.execute("SELECT id, timestamp, text, coalesce(meta, '') "
                          "FROM chunks ORDER BY id"):
        for field in row:
            h.update(str(field).encode())
            h.update(b"\x1f")
    return h.hexdigest()

def _remote_present(s3, bucket) -> bool:
    """HEAD the object so skip-unchanged means "replica confirmed current",
    not "the local marker says so" — a deleted object triggers a re-push.
    Duck-typed error check: no botocore exception classes, so tests can
    fake boto3 whole."""
    try:
        s3.head_object(Bucket=bucket, Key=config.SYNC_KEY)
        return True
    except Exception as e:
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise

def _client():
    """An S3 client with explicit retry and timeout settings. A bare client
    inherits botocore's legacy retry mode, which spends its attempts within
    seconds against a resolver that is down — long enough to fail, too short
    to outlast the blip. Standard mode retries more error classes with
    exponential backoff. Imports botocore only here: boto3 is an optional
    dependency (tests fake both wholesale)."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("sync: boto3 not installed — "
                 "uv pip install --python $(which python) boto3")
    retries = int(config.get("sync", "retries", "CLAUDE_RAG_SYNC_RETRIES", 5))
    return boto3.client(
        "s3", region_name=config.SYNC_REGION or None,
        config=Config(retries={"mode": "standard", "max_attempts": retries},
                      connect_timeout=_CONNECT_TIMEOUT,
                      read_timeout=_READ_TIMEOUT))

def _manifest_path() -> str:
    return config.DB_PATH + ".sync-manifest"

def _digests(path: str, part_size: int) -> list:
    """Per-part (sha256, md5) over the snapshot in one read pass. The sha256
    says whether a part still matches what the bucket holds; the md5 is what
    S3 builds a multipart ETag from, so the same pass yields the ETag the
    finished object must have."""
    out = []
    with open(path, "rb") as f:
        while chunk := f.read(part_size):
            out.append((hashlib.sha256(chunk).hexdigest(),
                        hashlib.md5(chunk).digest()))
    return out

def _expected_etag(digests: list) -> str:
    """What S3 will call an object assembled from exactly these parts: the
    md5 of the concatenated per-part md5s, then the part count."""
    return (hashlib.md5(b"".join(md5 for _, md5 in digests)).hexdigest()
            + f"-{len(digests)}")

def _read_manifest(part_size: int):
    """The previous push's part hashes, or None if they can't be used —
    absent, unreadable, or taken at a different part size."""
    try:
        with open(_manifest_path()) as f:
            m = json.load(f)
        return m if m["part_size"] == part_size and m["etag"] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None

def _remote_etag(s3, bucket):
    """The bucket's current ETag for the index, or None if it isn't there."""
    try:
        head = s3.head_object(Bucket=bucket, Key=config.SYNC_KEY)
    except Exception:
        return None
    return str(head.get("ETag", "")).strip('"') or None

def _send_part(s3, bucket, upload_id, snap, part_size, index, size, reuse):
    """One part of the multipart upload: copied server-side from the object
    already in the bucket when its bytes are unchanged, sent from the
    snapshot when they aren't."""
    num = index + 1
    start = index * part_size
    if reuse:
        res = s3.upload_part_copy(
            Bucket=bucket, Key=config.SYNC_KEY, UploadId=upload_id,
            PartNumber=num,
            CopySource={"Bucket": bucket, "Key": config.SYNC_KEY},
            CopySourceRange=f"bytes={start}-{start + size - 1}")
        etag = res["CopyPartResult"]["ETag"]
    else:
        with open(snap, "rb") as f:
            f.seek(start)
            body = f.read(size)
        etag = s3.upload_part(Bucket=bucket, Key=config.SYNC_KEY,
                              UploadId=upload_id, PartNumber=num,
                              Body=body)["ETag"]
    return {"PartNumber": num, "ETag": etag}

def _abort(s3, bucket, upload_id):
    """Best effort: aborting is itself a network call, and the usual reason
    we are here is that the network went away. The bucket's lifecycle rule
    (deploy/lambda/s3-lifecycle.json) is the cleanup that always works."""
    try:
        s3.abort_multipart_upload(Bucket=bucket, Key=config.SYNC_KEY,
                                  UploadId=upload_id)
    except Exception:
        pass

def _differential(s3, snap: str, bucket: str, part_size: int, digests: list):
    """Push only the parts whose bytes changed; S3 copies the rest from the
    object it already holds, so they never leave the machine. Returns None
    when the manifest can't be shown to describe that object — a different
    part size, a missing manifest, or anyone else having written the key —
    which is the caller's cue to push the whole file instead."""
    old = _read_manifest(part_size)
    if not old or not digests:
        return None
    if old["etag"] != _remote_etag(s3, bucket):
        return None

    total, seen = os.path.getsize(snap), old["parts"]
    plan = [(i, min(part_size, total - i * part_size),
             i < len(seen) and seen[i] == sha)
            for i, (sha, _) in enumerate(digests)]

    upload_id = s3.create_multipart_upload(
        Bucket=bucket, Key=config.SYNC_KEY)["UploadId"]
    try:
        with concurrent.futures.ThreadPoolExecutor(_COPY_WORKERS) as pool:
            parts = list(pool.map(
                lambda p: _send_part(s3, bucket, upload_id, snap, part_size,
                                     *p), plan))
        res = s3.complete_multipart_upload(
            Bucket=bucket, Key=config.SYNC_KEY, UploadId=upload_id,
            MultipartUpload={"Parts": sorted(parts,
                                             key=lambda p: p["PartNumber"])})
    except Exception:
        _abort(s3, bucket, upload_id)
        raise

    # The failure worth spending an md5 pass on isn't a crash — it's a part
    # mapped to the wrong offset, which yields a valid SQLite file that
    # answers queries wrongly and nothing downstream would ever flag.
    etag = str(res.get("ETag", "")).strip('"')
    want = _expected_etag(digests)
    if etag != want:
        raise VerifyError(f"uploaded object has ETag {etag}, expected {want} "
                          f"— replica not confirmed, leaving it to re-push")
    fresh = [p for p in plan if not p[2]]
    return {"mode": "differential", "bytes": sum(size for _, size, _ in fresh),
            "parts": len(plan), "changed": len(fresh),
            "manifest": {"etag": etag, "part_size": part_size,
                         "parts": [sha for sha, _ in digests]}}

def _push(s3, snap: str, bucket: str, part_size: int, digests: list) -> dict:
    """One whole attempt at getting the snapshot into the bucket. Anything
    that goes wrong inside the differential path falls back to the plain
    whole-file upload: that path may be slower than the one it replaces,
    never wrong. A failed verification is the exception — it means the
    object is not what we think it is, and the sync must fail."""
    if digests:
        try:
            out = _differential(s3, snap, bucket, part_size, digests)
            if out:
                return out
        except VerifyError:
            raise
        except Exception as e:
            print(f"sync: differential push failed ({e}) — sending whole file",
                  flush=True)
    s3.upload_file(snap, bucket, config.SYNC_KEY)
    etag = _remote_etag(s3, bucket) if digests else None
    return {"mode": "whole", "bytes": os.path.getsize(snap),
            "parts": len(digests), "changed": len(digests),
            "manifest": ({"etag": etag, "part_size": part_size,
                          "parts": [sha for sha, _ in digests]}
                         if etag else None)}

def _upload(s3, snap: str, bucket: str) -> dict:
    """Push the snapshot, retrying the whole transfer. The client's own
    retries cover a blip inside one request; this covers one that outlasts
    them, so a short outage costs seconds here instead of a whole refresh
    interval.

    A sleeping machine is not covered and cannot be: the process is frozen
    mid-transfer, not failing, and resumes to find the connection long dead.
    That case is the next tick's job."""
    part_size = config.SYNC_PART_SIZE
    digests = _digests(snap, part_size) if config.SYNC_DIFFERENTIAL else []
    for attempt in range(1, _PUSH_ATTEMPTS + 1):
        try:
            return _push(s3, snap, bucket, part_size, digests)
        except VerifyError:
            raise                      # retrying repeats the same bad mapping
        except Exception as e:
            if attempt == _PUSH_ATTEMPTS:
                raise
            print(f"sync: upload attempt {attempt}/{_PUSH_ATTEMPTS} failed "
                  f"({e}); retrying in {_RETRY_WAIT}s", flush=True)
            time.sleep(_RETRY_WAIT)

def main():
    """Returns the outcome for the refresh driver: {"action": "unconfigured"
    | "no-index" | "unchanged" | "pushed"[, "bytes": N, "mode": "whole" |
    "differential", "parts": N, "changed": N]}. `synced_at` stamping belongs
    to the caller — and only for unchanged/pushed."""
    bucket = config.SYNC_BUCKET
    if not bucket:
        print("sync: no [sync] bucket configured — skipping")
        return {"action": "unconfigured"}
    if not os.path.exists(config.DB_PATH):
        print("sync: no index yet — skipping")
        return {"action": "no-index"}

    db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    sig = signature(db)
    db.close()
    marker = config.DB_PATH + ".synced"
    last = open(marker).read().strip() if os.path.exists(marker) else ""

    s3 = _client()
    if sig == last and _remote_present(s3, bucket):
        print("sync: index unchanged since last push — replica current")
        return {"action": "unchanged"}

    snap = config.DB_PATH + ".sync-snapshot"
    src, dst = sqlite3.connect(config.DB_PATH), sqlite3.connect(snap)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    try:
        res = _upload(s3, snap, bucket)
    finally:
        os.remove(snap)
    # Both files describe the object that was just confirmed into the bucket,
    # so neither is written until it is there. A manifest that can't be
    # written is removed rather than left to describe an older object.
    if res["manifest"]:
        with open(_manifest_path(), "w") as f:
            json.dump(res["manifest"], f)
    elif os.path.exists(_manifest_path()):
        os.remove(_manifest_path())
    with open(marker, "w") as f:
        f.write(sig)
    size, how = res["bytes"], ""
    if res["mode"] == "differential":
        how = f" differential ({res['changed']}/{res['parts']} parts)"
    print(f"sync: pushed ~{size / 1e6:.0f}MB{how} to "
          f"s3://{bucket}/{config.SYNC_KEY}")
    return {"action": "pushed", "bytes": size, "mode": res["mode"],
            "parts": res["parts"], "changed": res["changed"]}

if __name__ == "__main__":
    main()
