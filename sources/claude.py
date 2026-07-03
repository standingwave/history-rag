"""Claude Code session source: genuine user prompts + assistant reply text.

Walks ~/.claude/projects/**/*.jsonl, dropping tool calls, tool results,
thinking blocks, and system/meta/sidechain lines.
"""
import json, glob, os, hashlib

ROOT = os.path.expanduser("~/.claude/projects")
MIN_CHARS = 40           # skip trivial messages
MAX_CHARS = 2000         # truncate giant messages (stay under embed token limit)

# Markers of synthetic / non-conversational user lines to drop entirely.
_JUNK_SUBSTRINGS = (
    "<command-name>", "<local-command-stdout>", "<command-message>",
    "<command-args>", "[Request interrupted", "Caveat: The messages below",
)

def _text_from_content(content, role) -> str:
    """Extract human-authored prompt text / assistant reply text only.

    Drops tool_result blocks (tool output recorded under user role), tool_use
    blocks, thinking blocks, and command/stdout wrappers.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    # A user message containing any tool_result block is tool output, not a
    # real prompt -> reject the whole message.
    if role == "user":
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return ""
    parts = []
    for b in content:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(parts)

def iter_turns(fp):
    """Yield (lineno, role, text, timestamp, cwd) for one session file's real
    conversational turns — the same filtering iter_chunks applies. Also used
    by the server's expand tool to show the conversation around a hit."""
    with open(fp) as f:
        for lineno, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "system":
                continue
            if obj.get("isMeta") or obj.get("isSidechain"):
                continue
            msg = obj.get("message", {})
            role = msg.get("role") or obj.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _text_from_content(msg.get("content", obj.get("content", "")), role)
            text = text.strip()
            if len(text) < MIN_CHARS:
                continue
            if any(j in text for j in _JUNK_SUBSTRINGS):
                continue
            yield lineno, role, text, obj.get("timestamp", ""), obj.get("cwd", "")

def iter_chunks():
    for fp in glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True):
        # project_hash = first path segment under ROOT, whether sessions sit
        # directly in the project dir or in a sessions/ subdir.
        rel = os.path.relpath(fp, ROOT)
        project_hash = rel.split(os.sep)[0]
        session_id = os.path.splitext(os.path.basename(fp))[0]
        for lineno, role, text, ts, cwd in iter_turns(fp):
            cid = hashlib.sha256(
                f"{session_id}:{lineno}:{text[:200]}".encode()
            ).hexdigest()[:32]
            yield cid, text[:MAX_CHARS], {
                "source": "claude",
                "timestamp": ts,
                "location": cwd,
                "meta": {
                    "session_id": session_id,
                    "project_hash": project_hash,
                    "role": role,
                    "lineno": lineno,
                },
            }
