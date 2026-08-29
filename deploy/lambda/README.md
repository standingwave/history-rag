# Lambda deployment

Runs the history MCP server as a read-only replica on AWS Lambda, reachable
from the Claude phone/web app as a custom connector. Design and rationale:
`wip/SPEC-lambda-remote.md`. The Mac remains the only writer; Lambda pulls
the index from S3 and embeds queries via the hosted API matching the indexed
model — Nomic for nomic-embed-text, Mixedbread for mxbai-embed-large.
Verify parity first: `tools/eval-embed-parity.py`.

Nothing here affects the local install — the stdio server, launchd job, and
config defaults are untouched.

## One-time AWS setup

Needs an AWS account and, for this provisioning section only, the `aws`
CLI. Nothing after setup depends on it: the sync, `set-env.py`, and the
day-to-day commands below go through boto3 in the rag venv, so the CLI
can come and go. Pick values once:

```sh
REGION=us-west-2
BUCKET=<globally-unique-bucket-name>
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
```

Bucket and first upload (the sync tool automates later uploads):

```sh
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration file://s3-lifecycle.json
aws s3 cp ~/.claude/history-rag.db "s3://$BUCKET/history-rag.db"
```

The lifecycle rule aborts incomplete multipart uploads a day after they
start. The index is large enough to upload in parts, so a sync that loses
the network mid-transfer leaves those parts behind — and the client's own
cleanup can't help, because aborting is itself a network call. Without the
rule the abandoned parts are billed as storage forever. It only touches
incomplete uploads; it never expires an object.

The Mac pushes with its own credentials, scoped to that one object by
`iam-mac-sync.json` — write, read (the sync HEADs the object, and with
`[sync] differential` has S3 copy the unchanged parts of it), and abort for
an interrupted multipart upload:

```sh
sed "s/__BUCKET__/$BUCKET/" iam-mac-sync.json | \
  aws iam put-user-policy --user-name <sync-user> \
    --policy-name history-rag-sync --policy-document file:///dev/stdin
```

Execution role (S3 read + CloudWatch logs):

```sh
aws iam create-role --role-name history-rag-lambda \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
sed "s/__BUCKET__/$BUCKET/" iam-lambda-role.json | \
  aws iam put-role-policy --role-name history-rag-lambda \
    --policy-name s3-read-index --policy-document file:///dev/stdin
aws iam attach-role-policy --role-name history-rag-lambda \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

Function, URL, and guardrails (1024 MB memory buys the CPU for KNN; reserved
concurrency 2 caps cost/abuse if the URL ever leaks):

```sh
./build.sh
aws lambda create-function --function-name history-rag \
  --runtime python3.12 --architectures x86_64 --handler app.handler \
  --zip-file fileb://history-rag-lambda.zip \
  --role "arn:aws:iam::$ACCOUNT:role/history-rag-lambda" \
  --memory-size 1024 --timeout 60 --ephemeral-storage Size=1024 \
  --region "$REGION" --environment "Variables={
    CLAUDE_RAG_SYNC_BUCKET=$BUCKET,
    CLAUDE_RAG_URL_SECRET=$SECRET,
    CLAUDE_RAG_MODEL=<indexed model>,
    CLAUDE_RAG_DIM=<indexed dim>,
    CLAUDE_RAG_EMBED_BACKEND=<hf-inference|mixedbread-api|nomic-api>,
    HF_TOKEN=<or MXBAI_API_KEY / NOMIC_API_KEY>}"
aws lambda put-function-concurrency --function-name history-rag \
  --reserved-concurrent-executions 2 --region "$REGION"
aws lambda create-function-url-config --function-name history-rag \
  --auth-type NONE --region "$REGION"
aws lambda add-permission --function-name history-rag \
  --action lambda:InvokeFunctionUrl --principal '*' \
  --function-url-auth-type NONE --statement-id public-url --region "$REGION"
# Since Oct 2025 public URLs ALSO need InvokeFunction, scoped to URL calls
# only via --invoked-via-function-url; without it every request 403s.
aws lambda add-permission --function-name history-rag \
  --action lambda:InvokeFunction --principal '*' \
  --invoked-via-function-url --statement-id public-url-invoke \
  --region "$REGION"
aws logs put-retention-policy --log-group-name /aws/lambda/history-rag \
  --retention-in-days 30 --region "$REGION" 2>/dev/null || true
```

The MCP endpoint is `https://<function-url-host>/$SECRET/mcp` — treat the
whole URL as a credential.

Also set `TZ` (e.g. `America/Los_Angeles`) in the function env: bare-date
window bounds are interpreted as the function's local days — without it,
they mean UTC. (The browser UI renders times client-side, so display is
unaffected either way.)

## Smoke test

```sh
URL="https://<function-url-host>/$SECRET/mcp"
curl -s "$URL" \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect the four tools. Then the real thing, from any machine:

```sh
claude mcp add -t http history-remote "$URL"
```

and compare a few `search_history` answers against the local stdio server.
Interactive poking: `npx @modelcontextprotocol/inspector`.

## Connect the Claude app

claude.ai → Settings → Connectors → Add custom connector → paste the URL
(no OAuth). Connectors are account-level: the phone app, web, and desktop
all pick it up.

## Direct access

Two thin clients ride the same endpoint (design: `wip/SPEC-direct-access.md`):

- **CLI** — `tools/hist.py`, stdlib only, copyable as a single file.
  Endpoint from `$HISTORY_RAG_URL`, else the URL field of the
  `history-rag remote` LastPass entry (`$HISTORY_RAG_LPASS_ENTRY`
  overrides the name). `hist search "that proxy bug" -k 5`, `hist stats`,
  `hist window --since 2026-07-01 --group-by day`, `hist expand <id>`;
  `--json` for piping.
- **Browser UI** — `ui.html` + `ui.js`, a static client served by the
  Lambda that calls the MCP endpoint from the page
  (`wip/SPEC-ui-rebuild.md`). Three tabs (Search / Ask / Browse), phone-
  sized, dark. Set it up once per device: open
  `https://<function-url-host>/login?token=$SECRET` — that mints a
  year-long auth cookie and lands on `/`, which you bookmark on the home
  screen. Page URLs never carry the secret and queries never ride a URL;
  shareable links are explicit (`copy link`, hash fragment — invisible to
  server logs). `https://<function-url-host>/$SECRET/` serves the same UI
  without the cookie; old `/$SECRET/search?...` bookmarks redirect into
  the client and never execute anything by navigation.

## Ask mode

The UI's Ask tab runs a model over the four tools in-process and renders
a cited answer (`wip/SPEC-ask-mode.md`). Ask is `POST /ask` only — a GET
can never trigger a paid call — and `hist ask` posts the same endpoint.
Configure named presets as JSON in the function env (there's no TOML on
Lambda), plus each preset's key. Presets may carry two optional display
strings the picker shows: `latency` (e.g. `"~10–20s"`) and `est_cost`
(e.g. `"$0.004"`) — nothing else about a preset ever crosses the wire
(`GET /config` returns name + those two fields only).

```sh
python3 set-env.py CLAUDE_RAG_ASK_MODELS - <<'EOF'
[{"name": "haiku", "backend": "anthropic",
  "model": "claude-haiku-4-5", "key_env": "ANTHROPIC_API_KEY",
  "latency": "~10-20s", "est_cost": "$0.004"}]
EOF
pbpaste | python3 set-env.py ANTHROPIC_API_KEY -
```

The picker offers only presets whose `key_env` is set; the client selects
by name — endpoints and models never come from the request. Bump the
function timeout for the agent loop:

```sh
~/.claude/rag-venv/bin/python -c "import boto3; boto3.client('lambda', region_name='us-west-2') \
  .update_function_configuration(FunctionName='history-rag', Timeout=120)"
```

## Deploys

Pushes to main touching the shipped files (`app.py`, `ui.html`, `ui.js`,
`requirements.txt`, `build.sh`, `server.py`, `config.py`, or the workflow
itself) deploy
automatically via `.github/workflows/deploy-lambda.yml`: tests → build →
OIDC role assumption → code update → a secret-less smoke invoke (the gate
must 404 a path outside the secret) → a `deployed-sha` tag on the
function. Manual runs are `gh workflow run deploy-lambda`. "What's live?":

```sh
~/.claude/rag-venv/bin/python -c "import boto3; c = boto3.client('lambda', region_name='us-west-2'); \
  print(c.list_tags(Resource=c.get_function(FunctionName='history-rag')['Configuration']['FunctionArn'])['Tags'])"
```

Fallback (first deploy, CI outage, offline work):

```sh
./build.sh && ~/.claude/rag-venv/bin/python -c "import boto3; boto3.client('lambda', region_name='us-west-2') \
  .update_function_code(FunctionName='history-rag', ZipFile=open('history-rag-lambda.zip', 'rb').read())"
```

### One-time CI setup

GitHub-OIDC federation — no AWS keys stored in the repo. Trust is scoped
to this repo's main branch, because `UpdateFunctionCode` is effectively
read access to the index and the embed key (deployed code sees both):

```sh
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1  # skip if the account has one
cat > trust.json <<EOF
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
  "Principal": {"Federated": "arn:aws:iam::$ACCOUNT:oidc-provider/token.actions.githubusercontent.com"},
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {"StringEquals": {
    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
    "token.actions.githubusercontent.com:sub": "repo:standingwave/history-rag:ref:refs/heads/main"}}}]}
EOF
aws iam create-role --role-name history-rag-deploy \
  --assume-role-policy-document file://trust.json
cat > deploy-policy.json <<EOF
{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
  "Action": ["lambda:UpdateFunctionCode", "lambda:GetFunction",
             "lambda:InvokeFunction", "lambda:TagResource", "lambda:ListTags"],
  "Resource": "arn:aws:lambda:$REGION:$ACCOUNT:function:history-rag"}]}
EOF
aws iam put-role-policy --role-name history-rag-deploy \
  --policy-name deploy-function-code --policy-document file://deploy-policy.json
gh secret set AWS_DEPLOY_ROLE_ARN   # the role's ARN, via stdin
gh variable set AWS_REGION --body "$REGION"
gh variable set LAMBDA_FUNCTION --body history-rag
```

## Secrets

Two secrets live in the function env: `CLAUDE_RAG_URL_SECRET` (the whole
endpoint URL is the credential) and the embedding API key. Keep the full
connector URL and the API key in a password manager; nothing secret belongs
in the repo, the TOML, or shell history. `set-env.py` rotates either one —
its stdin mode exists so keys never hit shell history or session
transcripts.

```sh
python3 set-env.py CLAUDE_RAG_URL_SECRET --random   # prints the new URL
pbpaste | python3 set-env.py HF_TOKEN -             # key via clipboard
```

Rotate on exposure (URL or key visible in a transcript, log, or screen
share) and otherwise on whatever calendar cadence you rotate the AWS access
key itself. Env updates recycle warm containers, so old values die
immediately; after a URL rotation, update the claude.ai connector and
re-run `/login?token=<new>` on each device — the auth cookie holds the
secret, so rotation invalidates it everywhere at once.

## Runbook
- **Staleness:** Lambda re-checks the S3 ETag at most every 5 min
  (`CLAUDE_RAG_DB_FRESHNESS`); end-to-end lag ≈ index cadence + 5 min.
- **Teardown:** delete the function, its URL config, the role, and the
  bucket. CloudWatch logs age out at 30 days.
- **Cost:** perpetual free tier covers personal query volume; S3 ~1¢/mo.
