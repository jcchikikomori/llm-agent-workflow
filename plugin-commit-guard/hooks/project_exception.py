#!/usr/bin/env python3
"""
Record, show or remove a project's commit-guard exception.

The exception is the user's one-time answer to "let Claude run signed commits
itself in this project when a GUI pinentry is available". It lives OUTSIDE the
repo, under ~/.claude/.commit-guard/exceptions/ (or $COMMIT_GUARD_STATE_DIR),
keyed by the project's real path -- so a cloned repo cannot ship its own
bypass, and nothing lands in git status.

Usage:
  project_exception.py --dir <project> --decision allow|delegate
  project_exception.py --dir <project> --show
  project_exception.py --dir <project> --remove
"""

import argparse
import datetime
import json
import os
import sys

import commit_guard_hook as guard


def project_root(path):
    """Normalise to the repo top level, the same key the hook looks up."""
    top = guard.run_git(["rev-parse", "--show-toplevel"], cwd=path)
    return os.path.realpath(top or path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", required=True, help="project directory")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--decision", choices=("allow", "delegate"))
    action.add_argument("--show", action="store_true")
    action.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.dir):
        print("commit-guard: not a directory: " + args.dir, file=sys.stderr)
        return 64
    project = project_root(args.dir)
    path = guard.exception_path(project)

    if args.show:
        print(guard.read_exception(project) or "unset")
        return 0
    if args.remove:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        print("commit-guard: exception removed for " + project)
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "project": project,
        "decision": args.decision,
        "reason": "gui-pinentry",
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, indent=2) + "\n")
    print("commit-guard: {0} recorded for {1} ({2})".format(args.decision, project, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
