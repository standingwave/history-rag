#!/usr/bin/env python3
"""Manage the checkbox task list in today's Obsidian daily note.

Usage (ID is 1, 2.1, 2.1.3 ...):
  tasks.py [--date YYYY-MM-DD] list
  tasks.py add "text"              # new top-level task
  tasks.py sub ID "text"           # new subtask under ID
  tasks.py toggle ID               # done <-> undone (this line only)
  tasks.py edit ID "new text"
  tasks.py delete ID               # removes the task and everything under it
  tasks.py attach ID /path/to/img  # copy into <vault>/Attachments, embed under ID
  tasks.py carry                   # copy undone top-level blocks from the nearest earlier note with tasks
  tasks.py start                   # carry, then add today's routines from Templates/Daily Tasks Template.md

A task's block is its line plus every following line indented deeper than it
(subtasks, ![[embeds]], notes). Vault: $OBSIDIAN_VAULT or the default below.
"""
import argparse
import datetime as dt
import os
import re
import shutil
import sys

DEFAULT_VAULT = os.path.expanduser("~/Documents/Obsidian")
ATTACH_DIR = "Attachments"
TEMPLATE = os.path.join("Templates", "Daily Tasks Template.md")
ROUTINE_HEADING = "## Routine"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_TAG_RE = re.compile(r"\s*#(mon|tue|wed|thu|fri|sat|sun|weekday|weekend)\b", re.I)
TASK_RE = re.compile(r"^(\s*)([-*]\s+)\[( |x|X)\]\s?(.*)$")
EMBED_RE = re.compile(r"^\s*!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]\s*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
INDENT = "\t"


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def note_path(vault, date):
    return os.path.join(vault, f"{date}.md")


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return f.read().split("\n")


def write_lines(path, lines):
    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def width(ws):
    return len(ws.expandtabs(4))


class Task:
    def __init__(self, i, ws, bullet, done, text):
        self.i, self.ws, self.bullet, self.done, self.text = i, ws, bullet, done, text
        self.depth = width(ws)
        self.id = ""
        self.end = i + 1  # exclusive end of block
        self.attachments = []
        self.children = []


def parse(lines):
    """Return flat list of Tasks (document order) with ids, blocks, children."""
    tasks = []
    for i, line in enumerate(lines):
        m = TASK_RE.match(line)
        if m:
            tasks.append(Task(i, m.group(1), m.group(2), m.group(3).lower() == "x", m.group(4)))
    # block ends: first later non-blank line with depth <= own depth
    for t in tasks:
        j = t.i + 1
        while j < len(lines):
            line = lines[j]
            if line.strip() and width(line[: len(line) - len(line.lstrip())]) <= t.depth:
                break
            j += 1
        while j > t.i + 1 and not lines[j - 1].strip():
            j -= 1
        t.end = j
    # tree by depth
    stack = []
    for t in tasks:
        while stack and stack[-1].depth >= t.depth:
            stack.pop()
        if stack:
            stack[-1].children.append(t)
        else:
            t.parent = None
        t.parent = stack[-1] if stack else None
        stack.append(t)
    roots = [t for t in tasks if t.parent is None]

    def number(ts, prefix):
        for k, t in enumerate(ts, 1):
            t.id = f"{prefix}{k}"
            number(t.children, t.id + ".")
    number(roots, "")
    # attachments: embeds directly in block, not inside a child's block
    for t in tasks:
        owned = set(range(t.i + 1, t.end))
        for c in t.children:
            owned -= set(range(c.i, c.end))
        for j in sorted(owned):
            m = EMBED_RE.match(lines[j])
            if m:
                t.attachments.append(m.group(1))
    return tasks


def find(tasks, tid):
    for t in tasks:
        if t.id == tid:
            return t
    die(f"no task {tid}")


def show(lines, date):
    tasks = parse(lines)
    print(f"# {date}")
    if not tasks:
        print("(no tasks)")
        return
    h = heading_index(lines)
    shown_heading = False
    for t in tasks:
        if h is not None and t.i > h and not shown_heading:
            print("    — routine —")
            shown_heading = True
        level = t.id.count(".")
        pad = "    " * level
        num = f"{t.id:>2}." if level == 0 else t.id
        print(f"{pad}{num} [{'x' if t.done else ' '}] {t.text}")
        for a in t.attachments:
            print(f"{pad}    📎 {a}")


def task_line(ws, text, done=False):
    return f"{ws}- [{'x' if done else ' '}] {text.strip()}"


def append_top(lines, text):
    h = heading_index(lines)
    roots = [t for t in parse(lines) if t.parent is None and (h is None or t.i < h)]
    new = task_line("", text)
    if roots:
        last = roots[-1]
        if last.text.strip() == "" and last.end == last.i + 1:
            lines[last.i] = new  # reuse an empty trailing checkbox
        else:
            lines.insert(last.end, new)
    elif h is not None:
        lines[h:h] = [new, ""]
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(new)


def heading_index(lines):
    for i, line in enumerate(lines):
        if line.strip() == ROUTINE_HEADING:
            return i
    return None


def append_block(lines, block, routine=False):
    """Append a whole block (head line + indented lines) as a top-level task."""
    if routine:
        h = heading_index(lines)
        if h is None:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", ROUTINE_HEADING]
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(block)
    else:
        append_top(lines, "")
        head = [t for t in parse(lines) if t.parent is None and t.text.strip() == ""][-1]
        lines[head.i : head.i + 1] = block


def append_in_block(lines, parent, line):
    lines.insert(parent.end, line)


def cmd_list(path, date, _a):
    show(read_lines(path), date)


def cmd_add(path, date, a):
    lines = read_lines(path)
    append_top(lines, a.text)
    write_lines(path, lines)
    show(lines, date)


def cmd_sub(path, date, a):
    lines = read_lines(path)
    p = find(parse(lines), a.id)
    append_in_block(lines, p, task_line(p.ws + INDENT, a.text))
    write_lines(path, lines)
    show(lines, date)


def cmd_toggle(path, date, a):
    lines = read_lines(path)
    t = find(parse(lines), a.id)
    lines[t.i] = f"{t.ws}{t.bullet}[{' ' if t.done else 'x'}] {t.text}"
    write_lines(path, lines)
    show(lines, date)


def cmd_edit(path, date, a):
    lines = read_lines(path)
    t = find(parse(lines), a.id)
    lines[t.i] = f"{t.ws}{t.bullet}[{'x' if t.done else ' '}] {a.text.strip()}"
    write_lines(path, lines)
    show(lines, date)


def cmd_delete(path, date, a):
    lines = read_lines(path)
    t = find(parse(lines), a.id)
    del lines[t.i : t.end]
    write_lines(path, lines)
    show(lines, date)


def cmd_attach(path, date, a):
    if not os.path.isfile(a.file):
        die(f"no such file: {a.file}")
    vault = os.path.dirname(path)
    lines = read_lines(path)
    t = find(parse(lines), a.id)
    os.makedirs(os.path.join(vault, ATTACH_DIR), exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(a.file))
    slug = re.sub(r"[^\w.-]+", "-", a.name or base).strip("-")
    name = f"{date} {slug}{ext.lower()}"
    dest = os.path.join(vault, ATTACH_DIR, name)
    n = 2
    while os.path.exists(dest):
        name = f"{date} {slug}-{n}{ext.lower()}"
        dest = os.path.join(vault, ATTACH_DIR, name)
        n += 1
    shutil.copy2(a.file, dest)
    lines.insert(t.i + 1, f"{t.ws}{INDENT}![[{name}]]")  # right under the task, before subtasks
    write_lines(path, lines)
    print(f"saved {ATTACH_DIR}/{name}")
    show(lines, date)


def cmd_carry(path, date, _a):
    lines = read_lines(path)
    carry_into(lines, path, date)
    write_lines(path, lines)
    show(lines, date)


def carry_into(lines, path, date):
    vault = os.path.dirname(path)
    earlier = sorted(f[:-3] for f in os.listdir(vault) if DATE_RE.match(f) and f[:-3] < date)
    src = None
    for cand in reversed(earlier):
        src_lines = read_lines(note_path(vault, cand))
        ts = [t for t in parse(src_lines) if t.text.strip()]
        if ts:
            src = cand
            break
    if src is None:
        die("no earlier daily note with tasks to carry from")
    undone = [t for t in ts if t.parent is None and not t.done]
    if not undone:
        print(f"nothing undone in {src}")
        return
    src_h = heading_index(src_lines)
    existing = {t.text.strip() for t in parse(lines) if t.parent is None}
    added = 0
    for t in undone:
        if t.text.strip() in existing:
            continue
        routine = src_h is not None and t.i > src_h
        append_block(lines, src_lines[t.i : t.end], routine=routine)
        added += 1
    print(f"carried {added} task(s) from {src}")


def routines_for(vault, date):
    """Template blocks that apply on `date`, with weekday tags stripped from the head line."""
    tpl = read_lines(os.path.join(vault, TEMPLATE))
    wd = WEEKDAYS[dt.date.fromisoformat(date).weekday()]
    out = []
    for t in parse(tpl):
        if t.parent is not None:
            continue
        tags = {m.lower() for m in DAY_TAG_RE.findall(t.text)}
        applies = (not tags or wd in tags
                   or ("weekday" in tags and wd in WEEKDAYS[:5])
                   or ("weekend" in tags and wd in WEEKDAYS[5:]))
        if not applies:
            continue
        clean = DAY_TAG_RE.sub("", t.text).strip()
        out.append([task_line("", clean)] + tpl[t.i + 1 : t.end])
    return out


def cmd_start(path, date, a):
    vault = os.path.dirname(path)
    lines = read_lines(path)
    carry_into(lines, path, date)
    existing = {t.text.strip() for t in parse(lines) if t.parent is None}
    added = 0
    for block in routines_for(vault, date):
        head = TASK_RE.match(block[0]).group(4).strip()
        if head in existing:
            continue
        append_block(lines, block, routine=True)
        added += 1
    print(f"added {added} routine(s)")
    write_lines(path, lines)
    show(lines, date)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=dt.date.today().isoformat())
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("add").add_argument("text")
    s = sub.add_parser("sub"); s.add_argument("id"); s.add_argument("text")
    sub.add_parser("toggle").add_argument("id")
    e = sub.add_parser("edit"); e.add_argument("id"); e.add_argument("text")
    sub.add_parser("delete").add_argument("id")
    at = sub.add_parser("attach"); at.add_argument("id"); at.add_argument("file")
    at.add_argument("--name", help="basename to use instead of the source file's")
    sub.add_parser("carry")
    sub.add_parser("start")
    a = p.parse_args()

    vault = os.environ.get("OBSIDIAN_VAULT", DEFAULT_VAULT)
    if not os.path.isdir(vault):
        die(f"vault not found: {vault}")
    path = note_path(vault, a.date)
    {"list": cmd_list, "add": cmd_add, "sub": cmd_sub, "toggle": cmd_toggle, "edit": cmd_edit,
     "delete": cmd_delete, "attach": cmd_attach, "carry": cmd_carry, "start": cmd_start}[a.cmd](path, a.date, a)


if __name__ == "__main__":
    main()
