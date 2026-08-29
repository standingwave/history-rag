# Convex app

The one UI (`wip/SPEC-convex-app.md`): the Today dashboard with task
writes over a Convex replica of `tasks`, `obsidian`, `calendar`, plus
Search / Ask / Browse over the whole archive — proxied to the Lambda in
stage 1, native in stage 3. Grew out of `wip/SPEC-convex-spike.md`.

```
convex/          backend: schema, RAG instance, sync (internal), search, today, archive (proxy), auth, crons
src/             React + Vite client: sign-in, widget grid, tasks sheet, Search/Ask/Browse sheets, reading view
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

4. **Query embeddings.** Hugging Face Inference serves the index's model
   (parity with local Ollama: cos 1.000, top-10 overlap 99%). Create a
   fine-grained token with the "Inference Providers" permission at
   https://huggingface.co/settings/tokens, then:
   ```sh
   npx convex env set HF_TOKEN "<token>"
   npx convex env set EMBED_PROVIDER hf
   ```
   Mixedbread's API (`MXBAI_API_KEY`, the default provider) works too but
   goes cold after ~1 min idle and takes 8–40 s to answer; a cron
   (`convex/crons.ts`) embeds every 3 min to keep whichever provider is
   active warm. `MXBAI_QUERY_PROMPT` mirrors `[core] mxbai_query_prompt`
   (default empty — don't set it unless the Mac has it).

5. **Deploy key for the Mac.** Dashboard → Settings → Deploy keys →
   generate a key for the dev deployment and add it to `.env.local`
   (gitignored) as `CONVEX_DEPLOY_KEY=…`. The Convex CLI, both Python
   tools, and the launchd applier all read it from there; an exported
   `CONVEX_DEPLOY_KEY` in the shell takes precedence if set.

6. **Archive proxy (stage 1).** The Lambda's function URL and URL secret,
   so `convex/archive.ts` can reach every source until stage 3:
   ```sh
   npx convex env set LAMBDA_URL https://<id>.lambda-url.us-west-2.on.aws
   npx convex env set LAMBDA_SECRET "$(~/.claude/rag-venv/bin/python -c "import boto3; \
     print(boto3.client('lambda', region_name='us-west-2').get_function_configuration(\
     FunctionName='history-rag')['Environment']['Variables']['CLAUDE_RAG_URL_SECRET'])")"
   ```
   The Lambda must be on a build that has `/api/search|window|expand`
   (deploy/lambda/README.md, "Deploys").

7. **Hosting.** `@convex-dev/static-hosting` serves `dist/` from
   `https://<deployment>.convex.site` — same origin as the auth routes,
   which stay at the root (app-owned mode in `convex/http.ts`).
   ```sh
   npm run smoke     # build + upload to the dev deployment; open the .convex.site URL
   npm run deploy    # build + push backend + upload (prod when one exists)
   ```
   `npm run dev` stays the dev loop (HMR); the hosted build is what the
   phone bookmarks.

8. **Applier under launchd** (so writes don't need a terminal):
   ```sh
   sed -e "s#__REPO__#$(git rev-parse --show-toplevel)#g" -e "s#__HOME__#$HOME#g" \
     com.user.convex-applier.plist > ~/Library/LaunchAgents/com.user.convex-applier.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.convex-applier.plist
   tail -f ~/.claude/convex-applier.log
   ```

9. **Config.** In `~/.claude/history-rag.toml`:
   ```toml
   [convex]
   url = "https://<deployment>.convex.cloud"    # from .env.local
   # sources = ["tasks", "obsidian", "calendar"]  # default
   # batch = 60                                    # chunks per upsert call
   ```

## Run

From the **repo root** (the Python tools live in `tools/`, not here):

```sh
cd /path/to/history-rag

# what would move (per source: chunks, upserts, removes)
~/.claude/rag-venv/bin/python tools/sync-convex.py --dry-run

# first push (~17k chunks; note wall time — measurement #5)
~/.claude/rag-venv/bin/python tools/sync-convex.py

# second run must be "unchanged" with zero calls
~/.claude/rag-venv/bin/python tools/sync-convex.py

# the app during development (LAN); the phone uses the hosted build (step 7)
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
