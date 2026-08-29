#!/usr/bin/env python3
"""
The single command Claude runs to actually carry out the memory-guard
remove/stash resolution. Deliberately does the whole thing itself --
recompute the live dirty watched paths, apply the action, mark each path
resolved -- rather than leaving Claude to hand-write a `git stash` command
and a separate mark_resolved.py call, since that split let a resolution get
recorded as done ("resolved") without the underlying git command ever
actually running (seen in real session state: paths marked action="stash"
that were still showing modified in `git status` afterward).

Usage:
  python3 apply_action.py --repo-root <path> --action remove|stash --session-id <id>

Always recomputes the dirty watched list live via `git status` at call
time -- never trusts anything recorded earlier, so nothing stale or
already-handled gets swept in, and nothing outside the watched paths is
ever touched.
"""

import argparse
import subprocess
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

from memory_guard_common import live_dirty_watched_paths, mark_resolved  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--action", required=True, choices=["remove", "stash"])
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()

    paths = live_dirty_watched_paths(args.repo_root)
    if not paths:
        print("[memory-guard] nothing currently dirty under watched paths -- nothing to do")
        return

    if args.action == "remove":
        removed = []
        for rel in paths:
            target = Path(args.repo_root) / rel
            try:
                target.unlink()
                removed.append(rel)
            except FileNotFoundError:
                pass
            except IsADirectoryError:
                print(f"[memory-guard] skipped {rel!r}: is a directory, not a file", file=sys.stderr)
                continue
            mark_resolved(args.session_id, rel, "remove")
        print(f"[memory-guard] removed {len(removed)} path(s):")
        for rel in removed:
            print(f"  - {rel}")
        return

    result = subprocess.run(
        ["git", "-C", args.repo_root, "stash", "push", "-u", "--", *paths],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[memory-guard] git stash push failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    for rel in paths:
        mark_resolved(args.session_id, rel, "stash")
    print(f"[memory-guard] stashed {len(paths)} path(s):")
    for rel in paths:
        print(f"  - {rel}")


if __name__ == "__main__":
    main()
