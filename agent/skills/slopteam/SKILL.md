---
name: slopteam
description: Take a software idea through GitHub tickets, implementation, tests and a verified deployment, with resumable progress. Use for slopteam runs or an explicitly requested idea-to-deployment pipeline.
---

# Slopteam

Turn the user's idea into a small working release. Work through the stages below
in one session, continuing after successful steps. Codex is the default
runtime; retain the selected model. This workflow does not require multiple agents.

The deterministic helper is `scripts/pipeline.py` relative to this skill. Run it
with Python 3.10+. The installed shortcut is `slopteam pipeline ...`. Start with
`slopteam pipeline --help` and `slopteam pipeline status`. All pipeline commands
run from the target Git repository, or accept `--repo /absolute/path` before the
subcommand. State and command logs live in `.slopteam/`, locally excluded from Git.
If the shortcut is unavailable, use the helper's absolute path instead.

## Idea and tickets

Inspect repository instructions, existing work, test commands, deployment files
and GitHub remote. Use a clean task branch/worktree; preserve unrelated changes.
If no repository exists, initialize one in the requested location. Create a
remote when the user authorized publishing; otherwise keep tickets local.
Avoid choosing a paid hosting service or production target without user direction.

Create a short product brief with the smallest useful scope and observable
acceptance criteria. Use `init --idea "..."` once. On continuation, inspect
`status` and resume the existing run instead of creating duplicate tickets.
`--run ID` selects an older run. Create a JSON plan file in `.slopteam/`:

```json
{"tickets":[{"id":"T1","title":"Build the smallest useful feature",
"body":"Scope and implementation notes",
"acceptance":["An observable result the user can verify"],"depends_on":[]}]}
```

Load it with `plan --file .slopteam/plan.json`. Tickets should be small enough
to implement and test individually; dependencies determine execution order.
Use `tickets --github OWNER/REPO` to publish real GitHub issues. This command
reconciles exact run markers before creating issues. If a creation outcome is
uncertain, it stops: check GitHub and use `reconcile-ticket ID --issue NUMBER`,
or `--not-created` only after confirming absence. Do not blindly retry mutations.

## Code and tests

For each available ticket, run `ticket ID --status in_progress`, implement it,
and exercise its acceptance criteria. Commit scoped changes, then run
`ticket ID --status done --summary "What changed and how it was checked"`.
The helper enforces dependencies. Done means implemented, not deployed.
If GitHub is unavailable, local tickets still support implementation and tests.

After all tickets are done, run each relevant test/build/lint command through
`check --command "..."`. Select meaningful checks for the actual change; a
trivial `true` command is not a test. The helper records exit codes, logs and a
fingerprint of committed, modified and untracked source. Failed checks block
deployment; any source change invalidates previous checks. Fix and rerun affected
checks, with at most three repair attempts without progress before reporting a
concrete blocker. Do not delete or weaken a failing check to make the run green.

## Deploy and verify

Use the repository's existing deployment mechanism and the user's authorized
target. Complete build/package work first. If a target, credential or permission
is missing, preserve the tested result and state exactly what is needed.
Configure or repair the deployment scripts when that is within the request.
Prefer a preview for a new app unless the user chose production. Do not merge
protected branches, force push, or invent successful deployments.

Run `deploy --command "DEPLOY_COMMAND" --smoke-command "LIVE_CHECK_COMMAND"
--environment preview --url https://DEPLOYED_URL` only after the checks pass.
Both commands must be concrete and noninteractive. A command may call a script
that waits for the hosting job and writes the returned URL; the smoke script
must check the resulting deployment, its release identity and critical behavior.
The `--url` value must identify that same target. A queued build is not success.
When the URL is allocated dynamically, use the provider's stable preview alias
or a checked-in deployment script with an explicit target; do not guess a URL.

The helper requires a clean commit, all tickets done, current passing checks,
a successful deployment command and a successful smoke check. It marks an
interrupted deployment as uncertain and never repeats it implicitly. Use
`verify-deploy` to retry only the smoke check after reconciling the provider's
state. Use `deploy ... --retry` for a known failed deployment only when a retry
is appropriate. A failed smoke check needs diagnosis before redeployment.

After verified deployment, run `close-tickets` for published issues. If PRs are
used, keep their state accurate: shipping a preview does not imply a merge.
Report the repository/PR, tickets, exact checks, deployment URL and any remaining
limitation. A local build or mock deployment is a test, not a live deployment.

## Recovery

`status` is the source of truth after an interruption. Completed stages are
retained, source changes invalidate check evidence, and deployment commands are
never automatically retried. Do not edit checkpoint JSON to bypass a gate.
The selected harness's permissions remain in effect. If the user stops or pauses,
stop new work and leave the checkpoint available for an explicit continuation.
