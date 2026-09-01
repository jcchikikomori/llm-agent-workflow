#!/usr/bin/env python3
"""
Record that a project has been mined into the palace.

Called by Claude right after a successful mempalace_mine, so the SessionStart
hook stops asking. Writes the stamp only -- it never mines anything itself.

  python3 scripts/mark_mined.py                    # stamp the current project
  python3 scripts/mark_mined.py --root /path/repo   # stamp another project
  python3 scripts/mark_mined.py --show              # print the current stamp
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from mempalace_docker_common import (  # noqa: E402
    mine_reason,
    project_root,
    read_stamp,
    write_stamp,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a mempalace mine stamp for a project.")
    parser.add_argument("--root", help="project root (default: git toplevel of cwd, else cwd)")
    parser.add_argument("--show", action="store_true", help="print the stored stamp and exit")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else project_root()

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
