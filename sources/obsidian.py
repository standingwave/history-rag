"""Obsidian source: vault notes, chunked by heading section, then paragraph
group.

Vaults come from CLAUDE_RAG_OBSIDIAN_VAULTS (colon-separated paths; a vault
is a folder containing .obsidian/). Unset -> this source is a no-op. Within a
vault, hidden dirs (.obsidian, .trash, .git) and template folders are skipped.

Notes split into one section per #/##/### heading (deeper headings stay
inside their parent section), and each section into paragraph groups of up
to GROUP_MAX chars. Every chunk embeds as "note title > heading" + the group
text: a whole 300-word section collapsed into one vector averages toward the
corpus mean and away from any query, while the title/heading prefix carries
exactly the vocabulary recall queries use (measured: prefixing moved a
representative note's distance 0.94 -> 0.69 on a query naming its topic).
Notes short enough to be one thought stay whole, title-prefixed. Ids hash
vault+path+heading+occurrence+group — deliberately NOT the text — so editing
a section re-embeds it in place instead of orphaning chunks; deleting or
renaming a section (or shrinking its group count) orphans (--prune --source
obsidian cleans up).

Timestamp is the note's `date:` frontmatter when present, else file mtime.
Frontmatter dates may be bare (2026-07-07) or full datetimes with optional
offset; naive values mean the author's local time. Either way the stored
timestamp is UTC ISO — the server's window filters compare lexicographically
against UTC bounds, so a naive local stamp would land notes in the wrong day.
Frontmatter is stripped from the indexed text, and any section that looks
credential-bearing is dropped via the shared secret regex — personal notes
hold passwords more often than you'd think.
"""
import os, re, hashlib
from datetime import datetime, timezone
from sources.common import SECRET_RE, group_paragraphs

MAX_CHARS = 2000
WHOLE_NOTE_MAX = 1500        # notes at or under this stay one chunk
GROUP_MAX = 700              # paragraph-group budget within a section
_HEADING_RE = re.compile(r"^#{1,3} +(.*)$", re.M)
_DATE_RE = re.compile(r"^date:\s*(\d{4}-\d{2}-\d{2}"
                      r"(?:[T ]\d{2}:\d{2}(?::\d{2})?"
                      r"(?:Z|[+-]\d{2}:?\d{2})?)?)", re.M)
_SKIP_DIRS = {".trash", "templates", "template"}

def _vaults():
    import config
    return config.get_paths("obsidian", "vaults", "CLAUDE_RAG_OBSIDIAN_VAULTS")

def _strip_frontmatter(raw: str):
    """Return (body, date-from-frontmatter-or-None)."""
    if raw.startswith("---\n"):
        end = raw.find("\n---", 4)
        if end != -1:
            m = _DATE_RE.search(raw[4:end])
            nl = raw.find("\n", end + 1)
            body = raw[nl + 1:] if nl != -1 else ""
            return body, (m.group(1) if m else None)
    return raw, None

def _sections(body: str):
    """Yield (heading, text) per #/##/### section; text keeps its heading line.
    Content before the first heading is its own section."""
    marks = list(_HEADING_RE.finditer(body))
    if not marks:
        yield "", body
        return
    if body[:marks[0].start()].strip():
        yield "", body[:marks[0].start()]
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        yield m.group(1).strip(), body[m.start():end]

def _groups(text: str):
    return group_paragraphs(text, GROUP_MAX)

def _fm_iso(fm_date: str) -> str:
    """Frontmatter date -> UTC ISO string ('' if unparseable). Bare dates
    become local midnight; naive datetimes are local time."""
    try:
        dt = datetime.fromisoformat(fm_date.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat()

def _mtime_iso(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path),
                                      tz=timezone.utc).isoformat()
    except OSError:
        return ""

def iter_chunks():
    for vault in _vaults():
        if not os.path.isdir(vault):
            continue
        vname = os.path.basename(vault.rstrip("/"))
        for dirpath, dirnames, filenames in os.walk(vault):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d.lower() not in _SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith(".md"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, errors="replace") as f:
                        raw = f.read()
                except OSError:
                    continue
                body, fm_date = _strip_frontmatter(raw)
                if not body.strip():
                    continue
                ts = (_fm_iso(fm_date) if fm_date else "") or _mtime_iso(path)
                rel = os.path.relpath(path, vault)
                title = os.path.splitext(fn)[0]
                whole = len(body) <= WHOLE_NOTE_MAX
                secs = [("", body)] if whole else _sections(body)
                counts: dict[str, int] = {}
                for heading, sec_text in secs:
                    if heading:      # the prefix below carries the heading
                        sec_text = sec_text.split("\n", 1)[1] \
                            if "\n" in sec_text else ""
                    prefix = f"{title} > {heading}" if heading else title
                    # occurrence index disambiguates repeated headings
                    n = counts.get(heading, 0)
                    counts[heading] = n + 1
                    groups = [sec_text] if whole else _groups(sec_text)
                    for g, gtext in enumerate(groups):
                        text = f"{prefix}\n{gtext.strip()}"[:MAX_CHARS]
                        if not gtext.strip() or SECRET_RE.search(text):
                            continue
                        cid = "obsidian:" + hashlib.sha256(
                            f"{vname}\0{rel}\0{heading}\0{n}\0{g}"
                            .encode()).hexdigest()[:26]
                        yield cid, text, {
                            "source": "obsidian",
                            "timestamp": ts,
                            "location": rel + (f"#{heading}" if heading else ""),
                            "meta": {"path": rel, "heading": heading,
                                     "vault": vname},
                        }
