"""Shared config for the Claude Code history RAG.

index.py and server.py both import these so the index is built and queried with
the same model and dimensions. Each value can be overridden by env var; swapping
the embedding model requires re-indexing from scratch (old vectors won't match):

    CLAUDE_RAG_MODEL=mxbai-embed-large CLAUDE_RAG_DIM=1024 python index.py --rebuild
"""
import os

EMBED_MODEL = os.environ.get("CLAUDE_RAG_MODEL", "nomic-embed-text")
DIM = int(os.environ.get("CLAUDE_RAG_DIM", "768"))
DB_PATH = os.path.expanduser(os.environ.get("CLAUDE_RAG_DB", "~/.claude/history-rag.db"))
OLLAMA = os.environ.get("CLAUDE_RAG_OLLAMA", "http://localhost:11434").rstrip("/") + "/api/embed"

# App-usage tracker (macOS): daemon writes here, sources/appusage.py reads it.
APPUSAGE_DB = os.path.expanduser(os.environ.get("CLAUDE_RAG_APPUSAGE_DB", "~/.claude/appusage.db"))
