#!/usr/bin/env python3
"""
Deletes the stored remove/stash preference for a project, so the next
flagged change in that repo asks the one-time question again. Not called
automatically by any hook -- run this by hand (or have Claude run it) when
you want to change your earlier choice.

Usage:
  python3 reset_preference.py --repo-root <path>
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import memory_guard_common as _mgc  # noqa: E402

# Cross-platform state dir: prefer the OpenCode config dir when it exists,
# else fall back to the Claude Code dir that memory_guard_common.py defaults
# to. The state/preference file formats are identical in both, so the two
# runtimes can share state. (memory_guard_common resolves STATE_DIR /
# PROJECT_PREFS_DIR at call time, so overriding them here is sufficient.)
if (Path.home() / ".config" / "opencode").exists():
    _mgc.STATE_DIR = Path.home() / ".config" / "opencode" / ".memory-guard"
    _mgc.PROJECT_PREFS_DIR = _mgc.STATE_DIR / "project-prefs"

from memory_guard_common import reset_project_preference  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    args = parser.parse_args()

    reset_project_preference(args.repo_root)
    print(f"[memory-guard] project preference cleared for {args.repo_root}")


if __name__ == "__main__":
    main()
