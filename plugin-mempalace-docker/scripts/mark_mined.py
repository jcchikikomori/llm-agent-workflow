#!/usr/bin/env python3
"""
Record that a project has been mined into the palace.

Called by Claude right after a successful mempalace_mine, so the SessionStart
hook stops asking. Writes the stamp only -- it never mines anything itself.

  python3 scripts/mark_mined.py                    # stamp the current project
  python3 scripts/mark_mined.py --root /path/repo   # stamp another project
  python3 scripts/mark_mined.py --show              # print the current stamp
  python3 scripts/mark_mined.py --report            # print the mine prompt, no stamp
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from mempalace_docker_common import (  # noqa: E402
    DEFAULT_MINE_TOOL,
    mine_reason,
    mine_report,
    project_root,
    read_stamp,
    write_stamp,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a mempalace mine stamp for a project.")
    parser.add_argument("--root", help="project root (default: git toplevel of cwd, else cwd)")
    parser.add_argument("--show", action="store_true", help="print the stored stamp and exit")
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the SessionStart mine prompt for the project (empty when up to date) and exit",
    )
    parser.add_argument(
        "--tool-name",
        default=DEFAULT_MINE_TOOL,
        help="MCP tool name to put in the --report text (another harness names it differently)",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else project_root()

    if args.report:
        text = mine_report(tool_name=args.tool_name, root=root)
        if text:
            print(text)
        return 0

    if args.show:
        stamp = read_stamp(root)
        if stamp is None:
            print(f"no stamp for {root}")
            return 1
        print(json.dumps(stamp, indent=2))
        reason = mine_reason(root)
        print(f"status: {'stale -- ' + reason if reason else 'up to date'}")
        return 0

    path = write_stamp(root)
    print(f"[mempalace-docker] marked mined: {root}")
    print(f"[mempalace-docker] stamp: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
