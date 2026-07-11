"""Rotation helper: set one env var on the deployed function.

Usage:
  set-env.py CLAUDE_RAG_URL_SECRET --random   # fresh 128-bit secret; prints
                                              # the new connector URL
  pbpaste | set-env.py MXBAI_API_KEY -        # value from stdin, so secrets
                                              # never touch shell history
  set-env.py NAME VALUE                       # explicit (lands in history —
                                              # prefer stdin for secrets)

Waits for the config change to finish; env updates recycle all warm
containers, so the old value stops being served immediately.
"""
import argparse, secrets, sys
import boto3

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("name")
    ap.add_argument("value", nargs="?",
                    help="literal value, or '-' for stdin")
    ap.add_argument("--random", action="store_true",
                    help="generate a 128-bit hex value")
    ap.add_argument("--function", default="history-rag")
    ap.add_argument("--region", default="us-west-2")
    args = ap.parse_args()

    if args.random:
        value = secrets.token_hex(16)
    elif args.value == "-":
        value = sys.stdin.read().strip()
    elif args.value:
        value = args.value
    else:
        sys.exit("need a value, '-' for stdin, or --random")
    if not value:
        sys.exit("empty value")

    lam = boto3.client("lambda", region_name=args.region)
    env = lam.get_function_configuration(
        FunctionName=args.function)["Environment"]["Variables"]
    env[args.name] = value
    lam.update_function_configuration(FunctionName=args.function,
                                      Environment={"Variables": env})
    lam.get_waiter("function_updated_v2").wait(FunctionName=args.function)
    print(f"{args.name} updated on {args.function}")
    if args.name == "CLAUDE_RAG_URL_SECRET":
        host = lam.get_function_url_config(
            FunctionName=args.function)["FunctionUrl"].rstrip("/")
        print(f"new connector URL: {host}/{value}/mcp")
        print("update the claude.ai connector and your password manager; "
              "the old URL is already dead.")

if __name__ == "__main__":
    main()
