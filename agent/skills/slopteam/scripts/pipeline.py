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
    return dt.datetime.now(dt.UTC).isoformat()


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

    def init(self, idea):
        if not idea.strip():
            raise Blocked("The idea cannot be empty")
        if (self.directory / "active").exists():
            self.load()
            if self.state.get("deployment", {}).get("status") != "verified":
                raise Blocked("An unfinished run exists; use status and continue it")
        self.run_id = uuid.uuid4().hex[:12]
        self.state = {
            "schema": 1,
            "id": self.run_id,
            "idea": idea,
            "created_at": now(),
            "tickets": [],
            "checks": {},
            "deployment": {},
            "github": None,
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
        self.save()

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
        if allowed.get(ticket["status"]) != status:
            raise Blocked("Tickets advance from todo to in_progress to done")
        if any(self.ticket(dep)["status"] != "done" for dep in ticket["depends_on"]):
            raise Blocked("Finish dependencies first")
        if status == "done" and not summary.strip():
            raise Blocked(
                "Done needs --summary with implementation and acceptance evidence"
            )
        ticket.update(status=status, summary=summary, updated_at=now())
        if status == "done":
            ticket["head"] = call(["git", "rev-parse", "HEAD"], self.repo)
        self.save()

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

    def release_gate(self):
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

    def gh(self, args, body=None):
        output = call(
            ["gh", *args], self.repo, json.dumps(body) if body is not None else None
        )
        try:
            return json.loads(output)
        except ValueError as exc:
            raise Blocked(
                "GitHub returned an unreadable response; reconcile before retrying"
            ) from exc

    def marker(self, ticket):
        return f"<!-- slopteam:{self.run_id}:{ticket['id']} -->"

    def publish_tickets(self, github):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github):
            raise Blocked("GitHub repository must be OWNER/REPO")
        if not self.state["tickets"]:
            raise Blocked("Load a plan before publishing tickets")
        if self.state["github"] and self.state["github"] != github:
            raise Blocked("This run is already bound to another GitHub repository")
        self.state["github"] = github
        self.save()
        pages = self.gh(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{github}/issues?state=all&per_page=100",
            ]
        )
        issues = [i for page in pages for i in page if "pull_request" not in i]
        for ticket in self.state["tickets"]:
            if ticket.get("issue"):
                continue
            matches = [
                i for i in issues if self.marker(ticket) in (i.get("body") or "")
            ]
            if len(matches) > 1:
                raise Blocked(
                    f"Multiple issues match {ticket['id']}; reconcile explicitly"
                )
            if matches:
                ticket.update(
                    issue=matches[0]["number"],
                    issue_url=matches[0]["html_url"],
                    publishing=False,
                )
                self.save()
                continue
            if ticket.get("publishing"):
                raise Blocked(
                    f"{ticket['id']} creation outcome is uncertain; use reconcile-ticket"
                )
            ticket["publishing"] = True
            self.save()
            body = (
                ticket["body"]
                + "\n\nAcceptance criteria:\n"
                + "\n".join("- [ ] " + x for x in ticket["acceptance"])
            )
            body += (
                "\n\nDependencies: "
                + (", ".join(ticket["depends_on"]) or "none")
                + "\n\n"
                + self.marker(ticket)
            )
            issue = self.gh(
                ["api", "--method", "POST", f"repos/{github}/issues", "--input", "-"],
                {"title": f"[{ticket['id']}] {ticket['title']}", "body": body},
            )
            ticket.update(
                issue=issue["number"], issue_url=issue["html_url"], publishing=False
            )
            self.save()

    def reconcile_ticket(self, tid, issue_number, not_created):
        ticket = self.ticket(tid)
        if ticket.get("issue"):
            raise Blocked("This ticket already has a linked issue")
        if issue_number:
            github = self.state["github"]
            if not github:
                raise Blocked("Publish tickets first to bind the GitHub repository")
            issue = self.gh(["api", f"repos/{github}/issues/{issue_number}"])
            if "pull_request" in issue or self.marker(ticket) not in (
                issue.get("body") or ""
            ):
                raise Blocked(
                    "The issue does not contain this ticket’s exact run marker"
                )
            ticket.update(issue=issue["number"], issue_url=issue["html_url"])
        elif not not_created:
            raise Blocked("Choose --issue NUMBER or --not-created")
        ticket["publishing"] = False
        self.save()

    def close_tickets(self):
        fingerprint = self.release_gate()
        deployment = self.state["deployment"]
        if (
            deployment.get("status") != "verified"
            or deployment.get("fingerprint") != fingerprint
        ):
            raise Blocked("Verify the current release before closing issues")
        github = self.state["github"]
        if not github:
            raise Blocked("No GitHub tickets were published")
        for ticket in self.state["tickets"]:
            if ticket.get("issue") and not ticket.get("closed"):
                self.gh(
                    [
                        "api",
                        "--method",
                        "PATCH",
                        f"repos/{github}/issues/{ticket['issue']}",
                        "--input",
                        "-",
                    ],
                    {"state": "closed", "state_reason": "completed"},
                )
                ticket["closed"] = True
                self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--run", help="Existing run ID; otherwise use the active run")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("init", help="Start a run")
    p.add_argument("--idea", required=True)
    sub.add_parser("status", help="Show checkpoint JSON")
    p = sub.add_parser("plan", help="Load a validated ticket dependency graph")
    p.add_argument("--file", type=Path, required=True)
    p = sub.add_parser("ticket", help="Advance one ticket")
    p.add_argument("id")
    p.add_argument("--status", choices=["in_progress", "done"], required=True)
    p.add_argument("--summary", default="")
    p = sub.add_parser("tickets", help="Publish/reconcile GitHub issues")
    p.add_argument("--github", required=True)
    p = sub.add_parser("reconcile-ticket", help="Resolve an uncertain issue creation")
    p.add_argument("id")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--issue", type=int)
    group.add_argument("--not-created", action="store_true")
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
    sub.add_parser("close-tickets", help="Close issues after verified deployment")
    args = parser.parse_args(argv)
    try:
        with Pipeline(args.repo, args.run).locked() as pipeline:
            if args.action == "init":
                pipeline.init(args.idea)
            else:
                pipeline.load()
                if args.action == "plan":
                    pipeline.plan(json.loads(args.file.read_text()))
                elif args.action == "ticket":
                    pipeline.mark(args.id, args.status, args.summary)
                elif args.action == "tickets":
                    pipeline.publish_tickets(args.github)
                elif args.action == "reconcile-ticket":
                    pipeline.reconcile_ticket(args.id, args.issue, args.not_created)
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
                elif args.action == "close-tickets":
                    pipeline.close_tickets()
            print(json.dumps(pipeline.state, indent=2))
    except (Blocked, OSError, ValueError, KeyError) as exc:
        print(f"slopteam: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
