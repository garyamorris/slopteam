---
name: slopteam
description: Coordinate separate planner, builder, tester and deployer agents to take an idea through Git-repository tickets, code, tests and verified deployment. Use for Slopteam runs and their recovery.
---

# Slopteam

You are the coordinator. Four **separate Codex conversations** perform the work:

| Agent | Owns | Hands off |
| --- | --- | --- |
| `planner` | Scope, acceptance criteria, local tickets | Committed ticket plan |
| `builder` | One ticket or repair | Implemented ticket and clean commit |
| `tester` | Independent review and meaningful tests | Pass/fail evidence for that commit |
| `deployer` | Release to the authorized target and live smoke | Verified URL and release identity |

Do not perform these stages yourself or relabel one conversation as multiple
agents. Separate agents need distinct session IDs; different models are optional.
Codex is the default for every role. No role spawns further agents.

Use **Git repositories**, not GitHub services. Tickets live in committed Markdown
at `docs/slopteam/RUN_ID/TICKET_ID.md`. Do not use `gh`, GitHub issues/APIs or
remote pushes unless the user separately changes that instruction. An existing
Git remote can remain configured without being used.

## Coordinator setup

The helper is `scripts/pipeline.py` relative to this skill, or the installed
`slopteam pipeline` shortcut. Prefer the helper's absolute path when handing it
to workers so the bundle is portable without the shortcut. Python 3.10+ is enough.
Commands take `--repo ABSOLUTE_WORKSPACE` and optional `--run ID` **before** the
subcommand. `status` shows local checkpoints under `.slopteam/`, excluded from Git.

Inspect the workspace and repository instructions briefly. Reuse a suitable
clean task worktree or create one if isolation is needed. Preserve other work.
Use one task worktree for the run and **one active worker at a time**; each worker
has its own conversation and receives the previous stage's committed files.
There is no automatic parallel writing or cherry-pick step.

Read `status`; if there is no run, use `init --team --idea "..."`. Resume existing
progress rather than creating duplicate tickets or deployments. If no Git repo
exists, initialize it in the requested directory. The planner can make the first
commit. Do not require a remote repository.

## Dispatch and handoff

Use `sys_session_send` with `agent` set to one of the four role names, a unique
human-readable `title`, and `args.input` containing a standalone assignment.
Include the role, absolute workspace and helper paths, run ID, assigned ticket,
acceptance criteria, current commit, prior stage artifacts, authorized deployment
target, and constraints. Tell the worker to run every command in that worktree;
the send API does not accept a `cwd` override. Use
`args.purpose: "implement"` for planner/builder/deployer and `"review"` for tester;
those values are transport categories, not permission to exceed the role.
Use a fresh conversation for each dispatch, including each builder ticket and
new tester pass. Do not reuse the builder's conversation for testing.

Immediately record the returned `conversation_id`:

```sh
python3 /absolute/path/pipeline.py --repo /task/worktree worker start \
  --role planner --session-id ACTUAL_CONVERSATION_ID --task "Plan the release"
```

For a builder, also pass `--ticket T1`. The helper rejects reused session IDs,
concurrent workers and out-of-order stages. Never invent an ID. If dispatch has
an uncertain outcome, inspect Omni's sessions/history before creating another.

Workers run autonomously and notify the coordinator through the inbox. Call
`sys_read_inbox` once; if the dispatched worker is still active, end your turn
and let completion wake you. Do not poll with sleeps/timers. Use
`sys_session_get_history` for unclear/empty results or recovery. Once terminal
completion is confirmed, record it using `worker finish --session-id ID
--status completed --summary "Result and evidence"`, or `--status failed`.
Worker completion is recorded by the coordinator, never self-certified by a worker.
Then dispatch the next stage in the same turn.

## Stage contracts

1. **Planner:** inspect product context; write a small dependency-ordered plan.
   The JSON schema is `{"tickets":[{"id":"T1","title":"...","body":"...",
   "acceptance":["observable outcome"],"depends_on":[]}]}`. Store the JSON under
   `.slopteam/` and load with `plan --file ...`; the helper writes versioned tickets.
   Commit those tickets. For an existing plan, inspect and continue it.
2. **Builder:** dispatch one fresh worker for each available ticket. It uses
   `ticket T1 --status in_progress`, implements and checks the feature, then
   `ticket T1 --status done --summary "..."`. It commits code AND ticket updates.
   A returned summary alone is not QA. Do not dispatch the tester until all
   tickets are done and the last builder has stopped.
3. **Tester:** send the acceptance contracts and actual files/commit to a fresh
   conversation. It independently inspects behavior, writes missing regression
   tests if useful, commits them and runs every relevant test/build/lint via
   `check --command "..."`. Every check must pass on the final clean commit.
   If it fails, record the failed worker, send the finding to a new builder for
   the affected ticket, and then launch a new tester. A done ticket can reopen
   with `--status in_progress --summary "QA finding"`. Stop after three repair
   cycles without progress. Do not bypass failing or stale evidence.
4. **Deployer:** dispatch only after the tester has completed successfully.
   It runs `deploy --command "..." --smoke-command "..." --environment preview
   --url https://TARGET` using existing project deployment scripts/credentials.
   Verify the correct release and critical behavior at the actual target.
   `verify-deploy` reruns smoke only after reconciling an uncertain deployment;
   `--retry` is for a diagnosed failed deployment, never a blind repeat.
   If deployment needs code changes, return to a builder then a fresh tester.

The release gate requires completed separate planner/builder/tester sessions,
current successful tests, the tester's source fingerprint, a clean commit and
an assigned deployer. No-op checks do not prove correctness. A deployer cannot
complete until deployment and smoke verification succeed.

If no deployment target or credential exists, preserve the tested release and
request the missing input. Do not invent hosting, buy services or treat a local
build as deployment. Only use a local deployment target when that is the user's
chosen scope. Report the tickets, worker roles/results, commit, checks, deployed
URL and limitations. Keep tickets marked done once implemented; deployment state
belongs in the checkpoint, so completing deployment does not change tested code.

## Recovery

Read `status` and reconcile each recorded running worker with Omni before doing
anything new. Never redispatch a live worker. Inspect its history if the inbox
notification was lost; record its confirmed terminal result and proceed.
On stop/pause, cancel the active worker with `sys_cancel_task` using its recorded
conversation ID, confirm its state, and start no new work until requested.
Do not claim a worker stopped if cancellation is unconfirmed. Keep logs and state.
