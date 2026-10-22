#!/usr/bin/env python3
"""Slopteam's dependency-free, resumable delivery ledger (Python 3.10+)."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path


class Blocked(Exception):
    """An actionable pipeline gate, reported without a traceback."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def call(argv, cwd, data=None):
    try:
        result = subprocess.run(
            argv, cwd=cwd, input=data, text=True, capture_output=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked(
            f"{argv[0]} unavailable or timed out: {type(exc).__name__}"
        ) from exc
    if result.returncode:
        # CLI responses may contain credentials or request bodies; retain only operation.
        raise Blocked(
            f"{' '.join(argv[:2])} failed (exit {result.returncode}); check access and arguments"
        )
    return result.stdout.strip()


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


class Pipeline:
    def __init__(self, repo, run_id=None):
        self.repo = Path(call(["git", "rev-parse", "--show-toplevel"], repo)).resolve()
        self.directory = self.repo / ".slopteam"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        exclude = Path(
            call(["git", "rev-parse", "--git-path", "info/exclude"], self.repo)
        )
        if not exclude.is_absolute():
            exclude = self.repo / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        content = exclude.read_text() if exclude.exists() else ""
        if "/.slopteam/" not in content.splitlines():
            with exclude.open("a") as stream:
                stream.write("\n/.slopteam/\n")
        self.run_id = run_id
        self.state = None

    @contextlib.contextmanager
    def locked(self):
        with (self.directory / "lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Blocked(
                    "Another Slopteam command is running in this worktree"
                ) from exc
            try:
                yield self
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def load(self):
        if not self.run_id:
            pointer = self.directory / "active"
            if not pointer.exists():
                raise Blocked('No run yet. Start with init --idea "your idea"')
            self.run_id = pointer.read_text().strip()
        if not re.fullmatch(r"[a-f0-9]{12}", self.run_id):
            raise Blocked("Invalid run ID")
        path = self.directory / f"{self.run_id}.json"
        if not path.exists():
            raise Blocked(f"Run {self.run_id} does not exist")
        self.state = json.loads(path.read_text())
        return self.state

    def save(self):
        self.state["updated_at"] = now()
        atomic_json(self.directory / f"{self.run_id}.json", self.state)

    def init(self, idea, team=False):
        if not idea.strip():
            raise Blocked("The idea cannot be empty")
        if (self.directory / "active").exists():
            self.load()
            if self.state.get("deployment", {}).get("status") != "verified":
                raise Blocked("An unfinished run exists; use status and continue it")
        self.run_id = uuid.uuid4().hex[:12]
        self.state = {
            "schema": 2,
            "id": self.run_id,
            "idea": idea,
            "created_at": now(),
            "tickets": [],
            "checks": {},
            "deployment": {},
            "team": team,
            "workers": [],
        }
        self.save()
        pointer = self.directory / "active.tmp"
        pointer.write_text(self.run_id + "\n")
        os.replace(pointer, self.directory / "active")
        return self.state

    def plan(self, value):
        tickets = value.get("tickets") if isinstance(value, dict) else None
        if not isinstance(tickets, list) or not tickets:
            raise Blocked("Plan needs a nonempty tickets array")
        output = []
        for ticket in tickets:
            if not isinstance(ticket, dict) or not re.fullmatch(
                r"T[1-9][0-9]*", str(ticket.get("id", ""))
            ):
                raise Blocked("Each ticket needs an ID such as T1")
            for key in ("title", "body"):
                if not isinstance(ticket.get(key), str) or not ticket[key].strip():
                    raise Blocked(f"{ticket['id']} needs a nonempty {key}")
            acceptance, deps = ticket.get("acceptance"), ticket.get("depends_on", [])
            if (
                not isinstance(acceptance, list)
                or not acceptance
                or any(not isinstance(x, str) or not x.strip() for x in acceptance)
            ):
                raise Blocked(f"{ticket['id']} needs acceptance criteria")
            if not isinstance(deps, list) or any(not isinstance(x, str) for x in deps):
                raise Blocked("depends_on must be a list of ticket IDs")
            output.append(
                {k: ticket[k] for k in ("id", "title", "body", "acceptance")}
                | {"depends_on": deps, "status": "todo"}
            )
        by_id = {t["id"]: t for t in output}
        if len(by_id) != len(output):
            raise Blocked("Duplicate ticket IDs")
        visited, visiting = set(), set()

        def visit(tid):
            if tid not in by_id:
                raise Blocked(f"Unknown dependency: {tid}")
            if tid in visiting:
                raise Blocked("Ticket dependencies contain a cycle")
            if tid in visited:
                return
            visiting.add(tid)
            for dep in by_id[tid]["depends_on"]:
                visit(dep)
            visiting.remove(tid)
            visited.add(tid)

        for tid in by_id:
            visit(tid)
        if self.state["tickets"]:
            raise Blocked(
                "A plan already exists; preserve it and continue the current run"
            )
        self.state["tickets"] = output
        for ticket in output:
            self.write_ticket(ticket)
        self.save()

    def write_ticket(self, ticket):
        """Keep the ticket contract and progress in ordinary versioned Git files."""
        directory = self.repo / "docs" / "slopteam" / self.run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{ticket['id']}.md"
        path.write_text(
            f"# {ticket['id']}: {ticket['title']}\n\n"
            f"Run: {self.run_id}\n\nStatus: {ticket['status']}\n\n"
            f"Idea: {self.state['idea']}\n\n{ticket['body']}\n\n"
            "## Acceptance criteria\n\n"
            + "\n".join("- " + x for x in ticket["acceptance"])
            + "\n\nDependencies: "
            + (", ".join(ticket["depends_on"]) or "none")
            + "\n\nProgress: "
            + ticket.get("summary", "Not started")
            + "\n",
            encoding="utf-8",
        )
        ticket["path"] = str(path.relative_to(self.repo))

    def ticket(self, tid):
        for ticket in self.state["tickets"]:
            if ticket["id"] == tid:
                return ticket
        raise Blocked(f"Unknown ticket: {tid}")

    def mark(self, tid, status, summary):
        ticket = self.ticket(tid)
        if status == ticket["status"]:
            return
        allowed = {"todo": "in_progress", "in_progress": "done"}
        reopening = ticket["status"] == "done" and status == "in_progress"
        if reopening and not summary.strip():
            raise Blocked("Reopening a ticket needs --summary describing the repair")
        if not reopening and allowed.get(ticket["status"]) != status:
            raise Blocked("Tickets advance from todo to in_progress to done")
        if any(self.ticket(dep)["status"] != "done" for dep in ticket["depends_on"]):
            raise Blocked("Finish dependencies first")
        if status == "done" and not summary.strip():
            raise Blocked(
                "Done needs --summary with implementation and acceptance evidence"
            )
        ticket.update(status=status, summary=summary, updated_at=now())
        self.write_ticket(ticket)
        self.save()

    def latest_worker(self, role):
        return next(
            (w for w in reversed(self.state.get("workers", [])) if w["role"] == role),
            None,
        )

    def worker_start(self, role, session_id, task, ticket_id=None):
        if role not in ("planner", "builder", "tester", "deployer"):
            raise Blocked("Unknown worker role")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session_id) or not task.strip():
            raise Blocked("A valid session ID and task are required")
        workers = self.state.setdefault("workers", [])
        if any(w["session_id"] == session_id for w in workers):
            raise Blocked(
                "Each dispatch needs a distinct session; do not reuse a builder as tester"
            )
        if any(w["status"] == "running" for w in workers):
            raise Blocked(
                "Finish or reconcile the running worker before dispatching another"
            )
        if (
            role != "planner"
            and (self.latest_worker("planner") or {}).get("status") != "completed"
        ):
            raise Blocked("The planner must complete first")
        if role == "builder":
            if not ticket_id:
                raise Blocked("A builder needs --ticket ID")
            self.ticket(ticket_id)
        if role in ("tester", "deployer"):
            if (self.latest_worker("builder") or {}).get("status") != "completed":
                raise Blocked("The builder must complete first")
            if not self.state["tickets"] or any(
                t["status"] != "done" for t in self.state["tickets"]
            ):
                raise Blocked("Finish all tickets before independent testing")
        if role == "deployer":
            self.release_gate(require_deployer=False)
        workers.append(
            {
                "role": role,
                "session_id": session_id,
                "task": task,
                "ticket": ticket_id,
                "status": "running",
                "started_at": now(),
            }
        )
        self.state["team"] = True
        self.save()

    def worker_finish(self, session_id, status, summary):
        worker = next(
            (w for w in self.state.get("workers", []) if w["session_id"] == session_id),
            None,
        )
        if not worker or worker["status"] != "running":
            raise Blocked("No running worker has that session ID")
        if status not in ("completed", "failed") or not summary.strip():
            raise Blocked("A terminal worker status and evidence summary are required")
        if status == "completed":
            if worker["role"] == "planner" and not self.state["tickets"]:
                raise Blocked("The planner has not produced a plan")
            if (
                worker["role"] == "builder"
                and self.ticket(worker["ticket"])["status"] != "done"
            ):
                raise Blocked("The assigned ticket is not done")
            if call(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"], self.repo
            ):
                raise Blocked("Commit the worker output before marking it completed")
            if worker["role"] == "tester":
                fingerprint = self.release_gate(check_team=False)
            else:
                fingerprint = self.fingerprint()
            if worker["role"] == "deployer":
                deployment = self.state["deployment"]
                if (
                    deployment.get("status") != "verified"
                    or deployment.get("fingerprint") != fingerprint
                ):
                    raise Blocked("The deployer has not verified the current release")
            worker.update(
                head=call(["git", "rev-parse", "HEAD"], self.repo),
                fingerprint=fingerprint,
            )
        worker.update(status=status, summary=summary, finished_at=now())
        self.save()

    def team_gate(self, fingerprint, require_deployer=True):
        for role in ("planner", "builder", "tester"):
            if (self.latest_worker(role) or {}).get("status") != "completed":
                raise Blocked(
                    f"The separate {role} agent must complete before deployment"
                )
        if self.latest_worker("tester").get("fingerprint") != fingerprint:
            raise Blocked("The independent tester must verify the current source")
        if require_deployer and (self.latest_worker("deployer") or {}).get(
            "status"
        ) not in ("running", "completed"):
            raise Blocked("Dispatch a separate deployer agent before deployment")

    def fingerprint(self):
        digest = hashlib.sha256()
        digest.update(call(["git", "rev-parse", "HEAD"], self.repo).encode())
        digest.update(
            call(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"], self.repo
            ).encode()
        )
        files = call(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            self.repo,
        )
        for name in sorted(set(files.split("\0")) - {""}):
            path = self.repo / name
            digest.update(name.encode() + b"\0")
            if path.is_symlink():
                digest.update(b"link:" + os.readlink(path).encode())
            elif path.is_file():
                digest.update(str(path.stat().st_mode).encode())
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif path.is_dir():
                digest.update(
                    b"directory"
                )  # Submodule state is included in git status.
            else:
                digest.update(b"deleted")
        return digest.hexdigest()

    def execute(self, command, timeout, kind):
        if not command.strip() or timeout <= 0:
            raise Blocked("A nonempty command and positive timeout are required")
        log = self.directory / f"{self.run_id}-{kind}-{uuid.uuid4().hex[:8]}.log"
        with log.open("w") as stream:
            process = subprocess.Popen(
                command,
                cwd=self.repo,
                shell=True,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                code = process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                code = 124 if isinstance(exc, subprocess.TimeoutExpired) else 130
        return {
            "command": command,
            "exit_code": code,
            "log": str(log.relative_to(self.repo)),
            "at": now(),
        }

    def check(self, command, timeout):
        fingerprint = self.fingerprint()
        # Persist the pending attempt before executing. A crash cannot retain old green evidence.
        self.state["checks"][command] = {
            "command": command,
            "fingerprint": fingerprint,
            "exit_code": None,
        }
        self.save()
        result = self.execute(command, timeout, "check")
        result.update(
            fingerprint=fingerprint, unchanged=fingerprint == self.fingerprint()
        )
        self.state["checks"][command] = result
        self.save()
        if result["exit_code"] != 0 or not result["unchanged"]:
            raise Blocked(f"Check failed or changed source; inspect {result['log']}")
        return result

    def release_gate(self, check_team=True, require_deployer=True):
        tickets = self.state["tickets"]
        if not tickets or any(t["status"] != "done" for t in tickets):
            raise Blocked("Finish all planned tickets before deployment")
        if call(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], self.repo
        ):
            raise Blocked("Commit the release; deployment requires a clean worktree")
        fingerprint = self.fingerprint()
        checks = self.state["checks"].values()
        if not checks or any(
            c.get("exit_code") != 0
            or not c.get("unchanged")
            or c.get("fingerprint") != fingerprint
            for c in checks
        ):
            raise Blocked(
                "Run all checks successfully on the current source before deployment"
            )
        if check_team and self.state.get("team"):
            self.team_gate(fingerprint, require_deployer)
        return fingerprint

    def deploy(self, command, smoke, environment, url, timeout, retry=False):
        fingerprint = self.release_gate()
        prior = self.state["deployment"]
        if prior.get("status") == "verified":
            raise Blocked(
                "This run is already deployed; start a new run for a new release"
            )
        if prior and not (retry and prior.get("status") == "failed"):
            raise Blocked(
                "A deployment was already attempted; reconcile it and use verify-deploy, or --retry for a known failure"
            )
        if not all(x.strip() for x in (command, smoke, environment)) or not re.match(
            r"^https?://[^\s]+$", url
        ):
            raise Blocked(
                "Deployment requires commands, environment and an HTTP(S) target URL"
            )
        if prior:
            self.state.setdefault("deployment_history", []).append(prior)
        self.state["deployment"] = {
            "status": "running",
            "fingerprint": fingerprint,
            "head": call(["git", "rev-parse", "HEAD"], self.repo),
            "environment": environment,
            "url": url,
            "smoke_command": smoke,
        }
        self.save()
        result = self.execute(command, timeout, "deploy")
        self.state["deployment"]["command_result"] = result
        self.state["deployment"]["status"] = (
            "deployed" if result["exit_code"] == 0 else "failed"
        )
        self.save()
        if result["exit_code"]:
            raise Blocked(
                f"Deployment command failed; inspect {result['log']} before retrying"
            )
        self.verify_deploy(timeout)

    def verify_deploy(self, timeout):
        fingerprint = self.release_gate()
        deployment = self.state["deployment"]
        if not deployment or deployment.get("fingerprint") != fingerprint:
            raise Blocked("No deployment matches the tested source")
        if deployment["status"] == "verified":
            return
        # Persist before smoke: a crash during verification must not appear verified.
        deployment["status"] = "verifying"
        self.save()
        result = self.execute(deployment["smoke_command"], timeout, "smoke")
        deployment["smoke_result"] = result
        deployment["status"] = (
            "verified"
            if result["exit_code"] == 0 and self.fingerprint() == fingerprint
            else "smoke_failed"
        )
        self.save()
        if deployment["status"] != "verified":
            raise Blocked(f"Deployment is unverified; inspect {result['log']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--run", help="Existing run ID; otherwise use the active run")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("init", help="Start a run")
    p.add_argument("--idea", required=True)
    p.add_argument("--team", action="store_true", help="Require separate stage agents")
    sub.add_parser("status", help="Show checkpoint JSON")
    p = sub.add_parser("plan", help="Load a validated ticket dependency graph")
    p.add_argument("--file", type=Path, required=True)
    p = sub.add_parser("ticket", help="Advance one ticket")
    p.add_argument("id")
    p.add_argument("--status", choices=["in_progress", "done"], required=True)
    p.add_argument("--summary", default="")
    p = sub.add_parser("worker", help="Record a real Omni worker dispatch or result")
    wp = p.add_subparsers(dest="worker_action", required=True)
    p = wp.add_parser("start")
    p.add_argument(
        "--role", choices=["planner", "builder", "tester", "deployer"], required=True
    )
    p.add_argument("--session-id", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--ticket")
    p = wp.add_parser("finish")
    p.add_argument("--session-id", required=True)
    p.add_argument("--status", choices=["completed", "failed"], required=True)
    p.add_argument("--summary", required=True)
    p = sub.add_parser("check", help="Run and record a test/build/lint check")
    p.add_argument("--command", required=True)
    p.add_argument("--timeout", type=int, default=900)
    p = sub.add_parser("deploy", help="Deploy current tested commit and smoke test it")
    p.add_argument("--command", required=True)
    p.add_argument("--smoke-command", required=True)
    p.add_argument("--environment", default="preview")
    p.add_argument("--url", required=True)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--retry", action="store_true")
    p = sub.add_parser("verify-deploy", help="Rerun only the live smoke check")
    p.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        with Pipeline(args.repo, args.run).locked() as pipeline:
            if args.action == "init":
                pipeline.init(args.idea, args.team)
            else:
                pipeline.load()
                if args.action == "plan":
                    pipeline.plan(json.loads(args.file.read_text()))
                elif args.action == "ticket":
                    pipeline.mark(args.id, args.status, args.summary)
                elif args.action == "worker":
                    if args.worker_action == "start":
                        pipeline.worker_start(
                            args.role, args.session_id, args.task, args.ticket
                        )
                    else:
                        pipeline.worker_finish(
                            args.session_id, args.status, args.summary
                        )
                elif args.action == "check":
                    pipeline.check(args.command, args.timeout)
                elif args.action == "deploy":
                    pipeline.deploy(
                        args.command,
                        args.smoke_command,
                        args.environment,
                        args.url,
                        args.timeout,
                        args.retry,
                    )
                elif args.action == "verify-deploy":
                    pipeline.verify_deploy(args.timeout)
            print(json.dumps(pipeline.state, indent=2))
    except (Blocked, OSError, ValueError, KeyError) as exc:
        print(f"slopteam: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
