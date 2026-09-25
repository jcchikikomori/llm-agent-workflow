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

# memory_guard_common picks the state dir (and the watched doc name) from
# MEMORY_GUARD_RUNTIME; see its module docstring.
from memory_guard_common import reset_project_preference  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    args = parser.parse_args()

    reset_project_preference(args.repo_root)
    print(f"[memory-guard] project preference cleared for {args.repo_root}")


if __name__ == "__main__":
    main()
