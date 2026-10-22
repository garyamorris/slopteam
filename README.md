# slopteam

**Idea → tickets → code → tests → deploy.** A small, resumable development
pipeline installed as an [Omnigent](https://omnigent.ai) agent. **Codex is the
default harness.** It uses your existing Omni login and model configuration.

Give it an idea in a repository. Slopteam scopes the first useful release,
creates tickets, implements them in dependency order, runs checks, deploys
to your configured target, and verifies the result. The agent makes engineering
decisions; a dependency-free Python helper records progress and checks stage gates.

## Install

Requires Omnigent (validated with 0.19.0), a configured Codex harness, Git,
Python 3.10+ on macOS/Linux, and authenticated `gh` for GitHub tickets.

```sh
git clone https://github.com/garyamorris/slopteam.git
cd slopteam
python3 scripts/install.py
```

This registers the agent in Omni and links `~/.local/bin/slopteam` to this
checkout. Keep the checkout in place. Re-run the installer after pulling updates.
Use `--agent-only` to skip the command shortcut. No background service is needed.

You can also install the self-contained agent bundle directly:

```sh
omni agent add garyamorris/slopteam/agent --yes
```

## Use

In Omni, create a chat, select **slopteam**, choose the target repository and
describe your idea. Its Codex prompt loads the bundled workflow automatically.

From a terminal:

```sh
slopteam --repo /path/to/my-app "Build a simple habit tracker and deploy a preview"
slopteam --repo /path/to/my-app "Continue the current run"
slopteam --repo /path/to/my-app --resume CONVERSATION_ID "Continue"
```

Use `--model MODEL` or `--harness HARNESS` for an explicit override. The launcher
passes `--harness codex` by default even when another Omni global harness is set.
Installation adds Slopteam beside your existing agents; its harness is scoped
to Slopteam. `--dry-run` prints the launch request without starting a session.

For inspection and recovery:

```sh
slopteam pipeline --repo /path/to/my-app status
slopteam pipeline --repo /path/to/my-app --run RUN_ID status
slopteam pipeline --help
```

## What it records

Each run keeps an idea, ticket dependency graph, GitHub issue links, ticket
progress, test/build results, deployment attempt and smoke result. State and logs
live in `.slopteam/` in the target worktree; the helper adds a local Git exclusion.
Do not commit the state or logs: commands may print sensitive project information.

- **Tickets:** validated dependencies; deterministic run markers prevent duplicate
  issue creation on ordinary retries. An uncertain API result requires reconciliation.
- **Code:** the agent implements each ticket and records its acceptance evidence.
- **Tests:** the helper runs actual shell commands, records exit status and logs,
  and fingerprints source. Code changes invalidate the earlier test results.
- **Deploy:** requires all tickets implemented, a clean commit and current passing
  checks. Deployment runs once, followed by a live smoke command.
- **Recovery:** failures remain visible. Interrupted deployment attempts are never
  automatically repeated; `verify-deploy` reruns only verification.

The helper verifies process results and source identity. The agent must choose
meaningful tests and a smoke check that proves the right release is live; a zero
exit code from a no-op command is not proof of product correctness. Ignored files,
external services and environment variables are outside the source fingerprint.

Deployments use the project's existing provider and credentials. If no target is
configured, Slopteam completes the work it can and asks for the missing target.
No hosting account, production deployment or paid service is created merely by
installing this agent. Native harness permission prompts still apply.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests exercise real temporary Git repositories and shell commands, including
deployment/smoke failures and stale-test rejection. GitHub is mocked in unit tests;
they never publish issues or deploy to an external service. CI runs on Python
3.10, 3.12 and 3.14.

`agent/` is the portable Omni bundle. Its `skills/slopteam/scripts/pipeline.py`
contains the ledger and command runner. `scripts/launch.py` and `bin/slopteam`
provide the convenience launcher; the bundle also works without those shortcuts.
