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

Needs an AWS account and the `aws` CLI configured. Pick values once:

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
aws s3 cp ~/.claude/history-rag.db "s3://$BUCKET/history-rag.db"
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
    CLAUDE_RAG_EMBED_BACKEND=<mixedbread-api|nomic-api>,
    MXBAI_API_KEY=<or NOMIC_API_KEY>}"
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

## Deploy loop

```sh
./build.sh && aws lambda update-function-code --function-name history-rag \
  --zip-file fileb://history-rag-lambda.zip --region "$REGION"
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
pbpaste | python3 set-env.py MXBAI_API_KEY -        # key via clipboard
```

Rotate on exposure (URL or key visible in a transcript, log, or screen
share) and otherwise on whatever calendar cadence you rotate the AWS access
key itself. Env updates recycle warm containers, so old values die
immediately; after a URL rotation, update the claude.ai connector.

## Runbook
- **Staleness:** Lambda re-checks the S3 ETag at most every 5 min
  (`CLAUDE_RAG_DB_FRESHNESS`); end-to-end lag ≈ index cadence + 5 min.
- **Teardown:** delete the function, its URL config, the role, and the
  bucket. CloudWatch logs age out at 30 days.
- **Cost:** perpetual free tier covers personal query volume; S3 ~1¢/mo.
