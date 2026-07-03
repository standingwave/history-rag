# Testing plan

The minimal test set that buys concrete value, updated after the week-one
build-out (six sources, time windows, disclosure tools, config file). Ordered
so each step pays for itself; stop anywhere and still be better off.

## Bugs found in review that tests must pin (fix alongside their test)
1. `index.py --rebuild --source X` drops BOTH tables then reindexes only X —
   silently destroys the other sources, including archive rows whose backing
   data no longer exists. Guard: forbid the combination.
2. `appusage/daemon.py` sleep-gap overcount: after wake, a same-app tick
   extends the open segment's `end_ts` across the whole sleep. Guard: close
   the segment when `now - end_ts` exceeds a few INTERVALs.

## Tier 0 — enabling refactor (~30 min, no behavior change)
- `server._db`/`_embed` and `index.main` read `config.DB_PATH` etc. by
  attribute (`import config`), not frozen `from config import` values, so
  `CLAUDE_RAG_CONFIG` + env re-point everything without reload gymnastics.
- conftest fixture: tmp dir with a scratch TOML (`CLAUDE_RAG_CONFIG`), temp
  `CLAUDE_RAG_DB`, and a deterministic hash→vector fake embedder patched over
  `index.embed_batch` / `server._embed` (no Ollama in tests).
- Reset hooks for the two hidden globals: `browser._keep_table = None`
  between tests; monkeypatch `claude.ROOT` at a fixture session dir.

## Tier 1 — pure unit (most value per line; no DB, no network)
- **Redaction** (`common.SECRET_RE` + shell's `_FLAG_SECRET_RE`): table of
  must-drop (passwords, tokens, AKIA keys, `user:pass@host`, `-pSecret`) and
  must-keep (`/my-project-x` paths, "Add token refresh" prose). Highest
  stakes in the repo — a false negative embeds a credential.
- **Time bounds** (`server._bound_to_utc`, `_window_where`): date-only
  since/until as local days, a winter date (DST offset differs), naive vs
  'Z' vs offset datetimes, include_undated SQL shape. This logic silently
  misattributed a whole evening once (the Castle bug); pin it.
- **Parsers**: zsh-extended incl. multiline continuation; bash `#epoch`;
  `claude._text_from_content` (drops tool_result/tool_use/thinking, keeps
  text) + junk-line filters; browser `_clean_url` (scheme/localhost/query
  strip, keep_params incl. empty-list disable, sorted param encoding) and
  `_search_text`; obsidian `_strip_frontmatter` + `_sections` (preamble,
  duplicate headings, ####-stays-inside, whole-note fallback).
- **Dedup/id logic**: git message-collapse + rebase-stable ids + stash
  exclusion (monkeypatch `git._git` with fabricated log output); shell
  atuin-supersedes-live-histfile guard (fabricated readers).
- **Aggregation**: `store.daily_durations` midnight attribution,
  `fmt_duration`; appusage `MIN_SECONDS` floor.
- **Config**: env > file > default precedence, `get_paths` string-vs-list,
  `[sources].enabled` filter + unknown-name exit, malformed-TOML exit,
  unknown-key warning.
- **`server._loc_prefix`** collapsing (repo@, folder/).

## Tier 2 — integration (real sqlite-vec + fake embedder, temp DB)
Real sqlite-vec matters: a mock would not have caught the vec0 update bug.
Each driver case below reproduces something that actually happened this week:
- incremental skip / text-change re-embed / timestamp-only metadata refresh
  (no re-embed) — the UTC migration path;
- per-source isolation: a mid-run exploding source logs, drops its partial
  batch, and does not block later sources;
- prune guards: requires `--source`; a failed or empty source never prunes;
  stale ids (and their vec rows) actually deleted;
- `--rebuild --source` forbidden (bug #1's test);
- server envelopes: search result shape incl. `id`; exact-vs-pool branch
  (force it with `EXACT_WINDOW_MAX` monkeypatched small); `exact`/`note`
  fields; `list_window` bound-required error, `total`, paging; `expand`
  per-source context shapes using the *index-fallback* paths, plus the two
  cheap live paths — claude via a fixture session file under a monkeypatched
  ROOT, git via a throwaway `git init` repo.

## Explicit non-goals
Daemon sensor subprocesses (`lsappinfo`/`ioreg` — test their regexes on
captured output only), Ollama integration, the MCP transport layer, launchd,
and expand's live paths that touch real machine stores (browser DBs, atuin,
the real vault). The dev smoke script covers those against the real machine.

## Dev-loop tools (`tools/`)
The manual steps repeated constantly during this week's development, scripted:
- **`tools/smoke.py`** — after any server/source edit: exercises every tool
  path in-process against the real index (stats+locations, list→expand per
  source, pool + exact search, paging, error paths). Exits non-zero on
  failure, and warns when the *running* MCP server process is older than
  `server.py` — the "edits don't apply until /mcp reconnect" trap.
- **`tools/kick.sh`** — kick the launchd refresh and block until its `done.`
  line, then print the per-source stats block (replaces the
  kickstart/sleep/tail dance).
- Scratch-index pattern (used by tests too): point `CLAUDE_RAG_CONFIG` and
  `CLAUDE_RAG_DB` at a tmp dir — never the real `~/.claude`. For one source:
  `index.py --dry-run --source <name>`.

## Order of work
1. Tier 0 refactor + fix the two pinned bugs with their tests.
2. Tier 1 redaction + time bounds + parsers (the incident-prone trio).
3. Tier 2 driver behaviors, then server envelopes.
4. Remaining Tier 1; CI (GitHub Actions, Tier 1+2, mac-only bits skipped)
   once the repo has outside users.
