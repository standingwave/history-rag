#!/usr/bin/env python3
"""Drain the Convex app's task-intent queue into the vault.

The phone toggles, adds, edits or deletes a task in the app; that writes a
`taskIntents` row. This process — a launchd agent that runs while the Mac
is awake, never a server — subscribes to the unapplied intents and applies
each one to the daily note through `~/.claude/skills/daily-tasks/tasks.py`
(imported for its grammar: where a new task goes, what a block is), then
confirms (or reports an error) through `today:applyIntent`. Intents that
arrive while the Mac sleeps are applied on wake.

Matching is by task text within the intent's day note (the skill's own
IDs are positional and can't be stored). Zero or several matches, or a
note that already has the wanted state, is reported rather than guessed.
An add to a day with no note first does what the skill's `start` does —
carry undone tasks, add the day's routines — so the note is a normal one.

With --kick, a successful apply also runs `index.py --source tasks` and a
tasks-only Convex push, so the replica confirms in seconds instead of on
the next 30-minute refresh.

Run:    ~/.claude/rag-venv/bin/python tools/convex-applier.py [--once] [--kick]
Config: [convex] url, deploy_key_env, tasks_script
        [obsidian] vaults (to resolve the intent's vault name to a path)
"""
import argparse, importlib.util, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

TASK_RE_FALLBACK = r"^(\s*)([-*]\s+)\[( |x|X)\]\s?(.*)$"

def _norm(s: str) -> str:
    return " ".join(s.split()).lower()

_skill_mod = None
def skill():
    """The daily-tasks skill's tasks.py, imported once from [convex] tasks_script."""
    global _skill_mod
    if _skill_mod is None:
        path = config.CONVEX_TASKS_SCRIPT
        spec = importlib.util.spec_from_file_location("daily_tasks", path)
        if spec is None or spec.loader is None:
            raise FileNotFoundError(f"tasks script not found: {path}")
        _skill_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_skill_mod)
    return _skill_mod

def _find(lines: list, text: str):
    """The one top-level task whose text matches: ((i, match), None) or
    (None, error)."""
    import re
    rx = re.compile(TASK_RE_FALLBACK)
    hits = []
    for i, line in enumerate(lines):
        m = rx.match(line)
        if m and len(m.group(1).expandtabs(4)) == 0 and _norm(m.group(4)) == _norm(text):
            hits.append((i, m))
    if not hits:
        return None, "task not found in that day's note"
    if len(hits) > 1:
        return None, "task text is ambiguous in that day's note"
    return hits[0], None

def _exists(lines: list, text: str) -> bool:
    hit, err = _find(lines, text)
    return hit is not None or (err or "").startswith("task text is ambiguous")

def apply_to_lines(lines: list, text: str, want: str):
    """Pure: flip the one top-level task whose text matches. Returns
    (new_lines | None, error | None); None/None means already in state."""
    hit, err = _find(lines, text)
    if err:
        return None, err
    i, m = hit
    done = m.group(3).lower() == "x"
    if (want == "done") == done:
        return None, None
    new = list(lines)
    new[i] = f"{m.group(1)}{m.group(2)}[{'x' if want == 'done' else ' '}] {m.group(4)}"
    return new, None

def add_to_lines(lines: list, text: str):
    """Pure: append a top-level task where the skill's `add` puts it."""
    if _exists(lines, text):
        return None, "a task with that text is already in that day's note"
    new = list(lines)
    skill().append_top(new, text)
    return new, None

def edit_in_lines(lines: list, text: str, new_text: str):
    """Pure: rewrite the matched task's text, keeping checkbox and block."""
    hit, err = _find(lines, text)
    if err:
        return None, err
    if _norm(new_text) == _norm(text):
        return None, None
    if _exists(lines, new_text):
        return None, "a task with the new text is already in that day's note"
    i, m = hit
    new = list(lines)
    new[i] = f"{m.group(1)}{m.group(2)}[{m.group(3)}] {new_text.strip()}"
    return new, None

def delete_from_lines(lines: list, text: str):
    """Pure: remove the matched task and its block (subtasks, embeds, notes)."""
    hit, err = _find(lines, text)
    if err:
        return None, err
    i, _ = hit
    t = next(t for t in skill().parse(lines) if t.i == i)
    new = list(lines)
    del new[t.i:t.end]
    return new, None

def _parent(lines: list, text: str):
    """The skill's Task object for the matched top-level task."""
    hit, err = _find(lines, text)
    if err:
        return None, err.replace("task", "parent task", 1)
    return next(t for t in skill().parse(lines) if t.i == hit[0]), None

def _child(parent, text: str):
    hits = [c for c in parent.children if _norm(c.text) == _norm(text)]
    if not hits:
        return None, "subtask not found under that task"
    if len(hits) > 1:
        return None, "subtask text is ambiguous under that task"
    return hits[0], None

def apply_sub(lines: list, parent_text: str, kind: str, text: str,
              want: str | None = None, new_text: str = ""):
    """Pure: toggle / add / edit / delete one direct subtask of a top-level
    task. Returns (new_lines | None, error | None) like the others."""
    sk = skill()
    parent, err = _parent(lines, parent_text)
    if err:
        return None, err
    new = list(lines)
    if kind == "add":
        if any(_norm(c.text) == _norm(text) for c in parent.children):
            return None, "a subtask with that text is already under that task"
        parent, _ = _parent(new, parent_text)
        sk.append_in_block(new, parent, sk.task_line(parent.ws + sk.INDENT, text))
        return new, None
    child, err = _child(parent, text)
    if err:
        return None, err
    if kind == "toggle":
        if (want == "done") == child.done:
            return None, None
        new[child.i] = f"{child.ws}{child.bullet}[{'x' if want == 'done' else ' '}] {child.text}"
    elif kind == "edit":
        if _norm(new_text) == _norm(child.text):
            return None, None
        if any(_norm(c.text) == _norm(new_text) for c in parent.children):
            return None, "a subtask with the new text is already under that task"
        new[child.i] = f"{child.ws}{child.bullet}[{'x' if child.done else ' '}] {new_text.strip()}"
    elif kind == "delete":
        del new[child.i:child.end]
    else:
        return None, f"unknown intent kind {kind!r}"
    return new, None

URL_RE = __import__("re").compile(r"^https?://\S+$")

def page_title(url: str, timeout: float = 3.0) -> str | None:
    """Best-effort <title> of a page; None when anything is off."""
    import re, urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if "html" not in (r.headers.get("Content-Type") or ""):
                return None
            head = r.read(65536).decode("utf-8", "replace")
        m = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
        t = " ".join(__import__("html").unescape(m.group(1)).split()) if m else ""
        return t[:120] or None
    except Exception:
        return None

def attach_line(text: str, title=page_title) -> str:
    """The note line for an attach intent: a bare URL becomes a link with
    the page's title (or the URL itself), anything else is kept as is."""
    if URL_RE.match(text):
        return f"[{title(text) or text}]({text})"
    return text

def attach_to_lines(lines: list, parent_text: str, text: str, title=page_title):
    """Pure: append one plain line at the end of the task's block."""
    sk = skill()
    parent, err = _parent(lines, parent_text)
    if err:
        return None, err
    new = list(lines)
    sk.append_in_block(new, parent, f"{parent.ws}{sk.INDENT}- {attach_line(text, title)}")
    return new, None

def start_lines(path: str, day: str) -> list:
    """A fresh day's note the way the skill's `start` makes it: carried
    undone tasks, then routines. Either step may find nothing."""
    sk = skill()
    lines: list = []
    try:
        sk.carry_into(lines, path, day)
    except SystemExit:
        pass                      # no earlier note with tasks
    try:
        existing = {t.text.strip() for t in sk.parse(lines) if t.parent is None}
        for block in sk.routines_for(os.path.dirname(path), day):
            head = sk.TASK_RE.match(block[0]).group(4).strip()
            if head not in existing:
                sk.append_block(lines, block, routine=True)
    except (OSError, ValueError, SystemExit):
        pass                      # no template, or a bad date
    return lines

def vault_path(name: str) -> str | None:
    for v in config.get_paths("obsidian", "vaults", "CLAUDE_RAG_OBSIDIAN_VAULTS"):
        if os.path.basename(v.rstrip("/")) == name:
            return v
    return None

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read().split("\n")

def _write(path, lines):
    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")

def apply_intent(intent: dict) -> str | None:
    """Apply one intent to the vault. Returns an error string or None."""
    vault = vault_path(intent["vault"])
    if not vault:
        return f"vault {intent['vault']!r} is not configured on this Mac"
    path = os.path.join(vault, f"{intent['day']}.md")
    kind = intent.get("kind") or "toggle"
    if os.path.isfile(path):
        lines = _read(path)
    elif kind == "add":
        lines = start_lines(path, intent["day"])
    else:
        return f"no note for {intent['day']}"
    if kind == "attach":
        new, err = attach_to_lines(lines, intent.get("parent") or "", intent["text"])
    elif intent.get("parent"):
        new, err = apply_sub(lines, intent["parent"], kind, intent["text"],
                             intent.get("want"), intent.get("newText") or "")
    elif kind == "toggle":
        new, err = apply_to_lines(lines, intent["text"], intent["want"])
    elif kind == "add":
        new, err = add_to_lines(lines, intent["text"])
    elif kind == "edit":
        new, err = edit_in_lines(lines, intent["text"], intent["newText"] or "")
    elif kind == "delete":
        new, err = delete_from_lines(lines, intent["text"])
    else:
        return f"unknown intent kind {kind!r}"
    if err:
        return err
    if new is not None:
        _write(path, new)
    return None


def deploy_key() -> str:
    """The deploy key: the configured env var, else the same variable in
    deploy/convex/.env.local (where the Convex CLI keeps it), so one file
    serves the CLI, these tools, and the launchd applier."""
    key = os.environ.get(config.CONVEX_DEPLOY_KEY_ENV, "")
    if key:
        return key
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_file = os.path.join(root, "deploy", "convex", ".env.local")
    try:
        with open(env_file) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == config.CONVEX_DEPLOY_KEY_ENV and v:
                    return v.strip().strip("'\"")
    except OSError:
        pass
    return ""

def _client():
    from convex import ConvexClient
    key = deploy_key()
    if not key:
        sys.exit(f"{config.CONVEX_DEPLOY_KEY_ENV} is not set "
                 "(env or deploy/convex/.env.local)")
    c = ConvexClient(config.CONVEX_URL)
    c.set_admin_auth(key)
    return c

def _kick():
    py = sys.executable
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([py, os.path.join(root, "index.py"), "--source", "tasks",
                    "--no-run-record"], check=False)
    subprocess.run([py, os.path.join(root, "tools", "sync-convex.py"),
                    "--source", "tasks"], check=False)

def drain(client, intents: list, kick: bool) -> int:
    applied = 0
    for it in intents:
        err = apply_intent(it)
        client.mutation("today:applyIntent",
                        {"id": it["id"], **({"error": err} if err else {})})
        stamp = time.strftime("%H:%M:%S")
        what = it.get("want") if (it.get("kind") or "toggle") == "toggle" else it.get("kind")
        where = f" under {it['parent'][:30]!r}" if it.get("parent") else ""
        print(f"{stamp} {it['day']} {what:6} {it['text'][:50]!r}{where} "
              f"-> {err or 'ok'}", flush=True)
        applied += not err
    if applied and kick:
        _kick()
    return applied

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--once", action="store_true",
                   help="drain what's queued now and exit")
    p.add_argument("--kick", action="store_true",
                   help="after applying, re-index tasks and push them")
    a = p.parse_args(argv)
    if not config.CONVEX_URL:
        sys.exit("no [convex] url configured")
    client = _client()
    if a.once:
        return drain(client, client.query("today:pendingIntents", {}), a.kick)
    print("watching taskIntents…", flush=True)
    while True:
        try:
            for intents in client.subscribe("today:pendingIntents", {}):
                if intents:
                    drain(client, intents, a.kick)
        except KeyboardInterrupt:
            return
        except Exception as e:           # network blips on wake; retry
            print(f"subscription dropped: {e}; retrying in 30s", flush=True)
            time.sleep(30)

if __name__ == "__main__":
    main()
