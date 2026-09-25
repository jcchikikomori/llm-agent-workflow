#!/usr/bin/env python3
"""
Shared helpers for the memory-guard plugin's hooks.

Path matching: which files under a repo count as "watched" (.claude/**,
root CLAUDE.md, docs/ticket-tracking/**), loaded from a small editable
config file so the user can adjust the list without touching Python.

Runtime: MEMORY_GUARD_RUNTIME, read once at import, picks the state dir and
the watched doc name. The opencode port prefixes every command it injects
with MEMORY_GUARD_RUNTIME=opencode, which selects
~/.config/opencode/.memory-guard/ and maps CLAUDE.md to AGENTS.md in the
loaded watched files (the config file itself still lists CLAUDE.md). Unset,
or any other value, keeps the Claude Code defaults: ~/.claude/.memory-guard/
and CLAUDE.md. The runtime is never guessed from which config dirs exist.

Session state: a per-session JSON file under STATE_DIR tracks which watched
paths have already been flagged this session, so the PostToolUse hook
doesn't re-prompt on every single edit to the same file. Guarded by flock
since parallel tool calls in one turn can run concurrently.

Project preference: a separate, session-independent file under
STATE_DIR/project-prefs/ records the "remove" or "stash" choice for a given
repo the first (and only) time it's asked, so later sessions never ask
again for that project. Deliberately stored outside the repo (never under
its .claude/) so writing it can't itself trigger a watched-path flag.
"""

import fcntl
import hashlib
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path

RUNTIME_ENV = "MEMORY_GUARD_RUNTIME"
OPENCODE_RUNTIME = "opencode"
IS_OPENCODE = os.environ.get(RUNTIME_ENV) == OPENCODE_RUNTIME

CLAUDE_DOC_NAME = "CLAUDE.md"
OPENCODE_DOC_NAME = "AGENTS.md"

if IS_OPENCODE:
    STATE_DIR = Path.home() / ".config" / "opencode" / ".memory-guard"
else:
    STATE_DIR = Path.home() / ".claude" / ".memory-guard"
PROJECT_PREFS_DIR = STATE_DIR / "project-prefs"

DEFAULT_WATCHED_DIRS = [".claude", "docs/ticket-tracking"]
DEFAULT_WATCHED_FILES = [CLAUDE_DOC_NAME]

_SESSION_KEY_PATTERN = re.compile(r"[^A-Za-z0-9._-]")

# Opportunistic cleanup: on ~10% of invocations, delete session state files
# older than this many days, so STATE_DIR doesn't grow forever.
GC_PROBABILITY = 0.1
GC_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def plugin_root() -> Path:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        return Path(root)
    # Fallback for manual/dry-run invocation outside the plugin runtime.
    return Path(__file__).resolve().parent.parent


def load_watched_patterns() -> "tuple[list[str], list[str]]":
    """Returns (watched_dirs, watched_files) for this runtime. Falls back to
    defaults on any missing/malformed config so a bad edit never disables
    the whole plugin."""
    dirs, files = _configured_watched_patterns()
    return dirs, _runtime_doc_names(files)


def _configured_watched_patterns() -> "tuple[list[str], list[str]]":
    config_path = plugin_root() / "config" / "watched-paths.json"
    try:
        data = json.loads(config_path.read_text())
        if not isinstance(data, dict):
            # Valid JSON that is not an object ([], null, "x"): defaults, as in the opencode port.
            raise ValueError("watched-paths.json must hold a JSON object")
        dirs = data.get("watched_dirs")
        files = data.get("watched_files")
        if not isinstance(dirs, list) or not isinstance(files, list):
            raise ValueError("watched_dirs/watched_files must be lists")
        if not all(isinstance(d, str) for d in dirs) or not all(isinstance(f, str) for f in files):
            raise ValueError("watched_dirs/watched_files entries must be strings")
        return dirs, files
    except (OSError, ValueError, json.JSONDecodeError):
        return DEFAULT_WATCHED_DIRS, DEFAULT_WATCHED_FILES


def _runtime_doc_names(files: "list[str]") -> "list[str]":
    """On opencode the root doc is AGENTS.md: CLAUDE.md maps to it, as in the
    opencode port. Claude Code keeps the configured names."""
    if not IS_OPENCODE:
        return files
    return [OPENCODE_DOC_NAME if f == CLAUDE_DOC_NAME else f for f in files]


def repo_root_for(cwd: str) -> "str | None":
    """Resolve the git repo root for cwd via `git rev-parse --show-toplevel`.
    Deliberately does NOT trust $CLAUDE_PROJECT_DIR — that's where Claude Code
    was launched from, which isn't guaranteed to be the git root for a
    monorepo subdirectory, worktree, or nested submodule."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def relpath_or_none(file_path: str, cwd: str, repo_root: str) -> "str | None":
    """Normalize file_path to a path relative to repo_root, using forward
    slashes. Returns None if the path resolves outside repo_root."""
    if not file_path:
        return None
    abs_path = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
    abs_path = os.path.realpath(abs_path)
    rel = os.path.relpath(abs_path, os.path.realpath(repo_root))
    rel = rel.replace(os.sep, "/")
    if rel.startswith(".."):
        return None
    return rel


def is_watched(rel_path: str, watched_dirs=None, watched_files=None) -> bool:
    if watched_dirs is None or watched_files is None:
        watched_dirs, watched_files = load_watched_patterns()
    if rel_path in watched_files:
        return True
    for d in watched_dirs:
        d = d.strip("/")
        if rel_path == d or rel_path.startswith(d + "/"):
            return True
    return False


def watched_pathspecs() -> "list[str]":
    """The pathspec list to pass to `git status`/`git stash` -- --
    (bare directory pathspecs already recurse, no glob magic needed)."""
    dirs, files = load_watched_patterns()
    return [*dirs, *files]


def parse_porcelain_paths(output: str) -> "list[str]":
    """Extracts paths from `git status --porcelain` output, taking the new
    path for renames (`R  old -> new`) and stripping quoting git applies to
    paths containing special characters."""
    paths = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip()
        if entry.startswith('"') and entry.endswith('"'):
            entry = entry[1:-1]
        paths.append(entry)
    return paths


def live_dirty_watched_paths(repo_root: str) -> "list[str]":
    """Recomputes, right now, which watched paths are actually dirty in
    repo_root. Never trust a recorded/cached list for anything destructive
    (removal) or working-tree-mutating (stash) -- always call this
    immediately before acting.

    --untracked-files=all is required here: by default `git status
    --porcelain` collapses an entirely-untracked directory into one `??
    <dir>/` line instead of listing the files inside it, which would make
    an untracked-but-watched subdirectory (e.g. a brand-new
    .claude/agent-memory/<agent>/ folder) unresolvable -- there'd be no
    individual file path to remove or stash, only a directory, and
    unlinking/stashing a directory as if it were a file silently fails.
    This scan is always scoped to the small watched pathspecs, not the
    whole repo, so the usual "don't scan untracked files repo-wide" cost
    concern doesn't apply."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain", "--untracked-files=all",
             "--", *watched_pathspecs()],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    watched_dirs, watched_files = load_watched_patterns()
    return [
        p for p in parse_porcelain_paths(result.stdout)
        if is_watched(p, watched_dirs, watched_files)
    ]


# --- Session state -----------------------------------------------------

def _sanitize_session_id(session_id: str) -> str:
    return _SESSION_KEY_PATTERN.sub("_", str(session_id))[:128] or "unknown"


def state_file_path(session_id: str) -> Path:
    return STATE_DIR / f"session_{_sanitize_session_id(session_id)}.json"


def lock_file_path(session_id: str) -> Path:
    return STATE_DIR / f"session_{_sanitize_session_id(session_id)}.lock"


def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return {"paths": {}}


def _write_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state))


def with_locked_state(session_id: str, fn):
    """Run fn(state: dict) -> dict under an flock on the session's state
    file, persisting the returned dict. Returns fn's return value."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = lock_file_path(session_id)
    state_path = state_file_path(session_id)
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            state = _read_state(state_path)
            state, result = fn(state)
            _write_state(state_path, state)
            return result
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def mark_pending_if_new(session_id: str, rel_path: str) -> bool:
    """Marks rel_path as 'pending' if it has no entry yet for this session.
    Returns True if this call newly flagged it (caller should emit the
    instruction), False if it was already pending or resolved (stay silent)."""

    def _update(state: dict):
        paths = state.setdefault("paths", {})
        if rel_path in paths:
            return state, False
        paths[rel_path] = {"status": "pending", "action": None, "ts": time.time()}
        return state, True

    return with_locked_state(session_id, _update)


def mark_resolved(session_id: str, rel_path: str, action: str) -> None:
    def _update(state: dict):
        paths = state.setdefault("paths", {})
        paths[rel_path] = {"status": "resolved", "action": action, "ts": time.time()}
        return state, None

    with_locked_state(session_id, _update)


def reset_path(session_id: str, rel_path: str) -> None:
    def _update(state: dict):
        state.setdefault("paths", {}).pop(rel_path, None)
        return state, None

    with_locked_state(session_id, _update)


# --- Project preference (remove vs stash, asked once per project ever) --

def _project_key(repo_root: str) -> str:
    return hashlib.sha256(os.path.realpath(repo_root).encode()).hexdigest()[:16]


def project_pref_path(repo_root: str) -> Path:
    return PROJECT_PREFS_DIR / f"{_project_key(repo_root)}.json"


def read_project_preference(repo_root: str) -> "str | None":
    """Returns 'remove', 'stash', or None if no preference has been set yet
    for this repo."""
    path = project_pref_path(repo_root)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    action = data.get("action")
    return action if action in ("remove", "stash") else None


def write_project_preference(repo_root: str, action: str) -> None:
    if action not in ("remove", "stash"):
        raise ValueError(f"action must be 'remove' or 'stash', got {action!r}")
    PROJECT_PREFS_DIR.mkdir(parents=True, exist_ok=True)
    path = project_pref_path(repo_root)
    path.write_text(json.dumps({
        "repo_root": os.path.realpath(repo_root),
        "action": action,
        "set_at": time.time(),
    }))


def reset_project_preference(repo_root: str) -> None:
    path = project_pref_path(repo_root)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def maybe_gc_old_sessions() -> None:
    if random.random() > GC_PROBABILITY:
        return
    if not STATE_DIR.is_dir():
        return
    cutoff = time.time() - GC_MAX_AGE_SECONDS
    for entry in STATE_DIR.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass
