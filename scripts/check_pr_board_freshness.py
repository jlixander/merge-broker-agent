#!/usr/bin/env python3
"""merge-broker board-freshness checker -- read-only, fail-closed, platform-agnostic.

A green CI board proves a PR passed against the BASE it was tested on -- not
the base it is about to merge into. If the target branch has since gained
files that could change the verdict (a workflow file, a file under a
configured "verdict path"), the green board no longer means what it appears
to mean. This script detects that gap and exits nonzero so a caller (a human,
or the merge-broker agent) can require a merge-forward and a fresh run before
trusting the board.

This does NOT replace branch-protection's "require branches up to date before
merging" -- use that where your plan/host supports it. This is the fallback
for repos where that setting is unavailable (e.g. GitHub Free on a private
repo), and it depends on being run; it is not a gate GitHub itself enforces.

Requirements: Python 3.9+, GitHub CLI (`gh`) authenticated for the target
repo. Pure `gh api` -- no local git clone required, run from anywhere.

Usage:
  python check_pr_board_freshness.py --repo owner/name --pr 123
      [--config merge-broker.config.json] [--max-age-minutes 30]

Config (JSON; all keys optional):
  default_branch     target branch (default "main")
  verdict_paths       [".github/workflows/**", "lambdas/**", ...]  glob-style
                       prefixes; a file under any of these landing in the gap
                       is "verdict-changing". Defaults to ["**"] (any file
                       counts) if omitted -- the conservative default.

Exit codes:
  0  FRESH        -- board's base is unchanged, or only inert files landed
                      since (no verdict_paths match) -- READY is trustworthy
  1  STALE        -- verdict-changing files landed in the base since the
                      board's CI run concluded -- do not trust READY as-is
  2  UNKNOWN       -- could not determine (missing gh/git, no CI run found,
                      network error) -- fail closed, treat like STALE for
                      merge purposes but say so explicitly
"""

import argparse
import fnmatch
import json
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def gh_json(args):
    r = run(["gh"] + args)
    if r.returncode != 0:
        return None, r.stderr.strip()
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError as e:
        return None, f"bad JSON from gh: {e}"


def parse_iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def matches_any(path, patterns):
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--config")
    ap.add_argument("--max-age-minutes", type=int, default=0,
                     help="skip the check entirely if the board's CI concluded "
                          "less than this many minutes ago (0 = always check)")
    args = ap.parse_args()

    cfg = {}
    if args.config:
        try:
            with open(args.config) as f:
                cfg = json.load(f)
        except OSError as e:
            print(f"UNKNOWN: cannot read config {args.config}: {e}")
            return 2

    default_branch = cfg.get("default_branch", "main")
    verdict_paths = cfg.get("verdict_paths", ["**"])

    pr, err = gh_json(["pr", "view", str(args.pr), "-R", args.repo,
                        "--json", "headRefOid,baseRefName,statusCheckRollup"])
    if err or pr is None:
        print(f"UNKNOWN: could not read PR #{args.pr}: {err}")
        return 2

    head = pr["headRefOid"]
    base_branch = pr.get("baseRefName") or default_branch

    runs, err = gh_json(["run", "list", "-R", args.repo, "--commit", head,
                          "--limit", "20",
                          "--json", "databaseId,status,conclusion,updatedAt,workflowName"])
    if err or not runs:
        print(f"UNKNOWN: no CI runs found for head {head[:12]}: {err or 'empty'}")
        return 2

    concluded = [r for r in runs if r.get("status") == "completed" and r.get("updatedAt")]
    if not concluded:
        print(f"UNKNOWN: no concluded CI run at head {head[:12]} yet")
        return 2

    board_time = max(parse_iso(r["updatedAt"]) for r in concluded)
    now = datetime.now(timezone.utc)
    age_minutes = (now - board_time).total_seconds() / 60

    if args.max_age_minutes and age_minutes < args.max_age_minutes:
        print(f"FRESH: board is {age_minutes:.0f}m old, under the {args.max_age_minutes}m "
              f"threshold -- not checked")
        return 0

    # Commits landed on base_branch's tip strictly after the board's own CI concluded,
    # via the GitHub API only -- no local clone assumed or required.
    #
    # Deliberately a single URL-encoded query string, not `-f key=value` pairs: on
    # Windows/Git-Bash, `gh api -f since=2026-09-10T22:00:00Z` silently 404s because the
    # colons in the ISO timestamp get mangled on the way to the argument -- the same MSYS
    # path/arg-rewriting class of bug this codebase's own doctrine already warns about
    # for `aws` and `git show <ref>:<path>`. A raw query string sidesteps it entirely.
    since_iso = board_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    commits_url = (f"repos/{args.repo}/commits?sha={urllib.parse.quote(base_branch)}"
                   f"&since={urllib.parse.quote(since_iso)}&per_page=100")
    commits, err = gh_json(["api", commits_url, "--paginate"])
    if err or commits is None:
        print(f"UNKNOWN: listing commits on {base_branch} since board concluded failed: {err}")
        return 2

    changed = set()
    for c in commits:
        sha = c.get("sha")
        if not sha:
            continue
        detail, err = gh_json(["api", f"repos/{args.repo}/commits/{sha}?per_page=300"])
        if err or detail is None:
            print(f"UNKNOWN: could not read commit {sha[:12]} on {base_branch}: {err}")
            return 2
        if "files" not in detail:
            print(f"UNKNOWN: commit {sha[:12]} on {base_branch} has no 'files' field in the "
                  f"API response (large-commit truncation or API change) -- cannot determine "
                  f"what changed, refusing to guess")
            return 2
        for f in detail["files"] or []:
            fn = f.get("filename")
            if fn:
                changed.add(fn)
            # a rename carries the old path too -- either side landing counts
            prev = f.get("previous_filename")
            if prev:
                changed.add(prev)

    changed = sorted(changed)
    if not changed:
        print(f"FRESH: origin/{base_branch} unchanged since board concluded "
              f"({age_minutes:.0f}m ago)")
        return 0

    verdict_changing = [f for f in changed if matches_any(f, verdict_paths)]
    if not verdict_changing:
        print(f"FRESH: {len(changed)} file(s) landed on origin/{base_branch} since the "
              f"board concluded ({age_minutes:.0f}m ago), none under verdict_paths "
              f"{verdict_paths} -- gap is inert")
        return 0

    print(f"STALE: board for head {head[:12]} concluded {age_minutes:.0f}m ago; "
          f"origin/{base_branch} has since gained {len(verdict_changing)} verdict-changing "
          f"file(s) (of {len(changed)} total): {', '.join(verdict_changing[:10])}"
          + (" ..." if len(verdict_changing) > 10 else ""))
    return 1


if __name__ == "__main__":
    sys.exit(main())
