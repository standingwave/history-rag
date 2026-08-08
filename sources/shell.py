"""Shell history source: working sessions from atuin, flat commands elsewhere.

Reads atuin's SQLite store when present (every run dated, with cwd and exit
code), plus ~/.bash_history and ~/.zsh_history (and archived files via
`[shell] histfiles` / CLAUDE_RAG_HISTFILES). Handles zsh extended format
(`: <epoch>:<elapsed>;<cmd>`) and bash `#<epoch>` timestamp lines.

Dated atuin runs with a real cwd group into WORKING SESSIONS — same cwd,
adjacent runs no more than SESSION_GAP apart — embedded as one narrative
chunk: where, when, the command sequence with failures marked. A bare
`npm test` line gives an embedder almost nothing; "in ~/dev/x: ran pytest
(failed), edited the filter, ran pytest, committed" ranks. Session ids hash
cwd + start time, so a still-open session grows and re-embeds in place; the
per-run detail stays reachable via expand() (session_commands). Location is
the abbreviated cwd, so location-prefix filtering works.

Everything else stays one deduped chunk per command (count + latest run):
histfile commands, and atuin's imported rows — cwd 'unknown', exit -1, and a
timestamp that is the IMPORT moment, not the run, so they index undated
rather than pile a thousand commands onto the day the import ran. Commands
atuin knows are skipped when read from live histfiles — atuin covers that
era, and counting both would inflate. Trivial commands are dropped, and any
command that looks like it contains a secret is skipped so it never gets
embedded or surfaced back into a session.
"""
import os, re, glob, hashlib, sqlite3, sys
from datetime import datetime, timezone
from sources.common import SECRET_RE, snapshot_db

MAX_CHARS = 2000
MIN_CHARS = 4
SESSION_GAP = 30 * 60        # idle seconds that close a working session
SESSION_MAX_LINES = 30       # narrative lines before the middle elides

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
    """Snapshot atuin's DB (live, WAL-mode) and connect; None if absent."""
    path = _atuin_db()
    if not path or not os.path.isfile(path):
        return None, None
    try:
        return snapshot_db(path)
    except (OSError, sqlite3.Error) as e:
        print(f"shell: skipping atuin ({path}): {e}", file=sys.stderr)
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

def _real_cwd(cwd: str) -> bool:
    """atuin's imported rows carry cwd 'unknown' (and a timestamp that is
    the import moment) — only rows with a real cwd are session material."""
    return bool(cwd) and cwd != "unknown"

def _sessions(rows):
    """Group dated (epoch, cmd, cwd, exit) runs into working sessions: same
    cwd, adjacent runs no more than SESSION_GAP apart. Yields
    (cwd, [(epoch, cmd, exit), ...]) with runs time-ordered."""
    by_cwd: dict[str, list] = {}
    for epoch, cmd, cwd, exit_code in rows:
        by_cwd.setdefault(cwd, []).append((epoch, cmd, exit_code))
    for cwd, runs in by_cwd.items():
        runs.sort(key=lambda r: r[0])
        cur: list = []
        for run in runs:
            if cur and run[0] - cur[-1][0] > SESSION_GAP:
                yield cwd, cur
                cur = []
            cur.append(run)
        if cur:
            yield cwd, cur

def _session_text(cwd, runs):
    """One session as a narrative: a header naming place, span, and size,
    then the command sequence — consecutive repeats collapsed to ×n, nonzero
    exits annotated, over-long sessions elided in the middle."""
    entries: list = []                     # [line, repeat-count]
    for _epoch, cmd, exit_code in runs:
        line = " ".join(cmd.split())[:200] \
            + (f" (exit {exit_code})" if exit_code else "")
        if entries and entries[-1][0] == line:
            entries[-1][1] += 1
        else:
            entries.append([line, 1])
    lines = [l + (f" (×{n})" if n > 1 else "") for l, n in entries]
    if len(lines) > SESSION_MAX_LINES:
        head = SESSION_MAX_LINES // 2
        tail = SESSION_MAX_LINES - head - 1
        elided = len(lines) - head - tail
        lines = lines[:head] + [f"… +{elided} more"] + lines[-tail:]
    d0 = datetime.fromtimestamp(runs[0][0], tz=timezone.utc)
    d1 = datetime.fromtimestamp(runs[-1][0], tz=timezone.utc)
    end = f"{d1:%H:%M}" if d1.date() == d0.date() else f"{d1:%Y-%m-%d %H:%M}"
    n = len(runs)
    header = (f"Shell session in {_abbrev(cwd)} — {d0:%Y-%m-%d %H:%M}–{end} "
              f"UTC ({n} command{'s' if n != 1 else ''})")
    return "\n".join([header, *lines])[:MAX_CHARS]

def session_commands(meta):
    """Every atuin run inside a session chunk's cwd + time window — the
    per-run detail the narrative summarizes, for expand(). None when atuin
    is absent or the meta doesn't parse. Secret-looking commands stay out,
    same as everywhere else."""
    db, tmp = _atuin_snapshot()
    if db is None:
        return None
    try:
        lo = int(datetime.fromisoformat(meta["start"]).timestamp() * 1e9 - 1e9)
        hi = int(datetime.fromisoformat(meta["end"]).timestamp() * 1e9 + 1e9)
        rows = db.execute(
            "SELECT timestamp, exit, command FROM history WHERE cwd = ? "
            "AND timestamp BETWEEN ? AND ? AND deleted_at IS NULL "
            "ORDER BY timestamp", (meta["cwd"], lo, hi)).fetchall()
        cmds = [{"timestamp": datetime.fromtimestamp(
                     t / 1e9, tz=timezone.utc).isoformat(),
                 "exit": e, "command": c[:500]}
                for t, e, c in rows
                if not (SECRET_RE.search(c) or _FLAG_SECRET_RE.search(c))]
        return {"cwd": _abbrev(meta["cwd"]), "commands": cmds} if cmds else None
    except (sqlite3.Error, KeyError, ValueError, TypeError):
        return None
    finally:
        os.unlink(tmp)

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
        if not _real_cwd(cwd or ""):
            continue        # imported row: epoch is the import moment, not a run
        if epoch >= since_epoch:
            yield epoch, cmd, _abbrev(cwd)
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
    session_rows = []
    flat: dict[str, list] = {}    # command -> [count, latest_epoch, location]
    atuin_cmds = set()
    for epoch, cmd, cwd, exit_code in _read_atuin():
        cmd = (cmd or "").strip()
        if not _keep(cmd):
            continue
        atuin_cmds.add(cmd)
        if epoch and _real_cwd(cwd or ""):
            session_rows.append((epoch, cmd, cwd, exit_code))
        else:
            # imported row: its timestamp is the import moment, not the run
            # — undated is the honest representation
            rec = flat.get(cmd)
            if rec is None:
                flat[cmd] = [1, 0, ""]
            else:
                rec[0] += 1

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
            rec = flat.get(cmd)
            if rec is None:
                flat[cmd] = [1, epoch, fname]
            else:
                rec[0] += 1
                if epoch > rec[1]:
                    rec[1:] = [epoch, fname]

    for cwd, runs in _sessions(session_rows):
        t0, t1 = runs[0][0], runs[-1][0]
        cid = "shell:" + hashlib.sha256(
            f"session\0{cwd}\0{int(t0)}".encode()).hexdigest()[:26]
        yield cid, _session_text(cwd, runs), {
            "source": "shell",
            "timestamp": _iso(t0),
            "location": _abbrev(cwd),
            "meta": {"kind": "session", "cwd": cwd, "commands": len(runs),
                     "start": _iso(t0), "end": _iso(t1)},
        }

    for cmd, (count, epoch, loc) in flat.items():
        cid = "shell:" + hashlib.sha256(cmd.encode()).hexdigest()[:26]
        yield cid, cmd[:MAX_CHARS], {
            "source": "shell",
            "timestamp": _iso(epoch),
            "location": loc,
            "meta": {"count": count},
        }
