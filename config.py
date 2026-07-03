"""Shared config for the Claude Code history RAG.

Settings resolve in precedence order: env var > config file > code default.
The config file is TOML at ~/.claude/history-rag.toml (path overridable via
CLAUDE_RAG_CONFIG); a missing file just means defaults, so zero-config
installs work unchanged. index.py and server.py both import from here so the
index is built and queried with the same model and dimensions; swapping the
embedding model requires re-indexing from scratch (old vectors won't match):

    CLAUDE_RAG_MODEL=mxbai-embed-large CLAUDE_RAG_DIM=1024 python index.py --rebuild
"""
import os, sys, tomllib

_CONFIG_PATH = os.path.expanduser(
    os.environ.get("CLAUDE_RAG_CONFIG", "~/.claude/history-rag.toml"))

_KNOWN = {
    "core": {"model", "dim", "db", "ollama"},
    "sources": {"enabled"},
    "shell": {"histfiles"},
    "browser": {"extra", "keep_params"},
    "git": {"roots", "author"},
    "obsidian": {"vaults"},
    "appusage": {"db"},
}

_FILE: dict = {}
if os.path.exists(_CONFIG_PATH):
    try:
        with open(_CONFIG_PATH, "rb") as f:
            _FILE = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        # A silently ignored config is worse than a crash.
        sys.exit(f"config error in {_CONFIG_PATH}: {e}")
    for _sec, _val in _FILE.items():
        if _sec not in _KNOWN:
            print(f"config: unknown section [{_sec}] in {_CONFIG_PATH}",
                  file=sys.stderr)
        elif isinstance(_val, dict):
            for _k in _val:
                if _k not in _KNOWN[_sec]:
                    print(f"config: unknown key {_sec}.{_k} in {_CONFIG_PATH}",
                          file=sys.stderr)

def get(section: str, key: str, env_var: str, default):
    """Resolve one setting: env var (raw string) > config file (TOML-typed)
    > default. Callers own type coercion for the env-string case."""
    if env_var:
        v = os.environ.get(env_var)
        if v is not None:
            return v
    return _FILE.get(section, {}).get(key, default)

def get_paths(section: str, key: str, env_var: str) -> list[str]:
    """A path-list setting: colon-separated string from env, list from file.
    Returns expanded absolute-ish paths, empties dropped."""
    v = get(section, key, env_var, [])
    items = v.split(":") if isinstance(v, str) else v
    return [os.path.expanduser(str(p)) for p in items if str(p).strip()]

EMBED_MODEL = get("core", "model", "CLAUDE_RAG_MODEL", "nomic-embed-text")
DIM = int(get("core", "dim", "CLAUDE_RAG_DIM", 768))
DB_PATH = os.path.expanduser(get("core", "db", "CLAUDE_RAG_DB",
                                 "~/.claude/history-rag.db"))
OLLAMA = str(get("core", "ollama", "CLAUDE_RAG_OLLAMA",
                 "http://localhost:11434")).rstrip("/") + "/api/embed"

# App-usage tracker (macOS): daemon writes here, sources/appusage.py reads it.
APPUSAGE_DB = os.path.expanduser(get("appusage", "db", "CLAUDE_RAG_APPUSAGE_DB",
                                     "~/.claude/appusage.db"))

# Which sources run; None means all (file-less installs unchanged).
ENABLED_SOURCES = _FILE.get("sources", {}).get("enabled")
