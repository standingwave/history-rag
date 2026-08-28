# Convex spike

Throwaway implementation of `wip/SPEC-convex-spike.md`: the Today
dashboard with task writes, on a Convex replica of three sources
(`tasks`, `obsidian`, `calendar`). Branch `convex-spike`; only the verdict
goes to main. Nothing here touches the Lambda, S3, or the local index.

```
convex/          backend: schema, RAG instance, sync (internal), search, today, auth
src/             React + Vite client: sign-in, widget grid, tasks sheet, search sheet
tools/sync-convex.py      (repo root) push changed chunks + vectors from SQLite
tools/convex-applier.py   (repo root) drain phone toggles into the vault
```

## One-time setup

1. **Deps.** `cd deploy/convex && npm install`. Then, for the Python side,
   `uv pip install --python ~/.claude/rag-venv/bin/python convex`.

2. **Project.** `npx convex dev` — creates the project on first run, writes
   `.env.local` (`CONVEX_DEPLOYMENT`, `VITE_CONVEX_URL`), generates
   `convex/_generated/`, and typechecks the functions. Leave it running
   while developing; it redeploys on save. `npm run typecheck` runs both
   tsconfigs once `_generated/` exists.

3. **Auth.** `npx @convex-dev/auth` sets `JWT_PRIVATE_KEY`, `JWKS`, and
   `SITE_URL` on the deployment. Then restrict the account:
   ```sh
   npx convex env set ALLOWED_EMAIL you@example.com
   ```
   First app launch: "create the account" with that email; every launch
   after: sign in. Any other address is refused server-side.

4. **Query embeddings.** Same key the Lambda uses, and the same query
   prompt as `[core] mxbai_query_prompt` (default empty — don't set it
   unless the Mac has it):
   The key already lives in the Lambda's env, so copy it across without
   it touching a clipboard or terminal:
   ```sh
   npx convex env set MXBAI_API_KEY "$(~/.claude/rag-venv/bin/python -c "import boto3; \
     print(boto3.client('lambda', region_name='us-west-2').get_function_configuration(\
     FunctionName='history-rag')['Environment']['Variables']['MXBAI_API_KEY'])")"
   npx convex env get MXBAI_API_KEY | awk '{print length($0)}'   # 32
   ```

5. **Deploy key for the Mac.** Dashboard → Settings → Deploy keys →
   generate. Keep it in the password manager; export it in the shell that
   runs the tools and paste it into the applier plist:
   ```sh
   export CONVEX_DEPLOY_KEY='prod:…|…'
   ```

6. **Config.** In `~/.claude/history-rag.toml`:
   ```toml
   [convex]
   url = "https://<deployment>.convex.cloud"    # from .env.local
   # sources = ["tasks", "obsidian", "calendar"]  # default
   # batch = 60                                    # chunks per upsert call
   ```

## Run

```sh
# what would move (per source: chunks, upserts, removes)
~/.claude/rag-venv/bin/python tools/sync-convex.py --dry-run

# first push (~17k chunks; note wall time — measurement #5)
~/.claude/rag-venv/bin/python tools/sync-convex.py

# second run must be "unchanged" with zero calls
~/.claude/rag-venv/bin/python tools/sync-convex.py

# the app, reachable from the phone on the LAN
npm run dev

# the write path: leave running in a terminal, or install the plist
~/.claude/rag-venv/bin/python tools/convex-applier.py --kick
```

The refresh chain (`tools/refresh.py`) already runs `sync-convex.py` as a
step after the S3 push and reports it in the stats block; with no
`[convex] url` it's a silent skip.

## Measuring (spec table)

- **#1 search parity**: Search sheet vs `search_history(source=…)` in
  Claude Code, same query, note top-5 overlap.
- **#2 range windows**: the Search sheet's status line prints
  `candidates · dropped by window · months`; `dropped` is the number.
- **#3 latency**: the same line shows round-trip ms; `today` is
  subscribed, so time the first paint after sign-in.
- **#4 write loop**: tap in the Tasks sheet (glyph turns yellow =
  pending), watch the applier log, then the note in Obsidian; with
  `--kick` the yellow clears after the tasks-only push.
- **#5/#6**: `sync-convex.py` prints seconds per source; storage is on the
  dashboard's Usage page.
- **#7**: toggle with the note open in Obsidian mobile; toggle with the
  Mac asleep, wake it, check the applier log for the catch-up.

## Known deviations from the spec

- One RAG entry per chunk for every source (the spec had one entry per
  obsidian *note*). Simpler diffing; the cost is that `chunkContext` is
  meaningless here. Revisit if search quality on notes looks off.
- No `syncRuns`-driven UI; the table exists for the dashboard's data view.
