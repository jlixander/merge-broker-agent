#!/usr/bin/env python3
"""merge-broker pre-merge verifier -- read-only, fail-closed, platform-agnostic.

Runs every deterministic check a merge request needs BEFORE the merge itself:
head pin, base branch, review-evidence gate, job-level CI at the head sha (with
per-workflow owedness), and your deploy queue. Prints a single verdict line
first (READY / REJECTED-<REASON>), then terse evidence lines. Exit 0 iff READY.
NEVER mutates anything and NEVER merges.

Requirements: Python 3.9+, GitHub CLI (`gh`) authenticated for the target repo.
Works from any agent framework that can run a shell command (Claude Code,
Codex CLI, Gemini CLI, Copilot CLI, OpenHands, Aider, custom harnesses).

Usage:
  python merge_broker_check.py --repo owner/name --pr 123 --head <sha>
      [--config merge-broker.config.json] [--allow-pending-displacement]

Config (JSON; all keys optional -- see examples/merge-broker.config.json):
  default_branch      target branch the protocol governs (default "main")
  review_gate         {"mode": "github-approval" | "command" | "off",
                       "command": "..."}  # command: exit 0 iff review evidence
                       exists at exactly $MB_HEAD; gets MB_REPO/MB_PR/MB_HEAD env
  required_workflows  [{"path": ".github/workflows/ci.yml",
                        "when": "always" | "paths"}]
                       # "paths": owed iff the diff hits the workflow's own
                       # on.pull_request.paths, parsed AT THE HEAD SHA
  test_owed           {"workflow_path": ".github/workflows/tests.yml",
                       "job_name_contains": "pytest", "paths": ["src/**"]}
                       # reject CI-UNTESTED if the diff hits paths but the green
                       # run of that workflow executed 0 matching jobs
  deploy              {"model": "actions" | "external" | "none",
                       "workflow": "deploy.yml",      # actions model
                       "status_command": "..."}       # external model, optional:
                       # exit 0 iff the deploy pipeline is idle and healthy

Verdict semantics: reasons starting with QUEUE-BUSY or ending in -PENDING /
-UNKNOWN are retryable (transient state); all others are terminal for this
request. Fail-closed: any unexpected error is REJECTED-CHECK-ERROR, never a pass.

Why these exact checks exist (each one is a real incident class):
- HEAD-MOVED: a head that advanced after review means the recorded evidence
  describes code nobody read.
- Job-level judging: run-level status lies both ways -- a run can report
  "queued" with most of its jobs already finished, and a run can be GREEN at
  the rollup having spawned ZERO jobs (empty matrix). A green rollup with no
  jobs tested nothing.
- "skipped" is "not applicable", never "passed".
- QUEUE-BUSY-PENDING: with a shared concurrency group and cancel-in-progress
  false, GitHub keeps one running + one pending run; a new push CANCELS the
  pending run silently, and a cancelled run is never requeued -- merged,
  CI-green, and never deployed, with nothing red to notice.
- external deploy model: when a provider (Amplify/Vercel/Netlify/Pages) builds
  the default branch on its own, merging IS deploying -- red CI ships anyway,
  so the default branch must be job-level green before you add to it.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

for stream in (sys.stdout, sys.stderr):  # verdicts must survive legacy consoles
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

GOOD_JOB = {"success", "skipped"}  # whitelist: any other conclusion, present or future, is not green


def sh(args, timeout=60, env_extra=None, shell=False):
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env=env, encoding="utf-8", errors="replace", shell=shell)
    return proc


def sh_ok(args, timeout=60):
    proc = sh(args, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}... rc={proc.returncode}: "
                           f"{proc.stderr.strip()[:300]}")
    return proc.stdout


def gh_json(args, timeout=60):
    return json.loads(sh_ok(["gh"] + args, timeout=timeout))


def reject(reason, lines):
    print(f"REJECTED-{reason}")
    for line in lines:
        print(f"  {line}")
    sys.exit(1)


def run_jobs(repo, run_id):
    data = gh_json(["run", "view", str(run_id), "-R", repo, "--json", "status,conclusion,jobs"])
    return data, (data.get("jobs") or [])


def judge_run_by_jobs(repo, run):
    """(verdict, detail). Job-level only -- run-level status/conclusion lie both ways."""
    data, jobs = run_jobs(repo, run["databaseId"])
    bad = [j for j in jobs if (j.get("conclusion") or "") not in GOOD_JOB]
    unfinished = [j for j in jobs if j.get("status") != "completed"]
    skipped = [j.get("name", "?") for j in jobs if j.get("conclusion") == "skipped"]
    detail = (f"run {run['databaseId']} [{run.get('workflowName', '?')}/{run.get('event', '?')}] "
              f"jobs={len(jobs)} bad={len(bad) - len(unfinished)} "
              f"unfinished={len(unfinished)} skipped={len(skipped)}")
    if data.get("status") != "completed" or unfinished:
        return "UNFINISHED", detail
    if not jobs:
        # cancelled-from-pending and empty-matrix greens both executed nothing
        return "EMPTY", detail + " :: zero spawned jobs = tested nothing"
    if bad:
        return "RED", detail + " :: " + "; ".join(f"{j['name']}={j['conclusion']}" for j in bad[:5])
    return "GREEN", detail + (f" :: skipped: {', '.join(skipped[:6])}" if skipped else "")


def pr_changed_files(repo, number):
    """Full changed-file list. gh pr view --json files silently caps at 100."""
    meta = gh_json(["pr", "view", str(number), "-R", repo, "--json", "changedFiles,files"])
    files = [f["path"] for f in meta.get("files") or []]
    if meta.get("changedFiles", 0) > len(files):
        out = sh_ok(["gh", "api", f"repos/{repo}/pulls/{number}/files",
                     "--paginate", "--jq", ".[].filename"], timeout=180)
        files = [line for line in out.splitlines() if line.strip()]
        if meta["changedFiles"] > len(files):
            raise RuntimeError(f"changed-file list incomplete: {len(files)}/{meta['changedFiles']}")
    return files


def workflow_meta(repo):
    """Map workflow file path -> display name (what run listings key on)."""
    data = gh_json(["api", f"repos/{repo}/actions/workflows", "--paginate",
                    "--jq", "{workflows: [.workflows[] | {path, name}]}"])
    return {w["path"]: w["name"] for w in data["workflows"]}


def pull_request_paths(repo, wf_path, ref):
    """Parse on.pull_request.paths from the workflow file AT THE REF -- never a
    stale local mirror. Tolerant line parser (stdlib has no yaml); zero paths
    parsed = fail closed (owedness unknown is not owedness satisfied)."""
    raw = sh_ok(["gh", "api", f"repos/{repo}/contents/{wf_path}?ref={ref}",
                 "-H", "Accept: application/vnd.github.raw"])
    paths, in_pr, in_paths, pr_indent, paths_indent = [], False, False, 0, 0
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if re.match(r"^pull_request\s*:", stripped):
            in_pr, pr_indent = True, indent
            continue
        if in_pr and indent <= pr_indent and not stripped.startswith("-"):
            in_pr = in_paths = False
        if in_pr and re.match(r"^paths\s*:", stripped):
            in_paths, paths_indent = True, indent
            continue
        if in_paths:
            if stripped.startswith("-") and indent > paths_indent:
                item = stripped[1:].strip().strip("'\"")
                if item:
                    paths.append(item)
            elif indent <= paths_indent:
                in_paths = False
    if not paths:
        raise RuntimeError(f"could not parse pull_request.paths from {wf_path}@{str(ref)[:12]}")
    return paths


def diff_hits(files, patterns):
    return [f for f in files if any(fnmatch.fnmatch(f, p) for p in patterns)]


def check_review_gate(cfg, repo, number, head, notes):
    gate = cfg.get("review_gate") or {"mode": "github-approval"}
    mode = gate.get("mode", "github-approval")
    if mode == "off":
        notes.append("review gate: OFF by config")
        return
    if mode == "command":
        cmd = gate.get("command")
        if not cmd:
            reject("CHECK-ERROR", ["review_gate.mode=command but no command configured"])
        proc = sh(cmd, timeout=120, shell=True,
                  env_extra={"MB_REPO": repo, "MB_PR": str(number), "MB_HEAD": head})
        if proc.returncode != 0:
            reject("EVIDENCE-GATE", [f"review-evidence command exited {proc.returncode}: "
                                     f"{(proc.stdout + proc.stderr).strip()[:300]}"])
        notes.append(f"review gate: evidence command passed at {head[:12]}")
        return
    # github-approval (default): the PR must be APPROVED, and no approval may
    # predate the head (a push after approval voids the evidence).
    data = gh_json(["pr", "view", str(number), "-R", repo,
                    "--json", "reviewDecision,latestReviews"])
    decision = data.get("reviewDecision") or ""
    if decision == "CHANGES_REQUESTED":
        reject("CHANGES-REQUESTED", ["reviewDecision=CHANGES_REQUESTED"])
    approvals = [r for r in (data.get("latestReviews") or []) if r.get("state") == "APPROVED"]
    if decision != "APPROVED" and not approvals:
        reject("NO-APPROVAL", [f"reviewDecision={decision or '(none)'} and no APPROVED review "
                               "-- record review evidence, or configure review_gate.mode: "
                               "command|off deliberately"])
    stale = [r for r in approvals if (r.get("commit") or {}).get("oid")
             and r["commit"]["oid"].lower() != head.lower()]
    if approvals and len(stale) == len(approvals):
        reject("STALE-APPROVAL", [f"every APPROVED review is pinned to an older commit than "
                                  f"{head[:12]} -- commits landed after the review"])
    notes.append(f"review gate: APPROVED at {head[:12]}")


def check_ci_at_head(cfg, repo, head, pr_files):
    """Job-level green for every OWED workflow at the head sha, judged on
    pull_request-event runs only (a green manual dispatch at the same sha must
    never mask a red PR run). Any-green across attempts per workflow, so a later
    failed re-run cannot mask an earlier real green."""
    owed = cfg.get("required_workflows")
    if owed is None:
        reject("CHECK-ERROR", ["required_workflows not configured -- the checker cannot know "
                               "which CI is owed; configure it (or [] to skip deliberately)"])
    notes = []
    if not owed:
        notes.append("CI owedness: DISABLED by config (empty required_workflows)")
    runs = gh_json(["run", "list", "-R", repo, "--commit", head, "--limit", "30",
                    "--json", "databaseId,workflowName,status,conclusion,event"])
    pr_runs = [r for r in runs if r.get("event") == "pull_request"]
    by_wf = {}
    for r in pr_runs:
        by_wf.setdefault(r.get("workflowName") or "?", []).append(r)

    wf_names = workflow_meta(repo) if owed else {}
    green_run_by_path = {}
    for item in owed or []:
        wf_path, when = item["path"], item.get("when", "always")
        name = wf_names.get(wf_path)
        if name is None:
            reject("CHECK-ERROR", [f"workflow {wf_path} not found in {repo} -- owedness unknown"])
        if when == "paths":
            hits = diff_hits(pr_files, pull_request_paths(repo, wf_path, head))
            if not hits:
                notes.append(f"{name}: not owed (diff misses its pull_request.paths)")
                continue
            owed_because = f"diff hits its paths ({hits[0]}{', ...' if len(hits) > 1 else ''})"
        else:
            owed_because = "unconditional trigger"
        wf_runs = by_wf.get(name, [])
        if not wf_runs:
            reject("CI-NOT-RUN", [f"{name} is owed at {head[:12]} ({owed_because}) but no "
                                  "pull_request run exists -- a control that never ran proves nothing"])
        verdicts = [judge_run_by_jobs(repo, r) + (r,) for r in wf_runs]
        green = [(d, r) for v, d, r in verdicts if v == "GREEN"]
        if green:
            notes.append(green[0][0])
            green_run_by_path[wf_path] = green[0][1]
            continue
        if any(v == "UNFINISHED" for v, _, _ in verdicts):
            reject("CI-PENDING", [d for v, d, _ in verdicts if v == "UNFINISHED"])
        if all(v == "EMPTY" for v, _, _ in verdicts):
            reject("CI-EMPTY", [d for _, d, _ in verdicts])
        reject("CI-RED", [d for v, d, _ in verdicts if v == "RED"] or [d for _, d, _ in verdicts])

    t = cfg.get("test_owed")
    if t and diff_hits(pr_files, t.get("paths") or []):
        run = green_run_by_path.get(t["workflow_path"])
        if run is None:
            reject("CI-UNTESTED", [f"test_owed paths are touched but {t['workflow_path']} has no "
                                   "green owed run to inspect -- add it to required_workflows"])
        _, jobs = run_jobs(repo, run["databaseId"])
        needle = t.get("job_name_contains", "test").lower()
        n = sum(1 for j in jobs if needle in (j.get("name") or "").lower()
                and j.get("conclusion") == "success")
        if n == 0:
            reject("CI-UNTESTED", [f"diff touches test-owed paths but the green run executed 0 "
                                   f"'{needle}' jobs -- skipped means not applicable, never passed"])
        notes.append(f"test jobs executed: {n}")
    return notes


def check_actions_queue(cfg, repo, allow_displacement):
    """Shared-concurrency deploy workflow: one running + one pending run; a new
    push cancels the PENDING one silently and permanently."""
    wf = (cfg.get("deploy") or {}).get("workflow")
    if not wf:
        reject("CHECK-ERROR", ["deploy.model=actions but deploy.workflow not configured"])
    runs = gh_json(["run", "list", "-R", repo, "--workflow", wf, "--limit", "50",
                    "--json", "databaseId,status,conclusion,headSha,event,createdAt"])
    active = [r for r in runs if r.get("status") != "completed"]
    if not active:
        return [f"deploy queue clear: no non-completed {wf} run in the last 50"]
    started, unstarted = [], []
    for r in active:
        _, jobs = run_jobs(repo, r["databaseId"])
        line = (f"run {r['databaseId']} status={r['status']} jobs={len(jobs)} "
                f"head={r['headSha'][:12]} event={r['event']}")
        (started if jobs else unstarted).append(line)
    if started:
        reject("QUEUE-BUSY-RUNNING", started +
               ["a started deploy must COMPLETE before the next merge; NEVER cancel it "
                "(its run-level status may say 'queued' with most jobs already done)"])
    if unstarted and not allow_displacement:
        reject("QUEUE-BUSY-PENDING", unstarted +
               ["merging now CANCELS this un-started run silently and permanently (cancelled "
                "runs are never requeued). Only an explicit BACK-TO-BACK directive authorizes "
                "displacing it."])
    return [f"DISPLACEMENT AUTHORIZED -- merging will cancel: {u}" for u in unstarted]


def check_external_deploy(cfg, repo, branch):
    """Auto-deploy-on-push provider: merging IS deploying, so the target branch
    must be job-level green, and an optional provider probe must report idle."""
    notes = []
    cmd = (cfg.get("deploy") or {}).get("status_command")
    if cmd:
        proc = sh(cmd, timeout=120, shell=True, env_extra={"MB_REPO": repo, "MB_BRANCH": branch})
        if proc.returncode != 0:
            reject("QUEUE-BUSY-RUNNING", [f"deploy status command exited {proc.returncode}: "
                                          f"{(proc.stdout + proc.stderr).strip()[:300]}"])
        notes.append("deploy provider probe: idle/healthy")
    tip = gh_json(["api", f"repos/{repo}/commits/{branch}", "--jq", "{sha: .sha}"])["sha"]
    runs = gh_json(["run", "list", "-R", repo, "--commit", tip, "--limit", "10",
                    "--json", "databaseId,workflowName,status,conclusion,event"])
    push_runs = [r for r in runs if r.get("event") == "push"]
    if not push_runs:
        reject("MAIN-CI-UNKNOWN", [f"no push CI run at {branch} tip {tip[:12]} -- cannot "
                                   "establish the branch is green; resolve before merging onto it"])
    by_wf = {}
    for r in push_runs:
        by_wf.setdefault(r.get("workflowName") or "?", []).append(r)
    for wf, wf_runs in by_wf.items():
        verdicts = [judge_run_by_jobs(repo, r) for r in wf_runs]
        if any(v == "GREEN" for v, _ in verdicts):
            continue
        if any(v == "UNFINISHED" for v, _ in verdicts):
            reject("MAIN-CI-PENDING", [d for v, d in verdicts if v == "UNFINISHED"] +
                   [f"{branch}-tip CI still running; retry shortly"])
        reject("MAIN-CI-RED", [d for _, d in verdicts] +
               [f"{branch} is not job-level green; with auto-deploy-on-push, merging onto a "
                f"red {branch} ships the redness"])
    notes.append(f"{branch} tip {tip[:12]} push CI green at job level")
    return notes


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--head", required=True, help="expected head sha (>=12 hex chars)")
    ap.add_argument("--config", default=None, help="path to merge-broker.config.json")
    ap.add_argument("--allow-pending-displacement", action="store_true",
                    help="BACK-TO-BACK only: permit merging over an un-started pending deploy run")
    args = ap.parse_args()

    cfg_path = args.config or ("merge-broker.config.json"
                               if os.path.exists("merge-broker.config.json") else None)
    if cfg_path:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    else:
        reject("CHECK-ERROR", ["no config: pass --config or place merge-broker.config.json in "
                               "the working directory (an explicit config is required so the "
                               "checker never guesses what your repo owes)"])

    if not re.fullmatch(r"[0-9a-fA-F]{12,40}", args.head):
        reject("BAD-REQUEST", [f"expected head sha '{args.head}' is not >=12 hex chars"])

    branch = cfg.get("default_branch", "main")
    pr = gh_json(["pr", "view", args.pr, "-R", args.repo, "--json",
                  "number,state,isDraft,headRefOid,baseRefName,mergeable,mergeStateStatus,url"])
    notes = []

    if pr["state"] != "OPEN" or pr["isDraft"]:
        reject("PR-NOT-OPEN", [f"state={pr['state']} draft={pr['isDraft']}"])
    if pr.get("baseRefName") != branch:
        reject("BASE-NOT-DEFAULT", [f"base is {pr.get('baseRefName')}, protocol governs {branch}"])
    live = pr["headRefOid"].lower()
    if not live.startswith(args.head.lower()):
        reject("HEAD-MOVED", [f"requested {args.head[:12]} but live head is {live[:12]} -- "
                              "the recorded evidence describes code nobody read; re-review required"])
    notes.append(f"head pinned: {live[:12]}")
    for _ in range(3):  # UNKNOWN = mergeability still computing; settle it, never assume
        if pr.get("mergeable") in ("MERGEABLE", "CONFLICTING"):
            break
        time.sleep(5)
        pr = gh_json(["pr", "view", args.pr, "-R", args.repo,
                      "--json", "number,mergeable,mergeStateStatus"])
    if pr.get("mergeable") == "CONFLICTING":
        reject("CONFLICT", [f"mergeStateStatus={pr.get('mergeStateStatus')} -- first-ready wins, "
                            "second rebases; never force-merge"])
    if pr.get("mergeable") != "MERGEABLE":
        reject("MERGEABILITY-UNKNOWN", ["GitHub has not settled mergeability; retry shortly"])

    number = pr.get("number") or int(args.pr)
    check_review_gate(cfg, args.repo, number, live, notes)
    pr_files = pr_changed_files(args.repo, number)
    notes += check_ci_at_head(cfg, args.repo, live, pr_files)

    model = (cfg.get("deploy") or {}).get("model", "none")
    if model == "actions":
        notes += check_actions_queue(cfg, args.repo, args.allow_pending_displacement)
    elif model == "external":
        notes += check_external_deploy(cfg, args.repo, branch)
    else:
        notes.append("deploy queue: no deploy model configured")

    print(f"READY {args.repo}#{number} @ {live}")
    for line in notes:
        print(f"  {line}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # fail closed, never a silent pass
        reject("CHECK-ERROR", [f"{type(exc).__name__}: {exc}"])
