# Testing plan

The minimal test set that buys concrete value, updated after the week-one
build-out (six sources, time windows, disclosure tools, config file). Ordered
so each step pays for itself; stop anywhere and still be better off.

## Bugs found in review that tests must pin (fix alongside their test)
1. ✅ FIXED `index.py --rebuild --source X` drops BOTH tables then reindexes
   only X — silently destroys the other sources, including archive rows whose
   backing data no longer exists. Guarded; pinned in `test_pinned_bugs.py`.
2. ✅ FIXED `appusage/daemon.py` sleep-gap overcount: after wake, a same-app
   tick extended the open segment's `end_ts` across the whole sleep. The loop
   body is now `daemon.tick(db, app, now, max_gap)` (testable), closing the
   segment on gaps > 3×INTERVAL; pinned in `test_pinned_bugs.py`.
3. ✅ RESOLVED — `--rebuild` reindexes from *sources* while the index is an
   archive, so model switches must re-embed from stored `chunks` text.
   Shipped as `tools/migrate-model.py` (copy-and-swap, eval-vector reuse on
   (id, text) match) with `tools/eval-model.py` for safe side-by-side
   evaluation first; the archive-survival case is pinned in
   `test_migrate_tool.py`. The stamp check (`config.check_stamp`) enforces
   model/dim consistency at every DB open.

## Tier 0 — enabling refactor ✅ DONE (no behavior change)
- `server._db`/`_embed` and `index` read config by attribute (`import
  config`), so `CLAUDE_RAG_CONFIG` + env re-point everything.
- `tests/conftest.py`: env pinned into a tmp dir before any project import
  (incl. `TZ=America/Los_Angeles` for deterministic time tests), autouse
  reset of `browser._keep_table`, and the hash→vector `fake_embed` fixture
  over `index.embed_batch` / `server._embed` (no Ollama in tests).
- ✅ (was "still to do") `claude.ROOT` monkeypatched at a fixture session dir
  for the live expand path, in `test_server_tools.py`.

## Tier 1 — pure unit (most value per line; no DB, no network) ✅ DONE
Implemented across `test_redaction.py`, `test_time_bounds.py`,
`test_parsers.py`, `test_git_source.py`, `test_shell_dedup.py`,
`test_config.py`, `test_aggregation.py`.
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

## Tier 2 — integration (real sqlite-vec + fake embedder, temp DB) ✅ DONE
Implemented in `tests/test_driver.py` (driver) and `tests/test_server_tools.py`
(tool envelopes + expand shapes, incl. the claude live path via monkeypatched
`claude.ROOT` and the git live path via a throwaway repo).
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

## Post-plan features (each shipped with its tests)
- **Stamp enforcement** (`test_stamp.py`): fresh-index stamping, model/dim
  mismatch refusal by both entry points, legacy-DB tolerance, `--rebuild`
  restamps.
- **Run health** (`test_run_health.py`): ok/partial/aborted rows, stamp
  refusal recorded, retention, `history_stats` health field incl. stall
  note, notify-once-per-incident debounce + config gate.
- **Backup tool** (`test_backup.py`): once-per-day, faithful copy, retention
  prune scoped by stem.
- **Model eval/migration** (`test_eval_tool.py`, `test_migrate_tool.py`):
  candidate path never prod, config restore on failure, archive-only chunks
  survive migration (pinned item 3), eval-vector reuse only on (id, text)
  match.
- **Daily digests** (`test_digest_source.py`): per-stream rollup content and
  day attribution (incl. the 23:30-local boundary), Chromium AND Safari
  visit-reader fixtures (schema sniff + epoch conversions + SQL since
  bound), atuin dedup in `iter_dated_runs`, backfill/resume/recompute window
  selection, pipeline determinism (zero re-embeds on unchanged data),
  `--prune --source digest` refusal.
- **group_by** (`test_group_by.py`): local-day bucketing across a UTC
  midnight, domain extraction + location fallback, undated gating, limit
  clamp + truncation flag, dimension validation, filter composition.
- **Snapshot reads** (`test_snapshot_db.py`): `snapshot_db` per journal
  mode — live WAL stores are copied via the backup API, never read raw.
- **Hosted embed backends** (`test_embed_backend.py`): backend switch,
  request shapes, key handling for the nomic/mixedbread APIs.
- **Day shape + bundle IDs** (`test_dayshape.py`): switches, focus blocks,
  breaks, bundle capture/migration/coalescing, the day-shape chunk.
- **Mic + calls** (`test_mic_calls.py`): probe logic, mic ticks, idle-veto
  ordering, call derivation + labeling, the calls sentence.
- **Playback idle** (`test_playback_idle.py`): pmset wake-lock parsing and
  the lazy active decision.
- **Calendar source** (`test_calendar_source.py`): Apple store reader,
  recurrence window, bounded prune, agenda expand.
- **App categories** (`test_app_category.py`): LSApplicationCategoryType
  resolution + app_meta cache + 7-day recheck, the `other` rollup, per-app
  meta / day-shape sentence / report line.
- **hist CLI** (`test_hist_cli.py`): flag→arguments mapping, endpoint
  resolution precedence (env beats lpass), tool-error exit codes, human vs
  `--json` rendering — faked urlopen, no network.
- **/search page** (`test_search_page.py`): secret gate, escaping (the
  index holds attacker-influenceable text), CSP hash pinning, filters →
  kwargs, window mode + paging, health line/banners — app.py imported with
  Lambda deps stubbed.
- **Refresh driver** (`test_refresh.py`): step isolation incl. SystemExit,
  one runs row per tick, prune validation + argv, `synced_at` stamping
  rules, notify debounce, summary line, `health.replica` derivation.

Shared plumbing lives in `tests/helpers.py` (script loading for non-package
tools, the fabricated-source index harness, appusage store builders); the
`no_category_resolution` fixture in conftest keeps appusage tests off the
real mdfind.

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
- **`tools/kick.sh`** — kick the launchd refresh and block until the
  driver's `refresh:` summary line (the last thing a tick prints, after
  backup and sync), then print the run's stats block.
- Scratch-index pattern (used by tests too): point `CLAUDE_RAG_CONFIG` and
  `CLAUDE_RAG_DB` at a tmp dir — never the real `~/.claude`. For one source:
  `index.py --dry-run --source <name>`.

## Order of work
1. ✅ Tier 0 refactor + fix the two pinned bugs with their tests.
2. ✅ Tier 1 redaction + time bounds + parsers (the incident-prone trio).
3. ✅ Tier 2 driver behaviors, then server envelopes.
4. ✅ Remaining Tier 1 (git ids, shell dedup, config precedence, aggregation).
5. ✅ `tools/migrate-model.py` (pinned item 3) and CI (GitHub Actions, pytest
   on push and PR — `f9578d9`).
6. ✅ Post-plan feature tests as they shipped (section above).
   Suite: 98 tests, <1s, no Ollama needed. Reviewed 2026-07-07: meets the
   plan; the only deliberate redundancies kept are stamp-mismatch (refusal
   vs. audit row) and notify (truth table vs. integration), both cheap.
