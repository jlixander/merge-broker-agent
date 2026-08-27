# merge-broker

**A cross-platform AI subagent that safely executes exactly one guarded merge to main
per invocation — and refuses everything else.**

Born from a real incident: a night of uncoordinated merges in a multi-agent fleet
produced silently cancelled deploys, orphaned commits (merged, CI-green, never
deployed — with nothing red to notice), and false-green test runs that had executed
zero tests. Every check in this agent exists because one of those things actually
happened.

Works with **Claude Code, OpenAI Codex CLI, Gemini CLI, GitHub Copilot CLI**, and any
open-source agent framework that can run shell commands (OpenHands, Aider, CrewAI,
LangGraph, custom harnesses). The agent definition is plain markdown; the verifier is
a single stdlib-only Python script driven by the GitHub CLI.

## The traps it defends against

| Trap | What actually goes wrong | The check |
|---|---|---|
| **Head moved since request** | The PR gained commits after review — the recorded evidence describes code nobody read | The request pins a head sha; any mismatch → `REJECTED-HEAD-MOVED` |
| **Rollup vs. spawned jobs** | A workflow run can be GREEN having spawned **zero** jobs (empty matrix, path filters) — the rollup can't tell that from passing | Jobs are counted; 0 jobs → `REJECTED-CI-EMPTY` |
| **"Skipped" read as "passed"** | Skipped tests mean *not applicable*, never *passed* | Skips are surfaced and attributed; test-owed paths with 0 executed test jobs → `REJECTED-CI-UNTESTED` |
| **Run-level vs. job-level status** | A run reporting `queued` can have most jobs already finished; a run reporting `failure` can have shipped everything | Every verdict is computed from job-level data only |
| **The wrong workflow is green** | A green *manual dispatch* at the same sha can mask a red PR run; an unrelated workflow's green can mask a missing required one | Only `pull_request`-event runs count, and every **owed** workflow must have run (owedness parsed from the workflow's own `paths:` at the head sha) |
| **Pending-slot cancellation** | With a shared concurrency group + `cancel-in-progress: false`, GitHub keeps one running + one pending run — a new push **silently cancels the pending one, forever** | Un-started deploy runs block the merge (`QUEUE-BUSY-PENDING`) unless displacement is explicitly directed |
| **Merging IS deploying** | Auto-deploy providers (Amplify, Vercel, Netlify, Pages) build main regardless of CI color — merging onto a red main ships the redness | External-deploy model requires the main tip job-level green + provider idle |
| **Stale review evidence** | An approval pinned to an older commit is evidence about different code | Approvals are checked against the exact head sha |
| **Scanner false positives** | Policy hooks that regex shell commands can fire on a *mention* of a merge command inside a quote or heredoc | The agent confines the merge literal to one command and treats hook false positives as hook defects — never a reason to weaken a gate |

## How it works

```
Coordinator ──► merge-broker agent ──► scripts/merge_broker_check.py (read-only, fail-closed)
   (owns the queue)                        │
                                           ├─ READY            ──► gh pr merge --squash ──► MERGED <sha>
                                           └─ REJECTED-<REASON> ──► terse reply, nothing mutated
```

- **One PR per invocation.** The coordinator (a human release manager or a designated
  coordinator agent session) owns ordering; the broker executes one request.
- **DRY-RUN by default.** The broker merges only when the prompt contains the literal
  token `EXECUTE`. Anyone else invoking it gets a harmless verification.
- **Fail-closed.** Any checker error is a rejection, never a pass.
- **Terse contract.** Replies are one verdict line — `READY` / `REJECTED-<REASON>` /
  `MERGED <sha>` — plus a few evidence lines.

## Install

**1. Commit the verifier and config to the target repo:**

```bash
cp scripts/merge_broker_check.py  YOUR_REPO/scripts/
cp examples/merge-broker.config.json  YOUR_REPO/merge-broker.config.json   # then edit it
```

**2. Install the agent on your platform:**

| Platform | Where |
|---|---|
| **Claude Code** | `cp merge-broker.md .claude/agents/` (per-project) or `~/.claude/agents/` (all projects). Invoke by asking Claude to use the merge-broker agent, e.g. `use merge-broker: DRY-RUN. MERGE-REQUEST owner/repo#123 <head-sha> fix: ...` |
| **Codex CLI** | Delete the YAML frontmatter (everything between the two `---` lines), save as `~/.codex/prompts/merge-broker.md`, invoke with `/merge-broker MERGE-REQUEST ...` — or paste the body into the repo's `AGENTS.md` |
| **Gemini CLI** | Body into `GEMINI.md`, or save as a reusable prompt/system-instruction file |
| **Copilot CLI** | Body as a prompt file / custom instruction |
| **OpenHands / Aider / CrewAI / LangGraph / custom** | Use the body (below the frontmatter) as the agent's system prompt; give the agent shell access |

**3. Requirements:** Python 3.9+, [GitHub CLI](https://cli.github.com/) authenticated
for the target repo (`gh auth status`). No other dependencies.

## Configuration (`merge-broker.config.json`)

| Key | Meaning |
|---|---|
| `default_branch` | The branch the protocol governs (default `"main"`). PRs targeting anything else are rejected. |
| `review_gate.mode` | `"github-approval"` (default): PR must be APPROVED, at the exact head sha. `"command"`: run your own evidence gate (`MB_REPO`/`MB_PR`/`MB_HEAD` env vars; exit 0 = pass) — plug in your review ledger, ticket system, or sign-off store. `"off"`: skip deliberately. |
| `required_workflows` | Workflows a PR **owes** a green run of: `"when": "always"`, or `"when": "paths"` to owe it only when the diff hits the workflow's own `on.pull_request.paths` (parsed at the head sha, never from a stale local copy). `[]` disables CI owedness — deliberately. |
| `test_owed` | Optional: if the diff hits `paths`, the green run of `workflow_path` must have executed ≥1 successful job whose name contains `job_name_contains`. This is the "skipped ≠ passed" gate. |
| `deploy.model` | `"actions"`: a deploy workflow with a shared concurrency group — enables the pending-slot queue check. `"external"`: an auto-deploy-on-push provider — enables the main-tip-green check and optional `status_command` probe (exit 0 = idle/healthy). `"none"`: no deploy gating. |

## Verdicts

`READY` (exit 0) — every gate passed; in EXECUTE mode the broker proceeds to merge.
Everything else is `REJECTED-<REASON>` (exit 1). Reasons starting with `QUEUE-BUSY`
or ending in `-PENDING`/`-UNKNOWN` are **retryable** (transient state — the broker
polls up to 10 minutes); all others are **terminal** for that request:
`BAD-REQUEST`, `PR-NOT-OPEN`, `BASE-NOT-DEFAULT`, `HEAD-MOVED`, `CONFLICT`,
`NO-APPROVAL`, `STALE-APPROVAL`, `CHANGES-REQUESTED`, `EVIDENCE-GATE`, `CI-NOT-RUN`,
`CI-EMPTY`, `CI-RED`, `CI-UNTESTED`, `MAIN-CI-RED`, `CHECK-ERROR`, and the broker's
own `GUARD-DENIED`, `MERGE-FAILED`, `MERGE-UNCONFIRMED`.

## Safety design

- The checker **never mutates anything** — it is safe to run anywhere, anytime.
- The agent performs **exactly one mutating command** per invocation (`gh pr merge
  --squash`), and only after a `READY`.
- **Fail-closed everywhere**: unexpected errors reject; unknown job conclusions are
  not green (whitelist, not blocklist); unparseable workflow triggers reject rather
  than assume.
- The broker **never cancels a workflow run** and never dispatches deploy/rollback
  workflows.
- Back-to-back merging (deliberately letting an un-started intermediate deploy be
  displaced) requires an explicit `BACK-TO-BACK` directive and every displaced run is
  enumerated in the reply — stated, never silent.

## License

MIT — see [LICENSE](LICENSE).
