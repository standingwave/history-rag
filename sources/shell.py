"""Shell history source: bash + zsh commands, deduped.

Reads atuin's SQLite store when present (every run dated, with cwd and exit
code), plus ~/.bash_history and ~/.zsh_history (and archived files via
`[shell] histfiles` / CLAUDE_RAG_HISTFILES). Handles zsh extended format
(`: <epoch>:<elapsed>;<cmd>`) and bash `#<epoch>` timestamp lines.

Identical commands collapse to one chunk carrying a run count and the latest
run's timestamp and cwd (as `location`, so shell supports location-prefix
filtering like the other sources). Commands atuin knows are skipped when read
from live histfiles — atuin covers that era, and counting both would inflate.
Trivial commands are dropped, and any command that looks like it contains a
secret is skipped so it never gets embedded or surfaced back into a session.
"""
import os, re, glob, hashlib, shutil, sqlite3, sys, tempfile
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
    """(live, archived) histfiles. Live ones are superseded by atuin for
    commands it knows; archived ones predate atuin and always count."""
    import config
    live = ["~/.zsh_history", "~/.zhistory", "~/.bash_history"]
    # macOS keeps per-session history snapshots in these dirs.
    for pat in ("~/.zsh_sessions/*.history*", "~/.bash_sessions/*.history*"):
        live += sorted(glob.glob(os.path.expanduser(pat)))
    seen, out_live, out_arch = set(), [], []
    for c in config.get_paths("shell", "histfiles", "CLAUDE_RAG_HISTFILES"):
        p = os.path.realpath(c)
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            out_arch.append(p)
    for c in live:
        p = os.path.realpath(os.path.expanduser(c))
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            out_live.append(p)
    return out_live, out_arch

def _atuin_db() -> str:
    import config
    return os.path.expanduser(str(config.get(
        "shell", "atuin_db", "CLAUDE_RAG_ATUIN_DB",
        "~/.local/share/atuin/history.db")))

def _abbrev(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path

def _atuin_snapshot():
    """Copy atuin's DB (it holds the file open) and connect; None if absent."""
    path = _atuin_db()
    if not path or not os.path.isfile(path):
        return None, None
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copyfile(path, tmp)
        return sqlite3.connect(tmp), tmp
    except OSError as e:
        print(f"shell: skipping atuin ({path}): {e}", file=sys.stderr)
        os.unlink(tmp)
        return None, None

def _read_atuin():
    """Yield (epoch_seconds, cmd, cwd, exit) for every recorded run."""
    db, tmp = _atuin_snapshot()
    if db is None:
        return
    try:
        yield from db.execute(
            "SELECT timestamp / 1000000000.0, command, cwd, exit "
            "FROM history WHERE deleted_at IS NULL")
        db.close()
    except sqlite3.Error as e:
        print(f"shell: skipping atuin: {e}", file=sys.stderr)
    finally:
        os.unlink(tmp)

def atuin_context(command: str, n: int):
    """The ±n commands around `command`'s latest atuin run — same-session
    neighbors preferred, time-neighbors when the session is tiny. None when
    atuin is absent or never saw the command, so context stays honest."""
    db, tmp = _atuin_snapshot()
    if db is None:
        return None
    try:
        hit = db.execute(
            "SELECT timestamp, session FROM history WHERE command = ? "
            "AND deleted_at IS NULL ORDER BY timestamp DESC LIMIT 1",
            (command,)).fetchone()
        if not hit:
            return None
        ts, session = hit
        rows = db.execute(
            "SELECT timestamp, cwd, exit, command FROM history WHERE "
            "session = ? AND deleted_at IS NULL ORDER BY timestamp",
            (session,)).fetchall()
        scope = "session"
        if len(rows) < 3:
            rows = sorted(db.execute(
                "SELECT timestamp, cwd, exit, command FROM history WHERE "
                "deleted_at IS NULL ORDER BY ABS(timestamp - ?) LIMIT ?",
                (ts, 2 * n + 1)).fetchall())
            scope = "time"
        idx = next((i for i, r in enumerate(rows) if r[0] == ts), 0)
        out = []
        for t, cwd, exit_code, cmd in rows[max(0, idx - n):idx + n + 1]:
            item = {"timestamp": datetime.fromtimestamp(
                        t / 1e9, tz=timezone.utc).isoformat(),
                    "cwd": _abbrev(cwd or ""), "exit": exit_code,
                    "command": cmd[:500]}
            if t == ts:
                item["target"] = True
            out.append(item)
        return {"scope": scope, "commands": out}
    except sqlite3.Error:
        return None
    finally:
        os.unlink(tmp)

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

def iter_dated_runs(since_epoch: float):
    """(epoch_seconds, cmd, cwd) for every dated run at/after `since_epoch`:
    atuin runs (cwd known) plus dated histfile entries for commands atuin
    doesn't know (cwd unknown -> ""). Per-run, NOT deduped — iter_chunks
    collapses a command to its latest run, so day rollups (the digest source)
    must count runs through here. Same keep/secret filtering as iter_chunks."""
    atuin_cmds = set()
    for epoch, cmd, cwd, _exit in _read_atuin():
        cmd = (cmd or "").strip()
        if not _keep(cmd):
            continue
        atuin_cmds.add(cmd)
        if epoch >= since_epoch:
            yield epoch, cmd, _abbrev(cwd or "")
    live, archived = _history_files()
    for path in live + archived:
        parse = _parse_zsh_extended if _looks_zsh_extended(path) else _parse_bash
        for epoch, cmd in parse(path):
            cmd = cmd.strip()
            if not epoch or epoch < since_epoch or not _keep(cmd):
                continue
            if cmd in atuin_cmds:
                continue           # atuin already covers this command's runs
            yield epoch, cmd, ""

def iter_chunks():
    # command -> [count, latest_epoch, location, atuin_cwd, atuin_exit]
    seen: dict[str, list] = {}
    atuin_cmds = set()
    for epoch, cmd, cwd, exit_code in _read_atuin():
        cmd = (cmd or "").strip()
        if not _keep(cmd):
            continue
        atuin_cmds.add(cmd)
        rec = seen.get(cmd)
        if rec is None:
            seen[cmd] = [1, epoch, _abbrev(cwd or ""), cwd or "", exit_code]
        else:
            rec[0] += 1
            if epoch > rec[1]:
                rec[1:] = [epoch, _abbrev(cwd or ""), cwd or "", exit_code]

    live, archived = _history_files()
    for path, is_live in [(p, True) for p in live] + [(p, False) for p in archived]:
        parse = _parse_zsh_extended if _looks_zsh_extended(path) else _parse_bash
        fname = os.path.basename(path)
        for epoch, cmd in parse(path):
            cmd = cmd.strip()
            if not _keep(cmd):
                continue
            if is_live and cmd in atuin_cmds:
                continue           # atuin already covers this command's runs
            rec = seen.get(cmd)
            if rec is None:
                seen[cmd] = [1, epoch, fname, "", None]
            else:
                rec[0] += 1
                if epoch > rec[1]:
                    rec[1] = epoch
                    if not rec[3]:     # never displace an atuin cwd-location
                        rec[2] = fname

    for cmd, (count, epoch, loc, cwd, exit_code) in seen.items():
        cid = "shell:" + hashlib.sha256(cmd.encode()).hexdigest()[:26]
        meta = {"count": count}
        if cwd:
            meta["cwd"] = cwd
            if exit_code is not None:
                meta["exit"] = exit_code
        yield cid, cmd[:MAX_CHARS], {
            "source": "shell",
            "timestamp": _iso(epoch),
            "location": loc,
            "meta": meta,
        }
