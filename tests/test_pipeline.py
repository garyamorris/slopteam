import contextlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent/skills/slopteam/scripts/pipeline.py"
spec = importlib.util.spec_from_file_location("pipeline", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Pipeline, Blocked = module.Pipeline, module.Blocked


def plan(dependencies=False):
    tickets = [
        {
            "id": "T1",
            "title": "Ship greeting",
            "body": "Print a greeting",
            "acceptance": ["Prints hello"],
            "depends_on": [],
        }
    ]
    if dependencies:
        tickets.append(
            {
                "id": "T2",
                "title": "Integrate",
                "body": "Use the greeting",
                "acceptance": ["Integration succeeds"],
                "depends_on": ["T1"],
            }
        )
    return {"tickets": tickets}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "project with spaces"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Slopteam Test")
        (self.repo / "app.py").write_text('print("hello")\n')
        self.git("add", "app.py")
        self.git("commit", "-m", "Initial fixture")
        self.p = Pipeline(self.repo)
        self.p.init("Build a greeting")
        self.check_command = f"{shlex.quote(sys.executable)} app.py"

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    def ready(self):
        self.p.plan(plan())
        self.p.mark("T1", "in_progress", "")
        self.p.mark("T1", "done", "Prints hello; checked output")
        self.git("add", "docs/slopteam")
        self.git("commit", "-m", "Complete local ticket")
        self.p.check(self.check_command, 10)

    def deploy(self, command="true", smoke="true", retry=False):
        self.p.deploy(command, smoke, "test", "http://localhost:8000", 10, retry)

    def test_reloads_checkpoint_and_excludes_state_from_git(self):
        reloaded = Pipeline(self.repo)
        self.assertEqual(reloaded.load()["idea"], "Build a greeting")
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(Blocked, "unfinished"):
            reloaded.init("Another idea")

    def test_bad_dependency_graphs_are_rejected(self):
        variants = []
        duplicate = plan()
        duplicate["tickets"] *= 2
        variants.append(duplicate)
        unknown = plan()
        unknown["tickets"][0]["depends_on"] = ["T9"]
        variants.append(unknown)
        cyclic = plan(True)
        cyclic["tickets"][0]["depends_on"] = ["T2"]
        variants.append(cyclic)
        variants += [{"tickets": []}, {"tickets": [None]}, {"tickets": [{"id": "T1"}]}]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(Blocked):
                self.p.plan(value)
        self.assertEqual(self.p.state["tickets"], [])

    def test_dependency_order_and_acceptance_summary(self):
        self.p.plan(plan(True))
        with self.assertRaisesRegex(Blocked, "dependencies"):
            self.p.mark("T2", "in_progress", "")
        self.p.mark("T1", "in_progress", "")
        with self.assertRaisesRegex(Blocked, "summary"):
            self.p.mark("T1", "done", "")
        self.p.mark("T1", "done", "Works")
        self.p.mark("T2", "in_progress", "")
        self.assertEqual(self.p.ticket("T2")["status"], "in_progress")

    def test_cannot_skip_implementation(self):
        self.p.plan(plan())
        with self.assertRaises(Blocked):
            self.p.mark("T1", "done", "Skipped work")
        with self.assertRaisesRegex(Blocked, "Finish all"):
            self.deploy()

    def test_real_local_deploy_and_smoke(self):
        self.ready()
        deployed = Path(self.temp.name) / "release.txt"
        command = f"{self.check_command} > {shlex.quote(str(deployed))}"
        smoke_code = f'from pathlib import Path; assert Path({str(deployed)!r}).read_text().strip() == "hello"'
        smoke = shlex.quote(sys.executable) + " -c " + shlex.quote(smoke_code)
        self.deploy(command, smoke)
        self.assertEqual(self.p.state["deployment"]["status"], "verified")
        self.assertTrue(deployed.is_file())
        self.assertEqual(Pipeline(self.repo).load()["deployment"]["status"], "verified")
        with self.assertRaisesRegex(Blocked, "already deployed"):
            self.deploy()

    def test_dirty_and_committed_source_changes_invalidate_checks(self):
        self.ready()
        (self.repo / "app.py").write_text('print("changed")\n')
        with self.assertRaisesRegex(Blocked, "clean"):
            self.deploy()
        self.git("add", "app.py")
        self.git("commit", "-m", "Changed source")
        with self.assertRaisesRegex(Blocked, "current source"):
            self.deploy()
        self.p.check(self.check_command, 10)
        self.deploy()

    def test_untracked_files_change_fingerprint(self):
        initial = self.p.fingerprint()
        (self.repo / "new file.txt").write_text("new")
        self.assertNotEqual(initial, self.p.fingerprint())

    def test_failing_and_pending_checks_block_deploy(self):
        self.ready()
        with self.assertRaises(Blocked):
            self.p.check("exit 4", 10)
        with self.assertRaisesRegex(Blocked, "checks"):
            self.deploy()
        self.assertEqual(self.p.state["checks"]["exit 4"]["exit_code"], 4)
        self.p.state["checks"]["exit 4"]["exit_code"] = None
        with self.assertRaises(Blocked):
            self.deploy()

    def test_check_that_changes_source_does_not_count(self):
        self.ready()
        with self.assertRaisesRegex(Blocked, "changed source"):
            self.p.check("echo change >> app.py", 10)

    def test_timeout_records_failure(self):
        command = (
            shlex.quote(sys.executable)
            + " -c "
            + shlex.quote("import time; time.sleep(10)")
        )
        with self.assertRaises(Blocked):
            self.p.check(command, 0.05)
        self.assertEqual(self.p.state["checks"][command]["exit_code"], 124)

    def test_deploy_failure_requires_explicit_retry(self):
        self.ready()
        with self.assertRaisesRegex(Blocked, "command failed"):
            self.deploy("exit 7")
        self.assertEqual(self.p.state["deployment"]["status"], "failed")
        with self.assertRaisesRegex(Blocked, "already attempted"):
            self.deploy()
        self.deploy(retry=True)
        self.assertEqual(self.p.state["deployment"]["status"], "verified")
        self.assertEqual(len(self.p.state["deployment_history"]), 1)

    def test_smoke_failure_is_not_success_and_can_retry_verification(self):
        self.ready()
        release = Path(self.temp.name) / "ready"
        smoke = "test -f " + shlex.quote(str(release))
        with self.assertRaisesRegex(Blocked, "unverified"):
            self.deploy(smoke=smoke)
        self.assertEqual(self.p.state["deployment"]["status"], "smoke_failed")
        with self.assertRaises(Blocked):
            self.deploy(retry=True)
        release.touch()
        self.p.verify_deploy(10)
        self.assertEqual(self.p.state["deployment"]["status"], "verified")

    def test_interrupted_deployment_is_never_automatically_repeated(self):
        self.ready()
        with patch.object(
            self.p, "execute", side_effect=RuntimeError("process interrupted")
        ):
            with self.assertRaises(RuntimeError):
                self.deploy()
        self.assertEqual(Pipeline(self.repo).load()["deployment"]["status"], "running")
        with self.assertRaisesRegex(Blocked, "reconcile"):
            self.deploy(retry=True)

    def test_deployment_source_mutation_prevents_verified_status(self):
        self.ready()
        with self.assertRaisesRegex(Blocked, "clean"):
            self.deploy("echo changed >> app.py")
        self.assertNotEqual(self.p.state["deployment"]["status"], "verified")

    def test_lock_prevents_concurrent_mutation(self):
        with self.p.locked():
            with self.assertRaisesRegex(Blocked, "Another Slopteam"):
                with Pipeline(self.repo).locked():
                    pass

    def test_invalid_run_id_cannot_escape_directory(self):
        with self.assertRaisesRegex(Blocked, "Invalid run"):
            Pipeline(self.repo, "../elsewhere").load()

    def test_tickets_are_versioned_git_files(self):
        self.ready()
        ticket = self.p.ticket("T1")
        path = self.repo / ticket["path"]
        self.assertIn("Status: done", path.read_text())
        self.assertIn("Prints hello", path.read_text())
        self.assertEqual(self.git("ls-files", ticket["path"]), ticket["path"])
        self.assertNotIn("github", self.p.state)

    def team_built(self):
        self.p.worker_start("planner", "planner-1", "Plan release")
        self.p.plan(plan())
        self.git("add", "docs")
        self.git("commit", "-m", "Plan release")
        self.p.worker_finish("planner-1", "completed", "Ticket plan committed")
        self.p.worker_start("builder", "builder-1", "Build greeting", "T1")
        self.p.mark("T1", "in_progress", "")
        self.p.mark("T1", "done", "Greeting implemented")
        self.git("add", "docs")
        self.git("commit", "-m", "Implement greeting")
        self.p.worker_finish("builder-1", "completed", "Implementation committed")

    def team_tested(self):
        self.team_built()
        self.p.worker_start("tester", "tester-1", "Independent QA")
        self.p.check(self.check_command, 10)
        self.p.worker_finish("tester-1", "completed", "Acceptance and checks passed")

    def test_four_distinct_workers_complete_a_release(self):
        self.team_tested()
        self.p.worker_start("deployer", "deployer-1", "Deploy release")
        self.deploy()
        self.p.worker_finish("deployer-1", "completed", "Smoke verified")
        workers = Pipeline(self.repo).load()["workers"]
        self.assertEqual(
            [w["role"] for w in workers], ["planner", "builder", "tester", "deployer"]
        )
        self.assertEqual(len({w["session_id"] for w in workers}), 4)
        self.assertTrue(all(w["status"] == "completed" for w in workers))

    def test_team_rejects_reused_session_and_concurrent_workers(self):
        self.p.worker_start("planner", "plan-1", "Plan")
        with self.assertRaisesRegex(Blocked, "running worker"):
            self.p.worker_start("planner", "plan-2", "Duplicate plan")
        self.p.worker_finish("plan-1", "failed", "Could not plan")
        with self.assertRaisesRegex(Blocked, "distinct session"):
            self.p.worker_start("tester", "plan-1", "Pretend to be separate")

    def test_team_requires_stage_order_and_real_ticket_output(self):
        with self.assertRaisesRegex(Blocked, "planner must complete"):
            self.p.worker_start("builder", "build-1", "Build", "T1")
        self.p.worker_start("planner", "plan-1", "Plan")
        with self.assertRaisesRegex(Blocked, "not produced"):
            self.p.worker_finish("plan-1", "completed", "Claimed success")

    def test_team_deployment_requires_independent_qa_and_deployer(self):
        self.team_built()
        self.p.check(self.check_command, 10)
        with self.assertRaisesRegex(Blocked, "separate tester"):
            self.deploy()
        self.p.worker_start("tester", "tester-1", "QA")
        self.p.worker_finish("tester-1", "completed", "Verified current checks")
        with self.assertRaisesRegex(Blocked, "separate deployer"):
            self.deploy()

    def test_source_change_requires_fresh_independent_tester(self):
        self.team_tested()
        (self.repo / "app.py").write_text('print("new release")\n')
        self.git("add", "app.py")
        self.git("commit", "-m", "Change release after QA")
        self.p.check(self.check_command, 10)
        with self.assertRaisesRegex(Blocked, "independent tester"):
            self.p.worker_start("deployer", "deploy-1", "Deploy stale QA")

    def test_failed_qa_returns_to_new_builder_and_new_tester(self):
        self.team_built()
        self.p.worker_start("tester", "tester-1", "QA")
        self.p.worker_finish("tester-1", "failed", "Greeting did not meet acceptance")
        self.p.worker_start("builder", "builder-2", "Fix greeting", "T1")
        self.p.mark("T1", "in_progress", "Repair QA finding")
        (self.repo / "app.py").write_text('print("hello, fixed")\n')
        self.p.mark("T1", "done", "Greeting repaired")
        self.git("add", "app.py", "docs")
        self.git("commit", "-m", "Fix greeting")
        self.p.worker_finish("builder-2", "completed", "Repair committed")
        self.p.worker_start("tester", "tester-2", "Fresh QA")
        self.p.check(self.check_command, 10)
        self.p.worker_finish("tester-2", "completed", "Repair verified")
        self.p.worker_start("deployer", "deploy-1", "Deploy repaired release")
        self.deploy()
        self.assertEqual(self.p.state["deployment"]["status"], "verified")

    def test_failed_deployment_cannot_be_reported_as_completed_worker(self):
        self.team_tested()
        self.p.worker_start("deployer", "deploy-1", "Deploy")
        with self.assertRaisesRegex(Blocked, "not verified"):
            self.p.worker_finish("deploy-1", "completed", "False success")

    def test_cli_status_reads_existing_run(self):
        result = subprocess.run(
            [sys.executable, str(SOURCE), "--repo", str(self.repo), "status"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], self.p.run_id)

    def test_invalid_plan_cli_returns_actionable_error(self):
        path = self.p.directory / "plan.json"
        path.write_text("{bad json")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = module.main(["--repo", str(self.repo), "plan", "--file", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("slopteam:", stderr.getvalue())


class LauncherTest(unittest.TestCase):
    def test_default_codex_and_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "omni"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bin/slopteam"),
                    "--dry-run",
                    "--repo",
                    directory,
                    "Build a tracker",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"]},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(
                output["argv"][output["argv"].index("--harness") + 1], "codex"
            )
            self.assertIn(
                "Build a tracker", output["argv"][output["argv"].index("--prompt") + 1]
            )
            self.assertEqual(output["cwd"], str(Path(directory).resolve()))


if __name__ == "__main__":
    unittest.main()
