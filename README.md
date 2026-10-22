# slopteam

**Idea → Git-repo tickets → code → tests → deploy**, with separate agents.

Slopteam is an [Omnigent](https://omnigent.ai) coordinator with four workers,
all defaulting to **Codex**:

| Agent | Job |
| --- | --- |
| Planner | Scope the smallest useful release and write tickets |
| Builder | Implement a ticket and commit its code |
| Tester | Independently review behavior and run meaningful checks |
| Deployer | Deploy the tested commit and verify the live result |

Each worker runs in its own Omni conversation. The coordinator dispatches work,
records session IDs and moves the result to the next stage. Workers run in order
in one task worktree. Each builder ticket and each testing pass gets a fresh
conversation; a tester never reuses the builder's conversation. Failed QA goes
back to a builder, then a new tester.

Tickets are ordinary **Git-tracked Markdown files** under
`docs/slopteam/RUN_ID/`. There is no GitHub API, issue tracker, `gh` dependency or
automatic push. Local Git repositories work without a remote.

## Install

Requires Omnigent (validated with 0.19.0), a configured Codex harness, Git and
Python 3.10+ on macOS/Linux. From this checkout:

```sh
python3 scripts/install.py
```

This installs the entire agent team in Omni and links `~/.local/bin/slopteam`
to this checkout. Keep the checkout in place. Use `--agent-only` to skip the
shortcut, or install just the portable bundle with `omni agent add ./agent`.
No background service is needed; existing Omni agents remain available.

## Use

In Omni, create a chat, select **slopteam**, choose the target repository and
give it an idea. The coordinator loads its workflow and dispatches the planner.
The stage conversations appear as child sessions in Omni.

```sh
slopteam --repo /path/to/my-app "Build a habit tracker and deploy to my preview target"
slopteam --repo /path/to/my-app "Continue the current run"
slopteam --repo /path/to/my-app --resume CONVERSATION_ID "Continue"
```

The coordinator and four workers all use your configured Codex model by default.
`--model` and `--harness` override the coordinator only; worker defaults are in
`agent/agents/ROLE/config.yaml`. `--dry-run` prints the launch request.

For inspection:

```sh
slopteam pipeline --repo /path/to/my-app status
slopteam pipeline --repo /path/to/my-app --run RUN_ID status
slopteam pipeline --help
```

## Progress and release checks

The coordinator starts agent runs with `init --team`. Checkpoints and command
logs live in `.slopteam/`, locally excluded from Git. They include the actual
worker session IDs, role, assigned ticket, result summary, commit and source
fingerprint. Git tracks the ticket contracts and implementation progress;
checkpoints remain local to the task worktree.

The helper rejects duplicate worker session IDs, concurrent workers in the same
run and out-of-order stages. Deployment requires completed planner/builder/tester
agents, a separate assigned deployer, all tickets done, a clean commit, current
passing checks and the independent tester's matching source fingerprint.

A deployment runs once, followed by a live smoke check. Interrupted attempts are
never automatically repeated; `verify-deploy` retries verification only. A worker
failure stays visible and the coordinator reconciles existing sessions before
redispatching. Installation never starts a product deployment.

The agent must choose useful tests and a smoke check that proves the right
release is live. A zero exit code from a no-op is not proof of correctness.
Ignored files, services and environment variables are outside the source
fingerprint. The helper records observed worker IDs/results; it is not a security
boundary against an agent deliberately falsifying its checkpoint.

Deployments use your project's scripts, target and credentials. If a target is
missing, the team preserves the tested work and asks for that detail. No hosting
account or paid service is created by installation. An existing Git remote stays
configured, but the pipeline does not push to it.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use real temporary Git repositories and shell commands. They check stage
ordering, worker separation, Git tickets, QA retries, stale evidence, interrupted
deployments and local deployment/smoke failure recovery. They never contact
GitHub or publish to external hosting.

`agent/` is the self-contained bundle, including the four role configurations,
coordinator workflow and dependency-free Python ledger. `scripts/launch.py` and
`bin/slopteam` are optional local conveniences. Without `--team`, the helper can
also be used manually; the installed agent always uses the full team workflow.
