"""Git source: your own commits across local repos.

One chunk per commit — subject + body, no diffs — filtered to commits you
authored. Repos come from CLAUDE_RAG_GIT_ROOTS (colon-separated); each entry
is either a repo itself or a directory scanned for repos a few levels deep
(hidden dirs and node_modules are not descended into). Unset -> this source
is a no-op.

"Yours" means the author email from each repo's own `git config user.email`,
so identity follows per-repo config; set CLAUDE_RAG_GIT_AUTHOR to force one
email everywhere. Repos with no resolvable email are skipped with a note.

All refs are read (--all, minus stash), so branch-only work is captured; the
author filter keeps other people's commits out. Rebases and cherry-picks
duplicate a commit under new shas, so identical messages within a repo
collapse to one chunk (run count in meta, latest copy wins timestamp/sha) and
the id hashes repo+message rather than the sha — rebase-stable. Only a
reworded message orphans its old chunk (a --prune --source git case).
"""
import os, subprocess, sys, hashlib
from datetime import datetime, timezone

MAX_CHARS = 2000
SCAN_DEPTH = 3
_SKIP_DIRS = {"node_modules", "__pycache__"}

def _roots():
    import config
    return config.get_paths("git", "roots", "CLAUDE_RAG_GIT_ROOTS")

def _find_repos(root: str):
    """Yield repo paths under root; a dir with .git is a repo and not descended
    into (worktrees carry a .git file, so exists() covers both)."""
    root = os.path.realpath(root)
    if os.path.exists(os.path.join(root, ".git")):
        yield root
        return
    for dirpath, dirnames, _ in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if os.path.exists(os.path.join(dirpath, ".git")):
            yield dirpath
            dirnames.clear()
            continue
        if depth >= SCAN_DEPTH:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in _SKIP_DIRS]

def _git(repo: str, *args) -> str:
    out = subprocess.run(["git", "-C", repo, *args],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200] or f"git {args[0]} failed")
    return out.stdout

def _author(repo: str) -> str:
    import config
    forced = config.get("git", "author", "CLAUDE_RAG_GIT_AUTHOR", "")
    if forced:
        return forced
    try:
        return _git(repo, "config", "user.email").strip()
    except (RuntimeError, subprocess.SubprocessError):
        return ""

def iter_chunks():
    seen_shas = set()
    entries: dict[tuple, list] = {}   # (repo, text) -> [count, date, sha]
    for root in _roots():
        if not os.path.isdir(root):
            continue
        for repo in _find_repos(root):
            email = _author(repo)
            if not email:
                print(f"git: skipping {repo} (no user.email)", file=sys.stderr)
                continue
            try:
                # NUL between commits (-z), \x01 between fields. Stash refs
                # would otherwise leak "index on main: ..." bookkeeping commits.
                log = _git(repo, "log", "--exclude=refs/stash", "--all",
                           "--no-merges", "-z", f"--author={email}",
                           "--pretty=format:%H%x01%aI%x01%s%x01%b")
            except (RuntimeError, subprocess.SubprocessError) as e:
                print(f"git: skipping {repo}: {e}", file=sys.stderr)
                continue
            for entry in log.split("\0"):
                parts = entry.split("\x01")
                if len(parts) != 4:
                    continue
                sha, date, subject, body = parts
                text = (subject + "\n" + body).strip()[:MAX_CHARS]
                if not text or sha in seen_shas:
                    continue
                # %aI carries the author's local offset; the index stores UTC
                # so time-window bounds compare lexicographically.
                try:
                    date = datetime.fromisoformat(date).astimezone(
                        timezone.utc).isoformat()
                except ValueError:
                    pass
                seen_shas.add(sha)
                rec = entries.get((repo, text))
                if rec is None:
                    entries[(repo, text)] = [1, date, sha]
                else:
                    rec[0] += 1
                    if date > rec[1]:
                        rec[1], rec[2] = date, sha
    for (repo, text), (count, date, sha) in entries.items():
        cid = "git:" + hashlib.sha256(f"{repo}\0{text}".encode()).hexdigest()[:26]
        yield cid, text, {
            "source": "git",
            "timestamp": date,
            "location": f"{os.path.basename(repo)}@{sha[:8]}",
            "meta": {"repo": repo, "sha": sha, "count": count},
        }
