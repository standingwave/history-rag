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
Config: [sync] bucket, key (default history-rag.db);
        env CLAUDE_RAG_SYNC_BUCKET / CLAUDE_RAG_SYNC_KEY.
        AWS credentials resolve through the standard boto3 chain
        (AWS_PROFILE / ~/.aws / env vars).
"""
import hashlib, os, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

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

def main():
    bucket = config.SYNC_BUCKET
    if not bucket:
        print("sync: no [sync] bucket configured — skipping")
        return
    if not os.path.exists(config.DB_PATH):
        print("sync: no index yet — skipping")
        return

    db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    sig = signature(db)
    db.close()
    marker = config.DB_PATH + ".synced"
    last = open(marker).read().strip() if os.path.exists(marker) else ""
    if sig == last:
        print("sync: index unchanged since last push — skipping")
        return

    try:
        import boto3
    except ImportError:
        sys.exit("sync: boto3 not installed — "
                 "uv pip install --python $(which python) boto3")

    snap = config.DB_PATH + ".sync-snapshot"
    src, dst = sqlite3.connect(config.DB_PATH), sqlite3.connect(snap)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    try:
        s3 = boto3.client("s3", region_name=config.SYNC_REGION or None)
        s3.upload_file(snap, bucket, config.SYNC_KEY)
    finally:
        os.remove(snap)
    with open(marker, "w") as f:
        f.write(sig)
    mb = os.path.getsize(config.DB_PATH) / 1e6
    print(f"sync: pushed ~{mb:.0f}MB to s3://{bucket}/{config.SYNC_KEY}")

if __name__ == "__main__":
    main()
