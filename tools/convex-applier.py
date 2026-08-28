#!/usr/bin/env python3
"""Drain the Convex spike's task-intent queue into the vault.

The phone flips a checkbox in the app; that writes a `taskIntents` row.
This process — a launchd agent that runs while the Mac is awake, never a
server — subscribes to the unapplied intents and applies each one to the
daily note with the same grammar `~/.claude/skills/daily-tasks/tasks.py`
uses, then confirms (or reports an error) through `today:applyIntent`.
Intents that arrive while the Mac sleeps are applied on wake.

Matching is by task text within the intent's day note (the skill's own
IDs are positional and can't be stored). Zero or several matches, or a
note that already has the wanted state, is reported rather than guessed.

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

def apply_to_lines(lines: list, text: str, want: str):
    """Pure: flip the one top-level task whose text matches. Returns
    (new_lines | None, error | None); None/None means already in state."""
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
    i, m = hits[0]
    done = m.group(3).lower() == "x"
    if (want == "done") == done:
        return None, None
    new = list(lines)
    new[i] = f"{m.group(1)}{m.group(2)}[{'x' if want == 'done' else ' '}] {m.group(4)}"
    return new, None

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
    if not os.path.isfile(path):
        return f"no note for {intent['day']}"
    lines = _read(path)
    new, err = apply_to_lines(lines, intent["text"], intent["want"])
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
        print(f"{stamp} {it['day']} {it['want']:4} {it['text'][:50]!r} "
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
