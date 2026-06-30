# Claude Code history RAG

Local semantic search over your `~/.claude` session history, exposed to Claude
Code as an MCP `search_history` tool. Global scope, full metadata. Everything
runs on your machine; chat text never leaves it.

## Quickstart
For the impatient (full detail in the numbered sections below):
```bash
brew install ollama && brew services start ollama
ollama pull nomic-embed-text
uv venv ~/.claude/rag-venv
uv pip install --python ~/.claude/rag-venv/bin/python -r requirements.txt
~/.claude/rag-venv/bin/python index.py            # build the index
claude mcp add history -- ~/.claude/rag-venv/bin/python "$(pwd)/server.py"
```

## Layout
- `config.py` — shared settings (model, dimensions, DB path, Ollama URL), each
  overridable by env var. Imported by both scripts so build and query always agree.
- `index.py` — scans `~/.claude/projects/**/*.jsonl`, filters to real prompts +
  assistant text, embeds via Ollama, writes `~/.claude/history-rag.db`.
- `server.py` — the MCP server exposing `search_history`.
- `inspect_sessions.py` — one-off: dumps the JSONL shape so you can confirm the
  parser matches your session files.

## 1. Prereqs

### Install Ollama
**macOS** (Homebrew, gives easy updates):
```bash
brew install ollama
brew services start ollama      # runs the daemon in the background
```
Or download the .dmg from https://ollama.com/download/mac and drag to
Applications (launch it once so the menu-bar daemon starts).

**Linux:**
```bash
curl -fsSL https://ollama.com/install.sh | sh   # sets up a systemd service
```

Verify the daemon is up (the indexer/server talk to it on port 11434):
```bash
ollama --version
curl http://localhost:11434/api/tags   # should return JSON, not connection refused
```

### Pull the embedding model + Python deps
```bash
ollama pull nomic-embed-text          # 768-dim, fast
```

**Using uv (recommended):**
```bash
uv venv ~/.claude/rag-venv
uv pip install --python ~/.claude/rag-venv/bin/python -r requirements.txt
```
(`requirements.txt` is just `sqlite-vec`, `requests`, `mcp[cli]`.)
uv resolves to prebuilt wheels, avoiding the Rust/maturin source builds that
break on Apple Silicon. Run index.py and register server.py with this venv's
interpreter: `~/.claude/rag-venv/bin/python`.

**Don't have pip and not using uv?** First get Python (it bundles pip). On macOS:
```bash
brew install python                   # installs python3 + pip3
python3 -m pip --version              # verify
```
Then install the deps (use `pip3`, or `python3 -m pip` if `pip` isn't on PATH):
```bash
python3 -m pip install -r requirements.txt
```

If `brew install python` warns about an "externally-managed environment" when
installing the deps, use a venv instead:
```bash
python3 -m venv ~/.claude/rag-venv
source ~/.claude/rag-venv/bin/activate
pip install -r requirements.txt
```
If you use a venv, run index.py and register server.py with that venv's
python: `~/.claude/rag-venv/bin/python`.

## 2. Inspect (do this first)
Confirms the JSONL field names match the parser. If your output shows
different keys (e.g. content not under `message.content`), tweak
`text_from_content` / `iter_messages` in index.py.
```bash
~/.claude/rag-venv/bin/python inspect_sessions.py
```

## 3. Build the index
Use the venv interpreter you installed deps into (bare `python` won't see them).

First preview what survives the filter (keeps only real user prompts +
assistant text; drops tool calls, tool results, thinking, system/meta lines):
```bash
~/.claude/rag-venv/bin/python index.py --dry-run
```
If that looks right, build:
```bash
~/.claude/rag-venv/bin/python index.py            # incremental (safe to re-run)
~/.claude/rag-venv/bin/python index.py --rebuild  # wipe + reindex from scratch
```
Writes `~/.claude/history-rag.db`.

## 4. Register the MCP server with Claude Code
Use the venv interpreter (bare `python` won't find the deps) and an absolute
path to server.py:
Run this from the repo directory — `$(pwd)` fills in the absolute path to
server.py (the registration needs an absolute path, not a relative one):
```bash
claude mcp add history -- ~/.claude/rag-venv/bin/python "$(pwd)/server.py"
```
Confirm it registered:
```bash
claude mcp list          # 'history' should appear
```
Then in a session, Claude can call `search_history("that proxy bug we hit", k=5)`.

## 5. Keep it fresh
The index only reflects sessions present at last run. Pick one:

**cron** — edit your crontab with `crontab -e`, then add a line. cron has a
minimal PATH and no `~` expansion, so use absolute paths. Get yours with
`echo "$HOME/.claude/rag-venv/bin/python $(pwd)/index.py"` and paste the result:
```cron
# refresh the history index every 30 min; log output for debugging
*/30 * * * * /ABS/PATH/rag-venv/bin/python /ABS/PATH/index.py >> $HOME/.claude/rag-index.log 2>&1
```
Note: cron needs the Ollama server running to embed new chunks. After saving,
check it fired by tailing the log:
```bash
tail -f ~/.claude/rag-index.log
```
On macOS, cron may need Full Disk Access (System Settings → Privacy & Security →
Full Disk Access → add `/usr/sbin/cron`) to read `~/.claude`.

**manual** — run when you want it current:
```bash
~/.claude/rag-venv/bin/python index.py
```

**file-watcher** — watch `~/.claude/projects/**/*.jsonl` and trigger index.py
on change (e.g. with `fswatch` or a launchd WatchPaths agent).

## 6. Verify it works inside a Claude Code session
After registering (step 4) and indexing (step 3):

1. **Confirm the server is connected.** In a session, run the MCP status command:
   ```
   /mcp
   ```
   You should see `history` listed as connected with a `search_history` tool.

2. **Ask Claude something that needs your history.** Natural-language prompts
   that force a lookup work best — Claude will call the tool on its own:
   ```
   Search my past sessions: what did we decide about the sqlite-vec schema?
   What have I worked on involving Ollama and embeddings?
   Have I hit an Apple Silicon / Rosetta issue before? What fixed it?
   ```
   Claude should invoke `search_history` and cite matched snippets with their
   session id / timestamp / project.

3. **Call the tool explicitly** if you want to test it directly:
   ```
   Use the search_history tool with query "Attio CRM setup" and k=5
   ```

4. **Sanity-check the raw DB** (outside Claude Code) to confirm rows exist:
   ```bash
   ~/.claude/rag-venv/bin/python - <<'PY'
   import sqlite3, sqlite_vec, os
   db = sqlite3.connect(os.path.expanduser("~/.claude/history-rag.db"))
   db.enable_load_extension(True); sqlite_vec.load(db)
   print("chunks:", db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
   for row in db.execute("SELECT role, timestamp, substr(text,1,70) FROM chunks LIMIT 5"):
       print(row)
   PY
   ```

**Troubleshooting:**
- `/mcp` doesn't list `history` → re-check `claude mcp list`; the path to
  server.py must be absolute and the interpreter must be the venv's.
- Tool errors with a connection error → Ollama isn't running (the server
  embeds your query at call time). Start it: `open -a Ollama`.
- Tool returns nothing → the index is empty or stale; re-run index.py.

## Notes
- Each message is one chunk (role/timestamp/session preserved). For long
  assistant turns you may later want sliding-window chunking; per-message is
  fine to start.
- `nomic-embed-text` is the speed pick. For higher quality, set the env vars
  (in both your indexing shell and the MCP registration) and re-index:
  ```bash
  ollama pull mxbai-embed-large
  CLAUDE_RAG_MODEL=mxbai-embed-large CLAUDE_RAG_DIM=1024 ~/.claude/rag-venv/bin/python index.py --rebuild
  ```
  Other overrides: `CLAUDE_RAG_DB`, `CLAUDE_RAG_OLLAMA`. See `config.py`.
- Distance is cosine-ish; lower = closer.
