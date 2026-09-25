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

# memory_guard_common picks the state dir (and the watched doc name) from
# MEMORY_GUARD_RUNTIME; see its module docstring.
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
