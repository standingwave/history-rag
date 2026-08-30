"""Tasks source: checkbox blocks from the vault's daily notes, one chunk per
task across its whole lifetime.

Daily notes (`YYYY-MM-DD.md` at the vault root) hold `- [ ]` / `- [x]`
lines; the `daily-tasks` skill copies undone top-level blocks forward each
morning, so one task appears verbatim in every note from first-seen until
checked. The obsidian source already indexes those notes, but a daily note
is short enough to be a single chunk holding every task, so a task query
hits the day, not the task, and nothing structured survives for a view.

Here the *task text* is the identity: id hashes vault + normalized text, so
the eight daily copies of "treat hoya" collapse to one chunk. Timestamp is
the latest note the task appears in — because carry only copies undone
blocks, that is the completion day for a done task and today for an open
one, so one window filter serves both "what did I finish last week" and
"today's list". Editing a task's wording orphans the old chunk (--prune
--source tasks cleans up; the vault is the durable record).

Grammar (shared with ~/.claude/skills/daily-tasks/tasks.py, the note's sole
scripted writer): a task is a checkbox line; its block is every following
line indented deeper; `![[…]]` lines in the block are attachments; a
`## Routine` heading starts the routine section. Routine tasks recur by
template and are indexed unless [tasks] index_routine is off.

Reads through obsidian.iter_notes — the vault list is [obsidian] vaults.
"""
import hashlib, re
from collections import defaultdict
from sources.common import SECRET_RE
from sources import obsidian

MAX_CHARS = 2000
ROUTINE = "Routine"
TASK_RE = re.compile(r"^(\s*)([-*]\s+)\[( |x|X)\]\s?(.*)$")
EMBED_RE = re.compile(r"^\s*!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]\s*$")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$")
DAILY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")

def _index_routine() -> bool:
    import config
    v = config.get("tasks", "index_routine", "CLAUDE_RAG_TASKS_ROUTINE", True)
    return str(v).lower() in ("1", "true", "yes") if isinstance(v, str) else bool(v)

def _width(ws: str) -> int:
    return len(ws.expandtabs(4))

def parse_note(body: str) -> list[dict]:
    """Top-level task blocks in document order:
    {text, done, section, line, subtasks:[{text, done, depth}], attachments,
    notes} — notes are the block's plain lines (links, remarks), bullet stripped."""
    lines = body.split("\n")
    tasks, section = [], ""
    i = 0
    while i < len(lines):
        line = lines[i]
        h = HEADING_RE.match(line)
        if h:
            section = h.group(1)
            i += 1
            continue
        m = TASK_RE.match(line)
        if not m:
            i += 1
            continue
        depth = _width(m.group(1))
        task = {"text": m.group(4).strip(), "done": m.group(3).lower() == "x",
                "section": section, "line": i, "subtasks": [], "attachments": [], "notes": []}
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip():
                if _width(nxt[:len(nxt) - len(nxt.lstrip())]) <= depth:
                    break
                sm = TASK_RE.match(nxt)
                em = EMBED_RE.match(nxt)
                if sm:
                    task["subtasks"].append({
                        "text": sm.group(4).strip(),
                        "done": sm.group(3).lower() == "x",
                        "depth": _width(sm.group(1)) - depth})
                elif em:
                    task["attachments"].append(em.group(1).strip())
                else:
                    task["notes"].append(re.sub(r"^\s*[-*]\s+", "", nxt).strip())
            j += 1
        tasks.append(task)
        i = j
    return tasks

def _norm(text: str) -> str:
    return " ".join(text.split()).lower()

def _embed_text(task: dict) -> str:
    out = ["Task: " + task["text"]]
    for st in task["subtasks"]:
        out.append("\t" * max(1, st["depth"] // 4) + st["text"])
    return "\n".join(out)[:MAX_CHARS]

def iter_daily(vaults=None):
    """(vault, date, tasks) per daily note, via the shared vault reader."""
    for vname, rel, body, _ts in obsidian.iter_notes(vaults):
        m = DAILY_RE.match(rel)
        if m:
            yield vname, m.group(1), parse_note(body)

def iter_chunks(vaults=None):
    routine_ok = _index_routine()
    # (vault, norm text) -> {dates: set, latest: (date, task)}
    life: dict = defaultdict(lambda: {"dates": set(), "latest": None})
    for vname, day, tasks in iter_daily(vaults):
        for t in tasks:
            if not t["text"]:
                continue
            key = (vname, _norm(t["text"]))
            entry = life[key]
            entry["dates"].add(day)
            if entry["latest"] is None or day > entry["latest"][0]:
                entry["latest"] = (day, t)
    for (vname, norm), entry in sorted(life.items()):
        day, t = entry["latest"]
        if t["section"] == ROUTINE and not routine_ok:
            continue
        text = _embed_text(t)
        if SECRET_RE.search(text):
            continue
        dates = sorted(entry["dates"])
        cid = "tasks:" + hashlib.sha256(
            f"{vname}\0{norm}".encode()).hexdigest()[:26]
        yield cid, text, {
            "source": "tasks",
            "timestamp": obsidian._fm_iso(day),
            "location": f"{day}.md#{t['line']}",
            "meta": {
                "vault": vname,
                "done": t["done"],
                "first_seen": dates[0],
                "last_seen": day,
                "done_on": day if t["done"] else None,
                "section": t["section"],
                "order": t["line"],
                "subtasks": t["subtasks"],
                "attachments": t["attachments"],
                "notes": t["notes"],
                "days": len(dates),
            },
        }
