#!/usr/bin/env python3
"""Launch Slopteam with an explicit Codex default and bootstrap prompt."""

import argparse
import os
import runpy
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "agent/skills/slopteam"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "pipeline":
        sys.argv = [str(SKILL / "scripts/pipeline.py"), *sys.argv[2:]]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return
    parser = argparse.ArgumentParser(
        description="Slopteam: idea → tickets → code → test → deploy"
    )
    parser.add_argument("idea", nargs="?", help="Idea, or a continuation instruction")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--harness", default="codex")
    parser.add_argument(
        "--model", help="Optional model; otherwise use the configured Codex default"
    )
    parser.add_argument("--resume", metavar="SESSION_ID")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print launch arguments without starting Omni",
    )
    args = parser.parse_args()
    if not args.repo.is_dir():
        parser.error("--repo must be an existing directory")
    omni = shutil.which("omni")
    if not omni:
        parser.error("omni is not on PATH; install Omnigent first")
    prompt = (
        "Use the Slopteam workflow below for this session. Its helper is at "
        + str(SKILL / "scripts/pipeline.py")
        + ".\n\n"
        + (SKILL / "SKILL.md").read_text()
        + "\n\nUser request:\n"
        + (
            args.idea
            or "Inspect Slopteam status. Resume an active run; if none exists, ask for my idea."
        )
    )
    argv = [
        omni,
        "run",
        str(ROOT / "agent"),
        "--harness",
        args.harness,
        "--prompt",
        prompt,
    ]
    if args.model:
        argv += ["--model", args.model]
    if args.resume:
        argv += ["--resume", args.resume]
    if args.dry_run:
        import json

        print(json.dumps({"cwd": str(args.repo.resolve()), "argv": argv}, indent=2))
        return
    os.chdir(args.repo)
    os.execv(omni, argv)


if __name__ == "__main__":
    main()
