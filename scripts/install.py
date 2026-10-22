#!/usr/bin/env python3
"""Install the portable Omni agent and optional local slopteam command."""

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-only", action="store_true", help="Do not install the command shortcut"
    )
    args = parser.parse_args()
    omni = shutil.which("omni")
    if not omni:
        parser.error("omni must be installed and on PATH")
    destination = Path.home() / ".local/bin/slopteam"
    target = ROOT / "bin/slopteam"
    if not args.agent_only and (destination.exists() or destination.is_symlink()):
        if not destination.is_symlink() or destination.resolve() != target:
            parser.error(
                f"{destination} already exists and belongs to another installation"
            )
    subprocess.run([omni, "agent", "add", str(ROOT / "agent")], check=True)
    if not args.agent_only:
        destination.parent.mkdir(parents=True, exist_ok=True)
        target.chmod(target.stat().st_mode | 0o111)
        if not destination.is_symlink():
            destination.symlink_to(target)
        print(f"Command installed: {destination}")
    print(
        "Slopteam installed with Codex as its default harness. Select slopteam in Omni."
    )


if __name__ == "__main__":
    main()
