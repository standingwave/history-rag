"""Push the index to S3 for the Lambda replica (deploy/lambda).

Runs at the end of the launchd chain (index && backup && sync) and no-ops
unless [sync] bucket is configured, so installs without a remote replica are
untouched. Uploads its own snapshot taken via SQLite's online backup API —
never the live file, which a concurrent indexer could be mid-write — and
skips the upload entirely when a content signature over the chunks table
matches the last push, so an idle machine (overnight, weekends) moves zero
bytes. The daily tools/backup.py copies are unrelated: those are disaster
recovery, this is replication.

Run:    ~/.claude/rag-venv/bin/python tools/sync-s3.py
Config: [sync] bucket, key (default history-rag.db), retries (default 5);
        env CLAUDE_RAG_SYNC_BUCKET / CLAUDE_RAG_SYNC_KEY /
        CLAUDE_RAG_SYNC_RETRIES.
        AWS credentials resolve through the standard boto3 chain
        (AWS_PROFILE / ~/.aws / env vars).
"""
import hashlib, os, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Transfer settings. A push moves the whole ~1.2GB index and takes minutes,
# so it is in flight for a large share of every refresh cycle and a brief
# loss of network lands inside it more often than not — every sync failure
# recorded so far (wip/SPEC-sync-resilience.md).
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 60
_PUSH_ATTEMPTS = 3          # the first push plus two retries
_RETRY_WAIT = 60            # seconds between them

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

def _upload(s3, snap: str, bucket: str) -> int:
    """Push the snapshot, retrying the whole transfer. The client's own
    retries cover a blip inside one request; this covers one that outlasts
    them, so a short outage costs seconds here instead of a whole refresh
    interval. Returns the attempt that succeeded.

    A sleeping machine is not covered and cannot be: the process is frozen
    mid-transfer, not failing, and resumes to find the connection long dead.
    That case is the next tick's job."""
    for attempt in range(1, _PUSH_ATTEMPTS + 1):
        try:
            s3.upload_file(snap, bucket, config.SYNC_KEY)
            return attempt
        except Exception as e:
            if attempt == _PUSH_ATTEMPTS:
                raise
            print(f"sync: upload attempt {attempt}/{_PUSH_ATTEMPTS} failed "
                  f"({e}); retrying in {_RETRY_WAIT}s", flush=True)
            time.sleep(_RETRY_WAIT)

def main():
    """Returns the outcome for the refresh driver: {"action": "unconfigured"
    | "no-index" | "unchanged" | "pushed"[, "bytes": N]}. `synced_at`
    stamping belongs to the caller — and only for unchanged/pushed."""
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
        _upload(s3, snap, bucket)
    finally:
        os.remove(snap)
    with open(marker, "w") as f:
        f.write(sig)
    size = os.path.getsize(config.DB_PATH)
    print(f"sync: pushed ~{size / 1e6:.0f}MB to s3://{bucket}/{config.SYNC_KEY}")
    return {"action": "pushed", "bytes": size}

if __name__ == "__main__":
    main()
