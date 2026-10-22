#!/usr/bin/env python3
"""
Shared state helpers for the mempalace-docker plugin.

All plugin state lives OUTSIDE any repo, under ~/.claude/.mempalace-docker/:

  projects/<hash>.json   per-project mine stamp {path, last_mined, head_sha}
  sessions/<id>          per-session debounce marker for the conflict warning
  conflicts-dismissed    written once the user has acknowledged the warning
  hub/state.json         what scripts/hub.sh mounted into the shared hub the
                         last time it created the container
  hub/token, hub/env     the hub's bearer token and docker --env-file (0600)

Keeping stamps out of the project means a mined repo stays mined across
clones, worktrees and branch switches, and nothing ever shows up in
`git status`.

The project registry is separate and tool-neutral: a directory of symlinks
under ${XDG_CONFIG_HOME:-~/.config}/mempalace-docker/projects/. Every target
is bind-mounted read-only into the hub at its identical host path, which is
why the mine prompt below names the host path rather than a container alias.
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
HUB_STATE_FILE = STATE_ROOT / "hub" / "state.json"

REGISTRY_DIR = Path(
    os.environ.get("MEMPALACE_HUB_PROJECTS_DIR")
    or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "mempalace-docker" / "projects"
)

# Plugin-bundled servers are namespaced: mcp__plugin_<plugin>_<server>__<tool>.
DEFAULT_MINE_TOOL = "mcp__plugin_mempalace-docker_mempalace__mempalace_mine"

DEFAULT_MAX_AGE_DAYS = 7
# Session markers are tiny; 30 days is plenty of runway before they are worth
# reaping, and reaping keeps the directory from growing without bound.
SESSION_GC_SECONDS = 30 * 24 * 3600
HUB_ENSURE_TIMEOUT = 20


def plugin_root() -> Path:
    return Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))


def hub_script() -> Path:
    return plugin_root() / "scripts" / "hub.sh"


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


# ------------------------------------------------------------- registry


def _real(path) -> Path:
    return Path(os.path.realpath(str(path)))


def _is_under(path: Path, parent: Path) -> bool:
    return path == parent or str(path).startswith(str(parent).rstrip("/") + "/")


def _dedupe_nested(paths) -> list:
    """Sorted targets with every path that sits inside a kept one dropped.

    Checks every kept path, not just the previous one: `/a/b-x` sorts between
    `/a/b` and `/a/b/c`.
    """
    kept = []
    for path in sorted(set(paths), key=str):
        if any(_is_under(path, parent) for parent in kept):
            continue
        kept.append(path)
    return kept


def registry_targets() -> list:
    """Resolved registry targets, de-duplicated like scripts/hub.sh does.

    Dangling entries are ignored here too; hub.sh is the one that reports
    them, once, when it builds the mounts.
    """
    if not REGISTRY_DIR.is_dir():
        return []
    targets = []
    for entry in REGISTRY_DIR.iterdir():
        resolved = _real(entry)
        if resolved.is_dir():
            targets.append(resolved)
    return _dedupe_nested(targets)


def covering_target(root: Path, targets):
    """The registered (or mounted) target that contains root, or None."""
    real_root = _real(root)
    for target in targets:
        if _is_under(real_root, _real(target)):
            return target
    return None


def auto_register_allowed(root: Path) -> bool:
    """Whether the SessionStart hook may register root on its own.

    Only a git checkout strictly inside $HOME and outside ~/.claude. A session
    started in $HOME itself (or anywhere above it) would otherwise mount the
    whole home directory, which is a choice for the user to make with
    `hub.sh register`, not something a hook should do quietly.
    """
    real_root = _real(root)
    home = _real(Path.home())
    if real_root == home or not _is_under(real_root, home):
        return False
    if _is_under(real_root, home / ".claude"):
        return False
    return run(["git", "rev-parse", "--show-toplevel"], cwd=str(real_root)) is not None


def register_project(root: Path):
    """Symlink root into the registry. Returns (link, created).

    created is False when the project was already registered or is covered by
    a registered parent. A name clash with a different target gets a short
    hash suffix so two `app` checkouts can coexist.
    """
    real_root = _real(root)
    covered = covering_target(real_root, registry_targets())
    if covered is not None:
        return covered, False

    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    link = REGISTRY_DIR / real_root.name
    if link.exists() or link.is_symlink():
        suffix = hashlib.sha256(str(real_root).encode("utf-8")).hexdigest()[:8]
        link = REGISTRY_DIR / f"{real_root.name}-{suffix}"
    link.symlink_to(real_root)
    return link, True


# ------------------------------------------------------------------ hub


def hub_state():
    try:
        data = json.loads(HUB_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def hub_mounted_targets() -> list:
    state = hub_state() or {}
    targets = state.get("mounted_targets")
    if not isinstance(targets, list):
        return []
    return [Path(t) for t in targets if isinstance(t, str) and t]


def hub_autostart_enabled() -> bool:
    raw = os.environ.get("MEMPALACE_HUB_AUTOSTART", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def ensure_hub(check: bool = True):
    """Run `hub.sh ensure` (no wait). Returns (ok, stderr). Never raises.

    The SessionStart hook must never block a session on a container start,
    so this bounds the call and treats every failure as "not now".
    """
    cmd = ["bash", str(hub_script()), "ensure"]
    if check:
        cmd.append("--check")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=HUB_ENSURE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return out.returncode == 0, out.stderr


def mine_report(tool_name: str = DEFAULT_MINE_TOOL, root=None) -> str:
    """The SessionStart mine prompt for this project, or "" when nothing is due.

    Three shapes, in order: the project is up to date (empty); the hub does
    not have the project mounted yet (ask for a restart, do not mine); the
    project is mounted (mine it by its host path).
    """
    root = Path(root) if root is not None else project_root()
    reason = mine_reason(root)
    if reason is None:
        return ""

    real_root = _real(root)
    mounted = covering_target(real_root, hub_mounted_targets())
    if mounted is None:
        registered = covering_target(real_root, registry_targets())
        how = (
            f"registered for the hub (via {registered})" if registered is not None
            else "not registered for the hub"
        )
        return (
            f"[mempalace-docker] Project not mined into the palace ({reason}):\n"
            f"      {root}\n"
            f"  It is {how}, but the running hub was started without it, so the hub "
            "cannot see that path yet.\n"
            "  Do NOT mine it now. Tell the user once that the hub needs a restart to "
            "mount it (the idle exit also applies it on its own):\n"
            f"      {hub_script()} restart\n"
            "  After that restart, the next session start raises this again with the "
            "mine instructions."
        )

    return (
        f"[mempalace-docker] Project not mined into the palace ({reason}):\n"
        f"      {root}\n"
        "  The hub mounts it read-only at that same path.\n"
        "  Mine it, then record the stamp so this stops being raised:\n"
        f"    1. call {tool_name} with the path `{real_root}` -- the absolute host "
        "path, exactly as written\n"
        f"    2. run: python3 {plugin_root() / 'scripts' / 'mark_mined.py'}\n"
        "  Do this in the background of whatever the user actually asked for; do "
        "not block their request on it, and do not mine twice in one session. If "
        "the mine fails, say so once and move on -- the first call after an idle "
        "stop is a cold start (the container boots and loads the embedding model), "
        "so a slow first call is expected rather than a hung hub."
    )


# ------------------------------------------------------------- sessions


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
