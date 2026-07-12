"""Dated backups of the sole-copy databases. The index outlives its sources
(aged-out transcripts, expired browser history), so losing history-rag.db
loses that history permanently; appusage.db is likewise the only record.

Designed to run after each scheduled index refresh (the plist chains it) but
acts at most once per local day: skips when today's backup exists, then
prunes to the newest [backup] keep files per database. Uses SQLite's online
backup API, so a concurrently writing indexer or daemon can't corrupt the
copy, and writes via a temp file so an interrupted run never leaves a
truncated backup behind.

Run:    ~/.claude/rag-venv/bin/python tools/backup.py
Config: [backup] dir (default ~/.claude/backups), keep (default 7);
        env CLAUDE_RAG_BACKUP_DIR / CLAUDE_RAG_BACKUP_KEEP.
"""
import datetime, glob, os, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

def backup_dir() -> str:
    return os.path.expanduser(str(config.get(
        "backup", "dir", "CLAUDE_RAG_BACKUP_DIR", "~/.claude/backups")))

def keep_count() -> int:
    return int(config.get("backup", "keep", "CLAUDE_RAG_BACKUP_KEEP", 7))

def backup_one(src_path: str, out_dir: str, today: str):
    """Copy src to <out_dir>/<stem>-<today>.db unless that file already
    exists. Returns the path written, or None (skipped or source missing)."""
    if not os.path.exists(src_path):
        return None
    stem = os.path.splitext(os.path.basename(src_path))[0]
    target = os.path.join(out_dir, f"{stem}-{today}.db")
    if os.path.exists(target):
        return None
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(target + ".tmp")
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    os.replace(target + ".tmp", target)
    return target

def prune(out_dir: str, stem: str, keep: int):
    """Drop all but the newest `keep` backups for one database. ISO-dated
    names sort chronologically, so lexicographic order is date order."""
    files = sorted(glob.glob(os.path.join(out_dir, f"{stem}-*.db")))
    removed = files[:-keep] if keep > 0 else []
    for f in removed:
        os.remove(f)
    return removed

def main():
    """Returns {stem: "written"|"current"} so the refresh driver can record
    the outcome without scraping stdout; standalone runs just print."""
    out_dir = backup_dir()
    os.makedirs(out_dir, exist_ok=True)
    today = datetime.date.today().isoformat()
    outcome = {}
    for src in (config.DB_PATH, config.APPUSAGE_DB):
        stem = os.path.splitext(os.path.basename(src))[0]
        written = backup_one(src, out_dir, today)
        removed = prune(out_dir, stem, keep_count())
        note = f", pruned {len(removed)}" if removed else ""
        print(f"{stem}: {written or 'already current'}{note}", flush=True)
        outcome[stem] = "written" if written else "current"
    return outcome

if __name__ == "__main__":
    main()
