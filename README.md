# Claude Code history RAG

Local semantic search over your history — Claude Code sessions and shell command
history — exposed to Claude Code as an MCP `search_history` tool. Everything is
indexed into one vector space, so a single query ranks chat turns and terminal
commands together. Runs entirely on your machine; nothing leaves it.

> Setting this up by handing it to your coding agent? Point it at
> [`AGENT_SETUP.md`](AGENT_SETUP.md) instead — that's the agent runbook. This
> README is the human walkthrough.

## Quickstart
For the impatient (full detail in the numbered sections below):
```bash
git clone https://github.com/standingwave/history-rag.git && cd history-rag
brew install ollama && brew services start ollama
ollama pull nomic-embed-text
uv venv ~/.claude/rag-venv
uv pip install --python ~/.claude/rag-venv/bin/python -r requirements.txt
~/.claude/rag-venv/bin/python index.py            # build the index
claude mcp add history -- ~/.claude/rag-venv/bin/python "$(pwd)/server.py"
```

## Layout
- `config.py` — shared settings (model, dimensions, DB path, Ollama URL), each
  overridable by env var. Imported everywhere so build and query always agree.
- `index.py` — driver: pulls chunks from every source in `SOURCES`, embeds via
  Ollama, writes `~/.claude/history-rag.db`.
- `sources/` — one module per content source, each yielding `(id, text, record)`:
  - `claude.py` — Claude Code session prompts + assistant text.
  - `shell.py` — bash + zsh command history, deduped.
  - `appusage.py` — daily per-app time from the tracker (macOS, optional).
- `appusage/` — optional macOS app-usage tracker: a `launchd` daemon that logs
  how long you spend in each app. See "App usage" below.
- `server.py` — the MCP server exposing `search_history`.
- `inspect_sessions.py` — one-off: dumps the JSONL shape so you can confirm the
  Claude parser matches your session files.
- [`TESTING.md`](TESTING.md) — recommended plan for adding a test suite.

## Sources
Every source feeds one shared index; pass `source="claude"`, `source="shell"`,
or `source="appusage"` to `search_history` to restrict a query.

**Shell history** reads `~/.zsh_history`, `~/.bash_history`, and the per-session
snapshots macOS keeps in `~/.zsh_sessions/` and `~/.bash_sessions/`. Live history
files are capped by your shell's `SAVEHIST`/`HISTSIZE`, but the session snapshots
reach further back. For history archived elsewhere (old machines, backups), point
`CLAUDE_RAG_HISTFILES` at the extra files (colon-separated):
```bash
CLAUDE_RAG_HISTFILES="$HOME/backups/zsh_history.2019:$HOME/backups/bash_history.old" \
  ~/.claude/rag-venv/bin/python index.py
```
Identical commands collapse to one entry (with a run count); trivial commands
(`ls`, `cd`, …) are dropped, and anything that looks like it contains a secret
(passwords, tokens, API keys, `user:pass@host` URLs) is skipped so it's never
embedded. Command timestamps only appear if zsh recorded them (`setopt
EXTENDED_HISTORY`); bash needs `HISTTIMEFORMAT` set.

**App usage (macOS, optional).** A small tracker records how long you spend in
each app so you can later ask "what was I doing the week I built X?". It's off
until you install the daemon; the `appusage` source yields nothing without it.

The daemon samples the frontmost app and idle time every 20s (via `lsappinfo`
and `ioreg` — no extra deps, no permissions), coalesces same-app stretches into
segments in `~/.claude/appusage.db`, and doesn't count idle (>2 min) or sleep
time. `sources/appusage.py` feeds daily per-app totals (≥1 min) into the index,
today included: the indexer re-embeds any chunk whose text changed, so today's
growing total stays current while finished days settle once.

Install it as a `launchd` agent (fills the plist's absolute-path placeholders,
then loads it):
```bash
PY=~/.claude/rag-venv/bin/python
DAEMON="$(pwd)/appusage/daemon.py"
sed -e "s#__PYTHON__#$PY#" -e "s#__DAEMON__#$DAEMON#" \
  appusage/com.user.appusage.plist > ~/Library/LaunchAgents/com.user.appusage.plist
launchctl load ~/Library/LaunchAgents/com.user.appusage.plist
```
See what it's captured any time (independent of the index):
```bash
~/.claude/rag-venv/bin/python appusage/report.py        # today + last 7 days
```
To stop and remove it:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.appusage.plist
rm ~/Library/LaunchAgents/com.user.appusage.plist
```
Tuning: `APPUSAGE_INTERVAL` (sample seconds) and `APPUSAGE_IDLE` (idle cutoff)
as env vars in the plist. Data is local, like everything else here.

**Adding a source:** drop a module in `sources/` with an `iter_chunks()`
generator that yields `(id, text, {"source", "timestamp", "location", "meta"})`,
then add it to `SOURCES` in `index.py`. The `id` must be stable across runs so
indexing stays incremental.

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
`_text_from_content` / `iter_chunks` in `sources/claude.py`.
```bash
~/.claude/rag-venv/bin/python inspect_sessions.py
```

## 3. Build the index
Use the venv interpreter you installed deps into (bare `python` won't see them).

First preview what survives the filter across all sources (Claude keeps real
prompts + assistant text, dropping tool calls/results/thinking/meta; shell keeps
deduped non-trivial commands):
```bash
~/.claude/rag-venv/bin/python index.py --dry-run
```
If that looks right, build:
```bash
~/.claude/rag-venv/bin/python index.py            # incremental (safe to re-run)
~/.claude/rag-venv/bin/python index.py --rebuild  # wipe + reindex from scratch
```
Writes `~/.claude/history-rag.db`. Use `--rebuild` after changing the embedding
model or the chunk schema (e.g. adding a source) — the table layout changes, so
an incremental run against an old DB won't work.

## 4. Register the MCP server with Claude Code
Run this from the repo directory, using the venv interpreter (bare `python`
won't find the deps). `$(pwd)` fills in the absolute path to server.py (the
registration needs an absolute path, not a relative one):
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
   You should see `history` listed as connected, with `search_history` and
   `history_stats` tools. (`history_stats` reports per-source counts and date
   coverage — a quick way for Claude to see what's indexed before searching.)

2. **Ask Claude something that needs your history.** Natural-language prompts
   that force a lookup work best — Claude will call the tool on its own:
   ```
   Search my past sessions: what did we decide about the sqlite-vec schema?
   What have I worked on involving Ollama and embeddings?
   What's that ffmpeg command I used to convert a webm? (search my shell history)
   ```
   Claude should invoke `search_history` and cite matched snippets with their
   source / timestamp / location.

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
   for row in db.execute("SELECT source, COUNT(*) FROM chunks GROUP BY source"):
       print(row)
   for row in db.execute("SELECT source, timestamp, substr(text,1,70) FROM chunks LIMIT 5"):
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
- One chunk per Claude message, per unique shell command, and per day-per-app.
  For long assistant turns you may later want sliding-window chunking;
  per-message is fine to start.
- Indexing is incremental and self-healing: a chunk is re-embedded only when its
  text changed, so growing app-usage totals stay current without a rebuild.
- `nomic-embed-text` is the speed pick. For higher quality, set the env vars
  (in both your indexing shell and the MCP registration) and re-index:
  ```bash
  ollama pull mxbai-embed-large
  CLAUDE_RAG_MODEL=mxbai-embed-large CLAUDE_RAG_DIM=1024 ~/.claude/rag-venv/bin/python index.py --rebuild
  ```
  Other overrides: `CLAUDE_RAG_DB`, `CLAUDE_RAG_OLLAMA`. See `config.py`.
- `search_history` returns `{query, count, results[]}`, ranked best-first, with
  a `distance` (L2; lower = closer) on each hit. `history_stats` reports the
  corpus. Filter a search with `source=` and trim noise with `max_distance=`.
