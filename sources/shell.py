"""Shell history source: bash + zsh commands, deduped.

Reads ~/.bash_history and ~/.zsh_history (plus any files in the colon-separated
CLAUDE_RAG_HISTFILES env var, for archived history). Handles zsh extended
format (`: <epoch>:<elapsed>;<cmd>`) and bash `#<epoch>` timestamp lines.

Identical commands collapse to one chunk carrying a run count and the latest
timestamp, so a decade of history reduces to its unique set. Trivial commands
are dropped, and any command that looks like it contains a secret is skipped so
it never gets embedded or surfaced back into a session.
"""
import os, re, glob, hashlib
from datetime import datetime, timezone
from sources.common import SECRET_RE

MAX_CHARS = 2000
MIN_CHARS = 4

_ZSH_RE = re.compile(r"^: (\d+):\d+;(.*)$")

# Bare commands too trivial to be worth a vector.
_STOP = {
    "ls", "ll", "la", "l", "cd", "cd ..", "..", "...", "pwd", "clear", "c",
    "exit", "q", "k", "gst", "gs", "gd", "h", "history", "top", "htop",
}

# Shell-only credential shape (mysql -pPassword style); too URL-hostile to
# live in the shared regex, where it would drop paths like /my-project-x.
_FLAG_SECRET_RE = re.compile(r"-p\S{6,}")

def _iso(epoch: int) -> str:
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""

def _history_files():
    candidates = ["~/.zsh_history", "~/.zhistory", "~/.bash_history"]
    # macOS keeps per-session history snapshots in these dirs.
    for pat in ("~/.zsh_sessions/*.history*", "~/.bash_sessions/*.history*"):
        candidates += sorted(glob.glob(os.path.expanduser(pat)))
    extra = os.environ.get("CLAUDE_RAG_HISTFILES", "")
    if extra:
        candidates += extra.split(":")
    seen, out = set(), []
    for c in candidates:
        p = os.path.realpath(os.path.expanduser(c))
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            out.append(p)
    return out

def _looks_zsh_extended(path: str) -> bool:
    with open(path, errors="replace") as f:
        for i, line in enumerate(f):
            if _ZSH_RE.match(line.rstrip("\n")):
                return True
            if i > 200:
                break
    return False

def _parse_zsh_extended(path):
    """Yield (epoch, command); non-prefixed lines continue a multiline command."""
    cur_ts, cur_cmd = 0, None
    with open(path, errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = _ZSH_RE.match(line)
            if m:
                if cur_cmd is not None:
                    yield cur_ts, cur_cmd
                cur_ts, cur_cmd = int(m.group(1)), m.group(2)
            elif cur_cmd is not None:
                cur_cmd += "\n" + line
    if cur_cmd is not None:
        yield cur_ts, cur_cmd

def _parse_bash(path):
    """Yield (epoch, command); `#<epoch>` lines set the next command's time."""
    pending = 0
    with open(path, errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line:
                continue
            if line.startswith("#") and line[1:].strip().isdigit():
                pending = int(line[1:].strip())
                continue
            yield pending, line
            pending = 0

def _keep(cmd: str) -> bool:
    return (len(cmd) >= MIN_CHARS and cmd not in _STOP
            and not SECRET_RE.search(cmd) and not _FLAG_SECRET_RE.search(cmd))

def iter_chunks():
    # command -> [count, latest_epoch, history_filename]
    seen: dict[str, list] = {}
    for path in _history_files():
        parse = _parse_zsh_extended if _looks_zsh_extended(path) else _parse_bash
        fname = os.path.basename(path)
        for epoch, cmd in parse(path):
            cmd = cmd.strip()
            if not _keep(cmd):
                continue
            rec = seen.get(cmd)
            if rec is None:
                seen[cmd] = [1, epoch, fname]
            else:
                rec[0] += 1
                if epoch > rec[1]:
                    rec[1] = epoch
    for cmd, (count, epoch, fname) in seen.items():
        cid = "shell:" + hashlib.sha256(cmd.encode()).hexdigest()[:26]
        yield cid, cmd[:MAX_CHARS], {
            "source": "shell",
            "timestamp": _iso(epoch),
            "location": fname,
            "meta": {"count": count},
        }
