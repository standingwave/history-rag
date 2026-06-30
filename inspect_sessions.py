#!/usr/bin/env python3
"""Run locally first. Dumps the shape of your ~/.claude session JSONL so the
indexer parser matches reality. Prints keys + a sample, redacts long text."""
import json, glob, os, collections

ROOT = os.path.expanduser("~/.claude/projects")
files = glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True)
print(f"projects root: {ROOT}")
print(f"session files found: {len(files)}\n")

key_counts = collections.Counter()
type_counts = collections.Counter()
sample_shown = {}

for fp in files[:50]:
    with open(fp) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            key_counts.update(obj.keys())
            t = obj.get("type", "<no-type>")
            type_counts[t] += 1
            if t not in sample_shown:
                redacted = json.dumps(obj)[:600]
                sample_shown[t] = redacted

print("== top-level keys (freq) ==")
for k, c in key_counts.most_common():
    print(f"  {k}: {c}")

print("\n== event types (freq) ==")
for k, c in type_counts.most_common():
    print(f"  {k}: {c}")

print("\n== one sample per type (truncated 600 chars) ==")
for t, s in sample_shown.items():
    print(f"\n--- type={t} ---\n{s}")
