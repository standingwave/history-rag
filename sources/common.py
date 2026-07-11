"""Helpers shared across sources."""
import os, re, shutil, sqlite3, tempfile
from pathlib import Path

def _copy_snapshot(path: str):
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copyfile(path, tmp)
        return sqlite3.connect(tmp), tmp
    except OSError:
        os.unlink(tmp)
        raise

def snapshot_db(path: str, lock_timeout: float = 2.0):
    """Consistent snapshot of a live SQLite DB -> (connection, tmp_path);
    the caller unlinks tmp_path. Raises sqlite3.Error/OSError; no temp file
    survives an error.

    Two live-store realities pull in opposite directions. WAL stores (atuin,
    Safari History) welcome concurrent readers but keep recent commits in
    the -wal sidecar, so a bare file copy is torn or silently stale — they
    need the online backup API. Chromium-family History is EXCLUSIVELY
    locked while the browser runs, so every sqlite read — including backup,
    which Python retries forever on SQLITE_BUSY — hangs; it needs the byte
    copy (July 2026: two indexers wedged in backup() on Helium's History).

    So: no pending WAL -> byte copy, nothing to lose. Pending WAL -> probe
    the store with a bounded lock wait, back up on success (single pass, so
    no busy-retry window), and fall back to the byte copy if the probe is
    locked out — a stale snapshot beats a hung indexer."""
    try:
        pending_wal = os.path.getsize(path + "-wal") > 0
    except OSError:
        pending_wal = False
    if not pending_wal:
        return _copy_snapshot(path)
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # as_uri() percent-encodes, so paths with spaces survive URI parsing
        src = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                              uri=True, timeout=lock_timeout)
        try:
            # Fail fast (not forever) if the store is exclusively locked.
            src.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            except BaseException:
                dst.close()
                raise
            return dst, tmp
        finally:
            src.close()
    except sqlite3.OperationalError:
        os.unlink(tmp)
        return _copy_snapshot(path)
    except BaseException:
        os.unlink(tmp)
        raise

# Text that likely contains a credential -> drop the chunk entirely, so it is
# never embedded or surfaced back into a session. Shared by every source that
# ingests free-form text (shell commands, URLs, notes).
#
# Two kinds of pattern: trigger words (a label like "password" near a value)
# and provider key SHAPES, for keys that appear with no label at all — a vault
# note reading "new relic: NRAK-..." has no trigger word, only the shape.
# Shapes must be precise enough that prose can't match: each is anchored at a
# word boundary and requires a long run of key-safe characters, so
# "desk-mounted-monitor-arm" or "public_html" stay clear.
SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|bearer|authorization|://[^/\s:]+:[^/\s@]+@"
    r"|AKIA[0-9A-Z]{16}"                    # AWS access key id
    r"|\bNRAK-[A-Z0-9]{20,}"                # New Relic
    r"|\b(public|private)_[A-Za-z0-9+/=]{16,}"   # ImageKit-style keypairs
    r"|\bsk-[A-Za-z0-9_-]{16,}"             # OpenAI / Anthropic / Stripe secret keys
    r"|\b(ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}|\bgithub_pat_"   # GitHub tokens
    r"|\bxox[baprs]-"                       # Slack tokens
    r"|\bAIza[0-9A-Za-z_-]{35}"             # Google API keys
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ"        # JWT (header.payload prefix)
    r")"
)
