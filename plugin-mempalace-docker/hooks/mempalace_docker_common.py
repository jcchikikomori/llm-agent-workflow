#!/usr/bin/env python3
"""
Shared state helpers for the mempalace-docker plugin.

All state lives OUTSIDE any repo, under ~/.claude/.mempalace-docker/:

  projects/<hash>.json   per-project mine stamp {path, last_mined, head_sha}
  sessions/<id>          per-session debounce marker for the conflict warning
  conflicts-dismissed    written once the user has acknowledged the warning

Keeping stamps out of the project means a mined repo stays mined across
clones, worktrees and branch switches, and nothing ever shows up in
`git status`.
"""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

STATE_ROOT = Path(os.environ.get("MEMPALACE_DOCKER_STATE", Path.home() / ".claude" / ".mempalace-docker"))
PROJECTS_DIR = STATE_ROOT / "projects"
SESSIONS_DIR = STATE_ROOT / "sessions"
DISMISSED_MARKER = STATE_ROOT / "conflicts-dismissed"

DEFAULT_MAX_AGE_DAYS = 7
# Session markers are tiny; 30 days is plenty of runway before they are worth
# reaping, and reaping keeps the directory from growing without bound.
SESSION_GC_SECONDS = 30 * 24 * 3600


def plugin_root() -> Path:
    return Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))


def run(cmd, cwd=None):
    """Run a command, returning stripped stdout or None. Never raises."""
    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def project_root(cwd=None) -> Path:
    """Git toplevel if there is one, else the working directory."""
    cwd = Path(cwd or os.getcwd())
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=str(cwd))
    return Path(top) if top else cwd


def head_sha(root: Path):
    return run(["git", "rev-parse", "HEAD"], cwd=str(root))


def project_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def stamp_path(root: Path) -> Path:
    return PROJECTS_DIR / f"{project_key(root)}.json"


def read_stamp(root: Path):
    try:
        return json.loads(stamp_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_stamp(root: Path, sha=None) -> Path:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    path = stamp_path(root)
    payload = {
        "path": str(root),
        "last_mined": time.time(),
        "last_mined_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "head_sha": sha if sha is not None else head_sha(root),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def max_age_days() -> float:
    raw = os.environ.get("MEMPALACE_MINE_MAX_AGE_DAYS")
    if not raw:
        return DEFAULT_MAX_AGE_DAYS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


def mine_reason(root: Path):
    """Why this project needs mining, or None if it is up to date."""
    stamp = read_stamp(root)
    if stamp is None:
        return "never mined"

    sha = head_sha(root)
    recorded = stamp.get("head_sha")
    if sha and recorded and sha != recorded:
        return f"HEAD moved ({recorded[:8]} -> {sha[:8]})"

    last = stamp.get("last_mined")
    if not isinstance(last, (int, float)):
        return "stamp unreadable"

    age_days = (time.time() - last) / 86400.0
    limit = max_age_days()
    if limit > 0 and age_days > limit:
        return f"last mined {age_days:.1f} days ago (limit {limit:g})"

    return None


def session_marked(session_id: str) -> bool:
    if not session_id:
        return False
    return (SESSIONS_DIR / session_id).exists()


def mark_session(session_id: str) -> None:
    if not session_id:
        return
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (SESSIONS_DIR / session_id).touch()


def gc_sessions() -> None:
    """Drop session markers older than SESSION_GC_SECONDS. Best effort."""
    if not SESSIONS_DIR.is_dir():
        return
    cutoff = time.time() - SESSION_GC_SECONDS
    for entry in SESSIONS_DIR.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def conflicts_dismissed() -> bool:
    return DISMISSED_MARKER.exists()
