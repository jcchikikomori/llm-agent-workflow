#!/usr/bin/env python3
"""
Lower-level helper: marks a single path "resolved" in a session's state
file. `apply_action.py` calls this internally for every path it processes
-- the normal memory-guard flow never needs to call this script directly.
Kept as a standalone entry point for manual/debug use only (e.g. clearing
a stuck "pending" entry from an old session without re-running the action).

Usage:
  python3 mark_resolved.py --session-id <id> --path <relpath> --action remove|stash
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

from memory_guard_common import mark_resolved  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--action", required=True, choices=["remove", "stash"])
    args = parser.parse_args()

    mark_resolved(args.session_id, args.path, args.action)
    print(f"[memory-guard] marked {args.path!r} resolved ({args.action}) for session {args.session_id}")


if __name__ == "__main__":
    main()
