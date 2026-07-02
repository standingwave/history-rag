# Testing plan

Recommended approach for adding a test suite to this project.

## What to test, by risk
1. **Secret redaction (`sources/shell.py`)** — highest stakes: a false negative
   embeds a password/token and surfaces it back into a session. Table of
   known-bad commands (must be dropped) and known-good ones (must survive).
2. **Re-embed path (`index.py`)** — regression test for the `vec0` update bug:
   index a chunk, change its text, re-index, assert the vector updated and
   `vec_chunks`/`chunks` stay 1:1 with no orphans.
3. **Parsers** — zsh-extended vs bash formats, multiline continuation, and
   `claude.py` content extraction (drops tool_result/thinking, keeps text).
4. **Aggregation** — `appusage` daily rollups, `MIN_SECONDS` filter, today
   included; `store.daily_durations` midnight attribution.

## Structure
pytest, in three tiers:
- **Tier 1 — pure unit** (no deps, runs anywhere): redaction, parsers, filters,
  `fmt_duration`, aggregation. Most of the value.
- **Tier 2 — integration** with real sqlite-vec + a fake embedder (no Ollama, no
  network): `index.py` incremental-skip / re-embed / consistency, and
  `server.py` output shapes. Real sqlite-vec matters — a mock wouldn't have
  caught the update bug.
- **Tier 3 — daemon coalescing** (macOS-only, injected sensors), skippable
  elsewhere.

## Isolation
Point `CLAUDE_RAG_DB` / `CLAUDE_RAG_APPUSAGE_DB` at temp files and use
checked-in fixture snippets under `tests/fixtures/` — never the real `~/.claude`.
Use a deterministic hash→vector fake embedder so plumbing is exercised without a
running Ollama.

## Design change to make first
`config.py` reads env vars at import time and the modules do
`from config import DB_PATH`, freezing paths at load. Do a light refactor so
`main()` and the embed calls take injectable params (`db_path=None`, an `embed=`
function) defaulting to the config value. Low-risk, makes everything trivially
testable.

## Recommendation
Do the light refactor, write Tier 1 plus the reindex regression test now, and
add CI (GitHub Actions running Tier 1+2 on push; daemon tests skip off-mac) once
the project is shared more widely.
