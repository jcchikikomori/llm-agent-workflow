#!/usr/bin/env python3
"""
Claude runs this exactly once per project, right after the user answers the
one-time "remove or stash" question, to persist the choice so future
sessions never ask again for this repo.

Usage:
  python3 set_preference.py --repo-root <path> --action remove|stash
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

# memory_guard_common picks the state dir (and the watched doc name) from
# MEMORY_GUARD_RUNTIME; see its module docstring.
from memory_guard_common import write_project_preference  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--action", required=True, choices=["remove", "stash"])
    args = parser.parse_args()

    write_project_preference(args.repo_root, args.action)
    print(f"[memory-guard] project preference set to {args.action!r} for {args.repo_root}")


if __name__ == "__main__":
    main()
