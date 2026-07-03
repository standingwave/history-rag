# Agent runbook: install the history RAG

**You are an AI coding agent setting this tool up on the user's machine.** Work
through the phases in order; each step is *detect → act → verify*. Do the work
yourself rather than handing the user commands. Report a short summary at the
end. (`README.md` is the human-facing version — follow this instead.)

## When to ask the user
Ask each question when its phase needs it, not all at once; use the default if
they defer. Everything else, decide yourself with sensible defaults.

| # | Ask | Phase | Default if they defer |
|---|-----|-------|-----------------------|
| Q1 | Which sources? claude sessions, shell, browser history, git commits, Obsidian notes, app usage (macOS daemon, see Q12) | 0 | claude + shell + browser (the zero-config ones) |
| Q2 | OK to index shell history? Can contain sensitive commands (redaction is on, but confirm). Mention atuin if `~/.local/share/atuin/history.db` exists — it adds timestamps, cwd, and exit codes | 0 | Index it (atuin included when present) |
| Q3 | OK to index browser history? Privacy-relevant; covers all profiles of Safari/Chrome/Helium found | 0 | Index it |
| Q4 | Archived shell history to include (`[shell] histfiles`)? | 0 | Live + macOS session dirs only |
| Q5 | If git chosen: which roots to scan (`[git] roots`)? | 0 | Skip the git source |
| Q6 | If Obsidian chosen: which vault paths (`[obsidian] vaults`)? | 0 | Skip the obsidian source |
| Q7 | Permission to install software (Ollama, brew packages) if missing? | 1 | Ask before any system install |
| Q8 | Embedding model: fast (`nomic-embed-text`) or higher-quality (`mxbai-embed-large`, dim 1024)? | 1 | `nomic-embed-text` |
| Q9 | Rebuild confirmation IF an index already exists with data (rebuild wipes it) | 3 | Incremental run, no rebuild |
| Q10 | MCP scope — `user` (all projects) or `project` (this repo)? | 5 | `user` |
| Q11 | Automatic refresh (launchd/cron), what interval? | 6 | Don't install; mention manual refresh |
| Q12 | (macOS) Install the app-usage tracker? Persistent daemon logging the frontmost app | 7 | Don't install — mention it's available |

STOP points (notify, don't ask — human actions you can't perform) are called
out inline: MCP reconnect (Phase 5), Full Disk Access and the Documents
permission prompt (Phases 3/6).

## Phase 0 — Locate the code & scope the work
Work from the repo root (has `server.py`, `config.py`, `index.py`, `sources/`).
If you don't have it: `git clone https://github.com/standingwave/history-rag.git && cd history-rag`.
Note the platform (`uname`); it changes the Ollama install and macOS-only bits.
**ASK → Q1–Q6.** Collect source choices, consents, and paths before indexing.

## Phase 1 — Ollama + embedding model
1. Detect: `ollama --version`; `curl -s http://localhost:11434/api/tags`.
2. Missing → **ASK Q7**, then macOS `brew install ollama && brew services start ollama`;
   Linux `curl -fsSL https://ollama.com/install.sh | sh`.
3. Daemon down → start it, re-check the curl returns JSON.
4. **ASK Q8**; `ollama pull <model>` if absent. Non-default model ⇒ carry
   `CLAUDE_RAG_MODEL`/`CLAUDE_RAG_DIM` through Phases 3 and 5 (see guardrails).

## Phase 2 — venv + dependencies
Canonical venv: `~/.claude/rag-venv` (outside the repo — the MCP registration
uses an absolute interpreter path). If missing:
`uv venv ~/.claude/rag-venv && uv pip install --python ~/.claude/rag-venv/bin/python -r requirements.txt`
(no uv: `python3 -m venv` + its `pip install`). From here on ALWAYS invoke
`~/.claude/rag-venv/bin/python`, never bare `python`.
Verify: `~/.claude/rag-venv/bin/python -c "import sqlite_vec, requests, mcp; print('deps ok')"`.

## Phase 3 — Build the index
1. Apply choices by writing `~/.claude/history-rag.toml` (read by every entry
   point — interactive, launchd, MCP server; precedence env > file > default):
   ```toml
   [sources]
   enabled = ["claude", "shell", "browser"]   # only what they chose
   [git]
   roots = ["~/dev"]                          # Q5 answer, if git chosen
   [obsidian]
   vaults = ["~/path/to/Vault"]               # Q6 answer, if chosen
   [shell]
   histfiles = []                             # Q4 archived paths, if any
   ```
   Omit `[sources].enabled` entirely when all sources are wanted. Never edit
   `SOURCES` in `index.py` — that fights git pulls.
2. Optional, if claude is a source: `inspect_sessions.py` to confirm the JSONL
   shape matches `sources/claude.py`.
3. Dry-run, per source is clearest: `index.py --dry-run --source <name>`.
   Zero lines = filters rejected everything — investigate before proceeding.
4. Existing `~/.claude/history-rag.db` with rows ⇒ incremental is safe and
   adding sources is additive (no rebuild). `--rebuild` only for a model/dim or
   column change — **ASK Q9** first.
5. Build: `index.py` (Ollama must be up). Each source prints a stats line
   (`git: 469 chunks, 12 embedded, 0 skipped, 0.8s`); a failing source is
   logged and skipped, so scan stderr rather than assuming silence = success.

**STOP if Safari was chosen and the log shows `browser: skipping safari`
(Operation not permitted):** the human must grant Full Disk Access to their
terminal app (interactive runs) — System Settings → Privacy & Security → Full
Disk Access. Other browsers index fine without it; the next run picks Safari up.

Verify (raw DB): per-source counts are non-zero for every chosen source:
```bash
~/.claude/rag-venv/bin/python -c "
import sqlite3, os
db = sqlite3.connect(os.path.expanduser('~/.claude/history-rag.db'))
for r in db.execute('SELECT source, COUNT(*) FROM chunks GROUP BY source'): print(r)"
```

## Phase 4 — Verify search works (in-process)
Do NOT verify via the MCP tool yet (a just-registered server isn't callable in
the session that registered it; a running one is stale). Exercise the module:
```bash
~/.claude/rag-venv/bin/python -c "import server; print(server.search_history('test', k=2))"
```
Expect JSON `{query, count, results[...]}` with `source`/`location`/`distance`
per hit. Also spot-check the filters: `source=`, `location=` (prefix, e.g.
`'chrome:'`), `since=`/`until=` (ISO dates). Empty results on a non-empty DB or
a `no such column` error ⇒ DB and code disagree — rebuild (Q9) and retry.

## Phase 5 — Register the MCP server
**ASK Q10**, then from the repo root:
```bash
claude mcp add history -s user -- ~/.claude/rag-venv/bin/python "$(pwd)/server.py"
```
(`-s project` for project scope; non-default model ⇒ add `--env CLAUDE_RAG_MODEL=…
--env CLAUDE_RAG_DIM=…`. Wrong existing registration ⇒ `claude mcp remove history`
first.) Confirm with `claude mcp list`.

**STOP — tell the human:** the tool isn't callable until they reconnect
(`/mcp` → `history`) or restart the session. Same applies after any later
`server.py` change — the running process doesn't pick up edits.

## Phase 6 — Keep it fresh
**ASK Q11.** If yes:
- **macOS — launchd.** Fill the plist and load it:
  ```bash
  PY=~/.claude/rag-venv/bin/python
  sed -e "s#__PYTHON__#$PY#" -e "s#__INDEX__#$(pwd)/index.py#" \
    com.user.history-index.plist > ~/Library/LaunchAgents/com.user.history-index.plist
  launchctl load ~/Library/LaunchAgents/com.user.history-index.plist
  ```
  No env plumbing needed — scheduled runs read `~/.claude/history-rag.toml`
  like every other entry point. Cadence: `StartInterval` seconds.
  Verify: `launchctl list | grep history-index`, then the per-source stats
  lines in `/tmp/history-index.log`.
- **Linux — cron** (absolute paths; config comes from the same file):
  ```cron
  */30 * * * * /ABS/rag-venv/bin/python /ABS/repo/index.py >> $HOME/.claude/rag-index.log 2>&1
  ```

**STOP — two macOS permission gotchas on the first scheduled run:**
- Safari under launchd needs Full Disk Access on the *resolved* python binary
  (`readlink -f ~/.claude/rag-venv/bin/python`), not the terminal. Re-grant
  after interpreter upgrades change that path; the tell is
  `browser: skipping safari` returning to the log.
- A vault or histfile under `~/Documents`/`~/Desktop` triggers a one-time
  "python would like to access…" dialog the human must approve. The tell: that
  source's stats line showing minutes, not milliseconds.

## Phase 7 — Optional: app-usage tracker (macOS only)
Persistent daemon logging the frontmost app — privacy-relevant, so **ASK Q12**
and install only on a clear yes. Independent of the rest (`appusage` source
no-ops without it).
```bash
PY=~/.claude/rag-venv/bin/python
sed -e "s#__PYTHON__#$PY#" -e "s#__DAEMON__#$(pwd)/appusage/daemon.py#" \
  appusage/com.user.appusage.plist > ~/Library/LaunchAgents/com.user.appusage.plist
launchctl load ~/Library/LaunchAgents/com.user.appusage.plist
```
Verify: `launchctl list | grep com.user.appusage` shows a PID;
`/tmp/appusage-daemon.log` clean. Data → `~/.claude/appusage.db`;
`appusage/report.py` shows totals; days flow into the index on the next run.

## Guardrails — do not violate
- **Never disable secret redaction** — the shared regex in `sources/common.py`
  plus shell's extra `-p` pattern keep credentials out of the index.
- **Never commit `~/.claude/history-rag.db`** or the venv (both gitignored;
  nothing machine-specific belongs in the repo).
- **Model/dim must match between index and queries** (`config.py`). Switching
  models ⇒ `--rebuild` AND re-register the server with matching env vars.
- **Rebuild is for model/dim/column changes only.** Adding a source is additive
  — plain incremental runs handle it.
- **`--prune` requires `--source`, and is only routine-safe for git/obsidian**
  (their backing stores are durable). The index is an *archive* for
  claude/shell/browser — their raw data expires, and pruning them deletes
  history the index has outlived.
- **New sources:** a module in `sources/` exposing `iter_chunks()` yielding
  `(id, text, {"source","timestamp","location","meta"})` with a run-stable id,
  added to `SOURCES` in `index.py`. Don't bolt onto existing modules.
- **Don't read the user's raw history/session files** to answer questions
  unless asked — that's what `search_history` is for.
