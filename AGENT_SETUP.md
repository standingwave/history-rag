# Agent runbook: install the history RAG

**You are an AI coding agent setting this tool up on the user's machine.** Work
through the phases in order. Each step is *detect → act → verify*: check current
state first, act only if needed, and confirm before moving on. Do the work
yourself rather than handing the user shell commands. Report a short summary at
the end.

This is the agent process. The human-facing version is `README.md` — don't make
the user follow that; follow this instead.

## When to ask the user
Pause and ask at these points (use the default if they defer). Ask each one when
you reach the phase that needs it — don't dump all questions at once. Everything
not on this list, decide yourself with sensible defaults.

| # | Ask | Phase | Default if they defer |
|---|-----|-------|-----------------------|
| Q1 | Which sources to index — Claude sessions, shell history, or both? | 0 | Both |
| Q2 | OK to index shell history? It can contain sensitive commands (secret redaction is on, but confirm). | 0 | Index it |
| Q3 | Any archived history elsewhere (old machines, backups) to include via `CLAUDE_RAG_HISTFILES`? | 0 | Live + macOS session dirs only |
| Q4 | Permission to install software (Ollama, Homebrew packages) if missing? | 1 | Ask before any system install — no silent installs |
| Q5 | Embedding model: fast (`nomic-embed-text`) or higher-quality (`mxbai-embed-large`, slower, larger)? | 1 | `nomic-embed-text` |
| Q6 | Rebuild confirmation IF an index already exists with data (rebuild wipes it). | 3 | Don't rebuild; do an incremental run |
| Q7 | MCP registration scope — `user` (all your projects) or `project` (just this repo)? | 5 | `user` |
| Q8 | Set up automatic refresh (cron), and at what interval? | 6 | Don't install one; mention manual refresh |
| Q9 | (macOS only) Install the app-usage tracker? It's a persistent `launchd` daemon that logs how long they spend in each app. | 7 | Don't install — mention it's available |

STOP points (notify, don't ask — these need a human action you can't perform)
are called out inline, e.g. reconnecting the MCP server in Phase 5.

## Phase 0 — Locate the code & scope the work
Work from the repo root (the directory containing `server.py`, `config.py`,
`index.py`, and `sources/`). Confirm with `ls`. If you don't have the code yet,
clone it first and `cd` in:
```bash
git clone https://github.com/standingwave/history-rag.git && cd history-rag
```
All paths below are relative to this root.

Note the platform: `uname` (`Darwin` = macOS, `Linux` = Linux). It changes only
the Ollama install command and whether shell session-snapshot dirs exist.

**ASK → Q1, Q2, Q3.** Settle sources, get explicit consent for shell history,
and collect any archived history paths before indexing anything. If they decline
a source, you'll trim `SOURCES` in `index.py` accordingly in Phase 3.

## Phase 1 — Ollama + embedding model
1. Detect: `ollama --version` and `curl -s http://localhost:11434/api/tags`.
2. If not installed — **ASK → Q4 before installing**, then:
   - macOS: `brew install ollama && brew services start ollama`
   - Linux: `curl -fsSL https://ollama.com/install.sh | sh`
3. If installed but the daemon is down: start it (`brew services start ollama`,
   or `ollama serve &` on Linux), then re-check the `curl` returns JSON.
4. **ASK → Q5** for the model. Pull it if absent: `ollama pull <model>`
   (default `nomic-embed-text`). If they chose a non-default model, you'll set
   `CLAUDE_RAG_MODEL`/`CLAUDE_RAG_DIM` consistently in Phases 3 and 5 (see
   guardrails) — `mxbai-embed-large` is dim 1024.

Verify: the `curl` to `/api/tags` returns JSON listing the chosen model.

## Phase 2 — venv + dependencies
Canonical venv path is `~/.claude/rag-venv` (keep it here, not in the repo —
the MCP server is registered with an absolute interpreter path, so the venv must
not move when the repo moves).
1. If `~/.claude/rag-venv/bin/python` is missing, create it. Prefer uv:
   `uv venv ~/.claude/rag-venv` then
   `uv pip install --python ~/.claude/rag-venv/bin/python -r requirements.txt`.
   No uv: `python3 -m venv ~/.claude/rag-venv` then
   `~/.claude/rag-venv/bin/pip install -r requirements.txt`.
2. From here on, ALWAYS invoke `~/.claude/rag-venv/bin/python` — never bare
   `python`/`python3`, which won't see the deps.

Verify: `~/.claude/rag-venv/bin/python -c "import sqlite_vec, requests, mcp; print('deps ok')"`.

## Phase 3 — Build the index
1. Apply the Q1 source choice: if they excluded a source, edit `SOURCES` in
   `index.py` to list only the wanted modules. If they gave archived paths (Q3),
   export `CLAUDE_RAG_HISTFILES="path1:path2"` for the index command.
2. Optional, only if `~/.claude/projects` exists and Claude is a chosen source:
   run `~/.claude/rag-venv/bin/python inspect_sessions.py` and confirm the JSONL
   shape matches the parser. If keys differ, adjust `_text_from_content` /
   `iter_chunks` in `sources/claude.py` before building.
3. Dry-run: `~/.claude/rag-venv/bin/python index.py --dry-run`. Expect the
   chosen sources' lines. Zero lines means the filters rejected everything —
   investigate, don't proceed.
4. **Check for an existing index** (`~/.claude/history-rag.db`). If it exists and
   has rows, an incremental `index.py` is safe. Only `--rebuild` (which wipes it)
   if the schema/model changed — and **ASK → Q6** first.
5. Build: `~/.claude/rag-venv/bin/python index.py` (add `--rebuild` only per
   step 4). Needs Ollama up. Prefix with the chosen model's env vars if non-default.

Verify (raw DB, no MCP needed):
```bash
~/.claude/rag-venv/bin/python - <<'PY'
import sqlite3, os
db = sqlite3.connect(os.path.expanduser("~/.claude/history-rag.db"))
print("total:", db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
for r in db.execute("SELECT source, COUNT(*) FROM chunks GROUP BY source"): print(r)
PY
```
Expect a non-zero total covering the chosen sources.

## Phase 4 — Verify search works (in-process)
Do NOT verify by calling the `search_history` MCP tool yet — a server you just
registered is not callable in the session that registered it, and any already-
running instance is stale. Instead exercise the server module directly:
```bash
~/.claude/rag-venv/bin/python -c "import server; print(server.search_history('test', k=2))"
```
Expect a JSON array of hits, each with `source`/`location`/`distance`. An empty
array on a non-empty DB, or a `no such column` error, means the DB and the code
disagree → rebuild (Phase 3, with Q6 consent) and retry.

## Phase 5 — Register the MCP server
**ASK → Q7** for scope, then run from the repo root so `$(pwd)` is absolute:
```bash
claude mcp add history -s user -- ~/.claude/rag-venv/bin/python "$(pwd)/server.py"
```
(Use `-s project` instead if they chose project scope. If a non-default model was
chosen, add `--env CLAUDE_RAG_MODEL=… --env CLAUDE_RAG_DIM=…` so the server
embeds queries with the same model the index was built with.) If `history` is
already registered with the wrong path, `claude mcp remove history` first.
Confirm with `claude mcp list`.

**STOP — tell the human (don't ask):** the tool is registered but won't be
callable until they reconnect it (`/mcp` → reconnect `history`) or restart their
session. They must do this; you cannot. After they do, `search_history("…", k=5)`
works, with optional `source="claude"|"shell"`.

## Phase 6 — Keep it fresh
**ASK → Q8.** If they want automatic refresh:
- **macOS — prefer launchd** (survives sleep, no Full Disk Access hassle). Fill
  the plist placeholders and load it:
  ```bash
  PY=~/.claude/rag-venv/bin/python
  sed -e "s#__PYTHON__#$PY#" -e "s#__INDEX__#$(pwd)/index.py#" \
    com.user.history-index.plist > ~/Library/LaunchAgents/com.user.history-index.plist
  launchctl load ~/Library/LaunchAgents/com.user.history-index.plist
  ```
  Adjust cadence via `StartInterval` in the plist. Verify with
  `launchctl list | grep history-index` and `tail /tmp/history-index.log`.
- **Linux — cron** (absolute paths; cron has no `~` and a minimal PATH):
  ```cron
  */30 * * * * /ABS/rag-venv/bin/python /ABS/repo/index.py >> $HOME/.claude/rag-index.log 2>&1
  ```
Either way it needs Ollama running. If they declined, tell them the manual
refresh command (`index.py`) and move on.

## Phase 7 — Optional: app-usage tracker (macOS only)
Skip on Linux. This is a persistent background daemon that logs the frontmost
app over time — privacy-relevant — so **ASK → Q9** and install ONLY on a clear
yes. It's independent of the rest; the `appusage` source is a no-op without it.
1. Fill the plist placeholders with absolute paths and load it:
   ```bash
   PY=~/.claude/rag-venv/bin/python
   sed -e "s#__PYTHON__#$PY#" -e "s#__DAEMON__#$(pwd)/appusage/daemon.py#" \
     appusage/com.user.appusage.plist > ~/Library/LaunchAgents/com.user.appusage.plist
   launchctl load ~/Library/LaunchAgents/com.user.appusage.plist
   ```
2. Verify it's running: `launchctl list | grep com.user.appusage` (a PID in the
   first column means it's up), and `/tmp/appusage-daemon.log` has no errors.
3. Tell them: data lands in `~/.claude/appusage.db`; `appusage/report.py` shows
   totals; finished days flow into the index on the next `index.py` run. To
   remove: `launchctl unload …` then delete the plist.

## Guardrails — do not violate
- **Never disable the secret redaction** in `sources/shell.py`. It drops
  commands containing passwords/tokens/keys so they're never embedded.
- **Never commit `~/.claude/history-rag.db`** or the venv. The DB lives outside
  the repo by design; the venv and `*.db` are gitignored.
- **Model/dim must match between index and queries.** Centralized in `config.py`.
  To switch models you MUST `index.py --rebuild` AND register the server with the
  same `CLAUDE_RAG_MODEL`/`CLAUDE_RAG_DIM`. Mismatched dims = broken search.
- **Schema change ⇒ `--rebuild`.** Adding a source or changing columns makes
  incremental runs against an old DB fail; rebuild from scratch (with Q6 consent).
- **Adding a new source:** create a module in `sources/` exposing
  `iter_chunks()` that yields `(id, text, {"source","timestamp","location","meta"})`
  with a run-stable `id`, then add it to `SOURCES` in `index.py`. Don't bolt new
  source logic onto the existing modules.
- **Don't read the user's raw history/session files** to answer questions unless
  they ask you to — that's what `search_history` is for.
