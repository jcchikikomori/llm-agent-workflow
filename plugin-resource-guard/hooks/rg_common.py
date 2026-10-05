"""
Shared helpers for resource-guard: paths, config, JSON state, locks, events.

Everything the hooks, the watchdog and the CLI share lives under one state
directory (~/.claude/.resource-guard, or $RESOURCE_GUARD_STATE_DIR). Each
root the code reads (/proc, /sys, ~/.claude) has an env override so tests can
point it at a fixture tree instead of the live machine.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import fcntl
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Iterator

STATE_ENV = "RESOURCE_GUARD_STATE_DIR"
CONFIG_ENV = "RESOURCE_GUARD_CONFIG"
DISABLE_ENV = "RESOURCE_GUARD_DISABLE"
PROC_ENV = "RESOURCE_GUARD_PROC_ROOT"
SYS_ENV = "RESOURCE_GUARD_SYS_ROOT"
CLAUDE_HOME_ENV = "RESOURCE_GUARD_CLAUDE_HOME"

LEVELS = ("ok", "elevated", "critical", "hard")

EVENTS_MAX_BYTES = 256 * 1024


class LockTimeout(Exception):
    """Raised when a state lock can't be taken within its time bound."""


def plugin_root() -> Path:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        return Path(root)
    # Fallback for the CLI and the watchdog, which run outside a hook.
    return Path(__file__).resolve().parent.parent


def claude_home() -> Path:
    override = os.environ.get(CLAUDE_HOME_ENV)
    return Path(override) if override else Path.home() / ".claude"


def proc_root() -> Path:
    return Path(os.environ.get(PROC_ENV) or "/proc")


def sys_root() -> Path:
    return Path(os.environ.get(SYS_ENV) or "/sys")


def state_dir() -> Path:
    override = os.environ.get(STATE_ENV)
    path = Path(override) if override else claude_home() / ".resource-guard"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def level_index(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def deep_merge(base: dict, override: dict) -> dict:
    """Return base with override merged in; nested dicts merge, the rest
    (lists included) is replaced, so a user list never half-appends."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def user_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    return Path(override) if override else claude_home() / "resource-guard.json"


def load_config() -> dict:
    """Bundled defaults merged with the user's file. A missing or broken user
    file falls back to the defaults, so a bad edit never disables the guard
    by accident (the kill switch is explicit: enabled=false)."""
    defaults = read_json(plugin_root() / "config" / "defaults.json", {})
    user = read_json(user_config_path(), {})
    if not isinstance(user, dict):
        user = {}
    return deep_merge(defaults, user)


def disabled(cfg: dict) -> bool:
    return os.environ.get(DISABLE_ENV) == "1" or not cfg.get("enabled", True)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=1, sort_keys=True))


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file in the same directory and rename over the target,
    so a reader never sees a half-written file. The temp file is created
    fresh (O_EXCL, O_NOFOLLOW), never opened through a planted symlink."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked(path: Path, timeout: float | None = None, poll: float = 0.05) -> Iterator[None]:
    """Hold an exclusive flock on path. With a timeout, poll LOCK_NB and raise
    LockTimeout instead of blocking forever (a hook must stay inside its
    own time budget)."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeout(str(path)) from exc
                    time.sleep(poll)
        yield
    finally:
        os.close(fd)


def actions_lock() -> Path:
    return state_dir() / "actions.lock"


def events_path() -> Path:
    return state_dir() / "events.jsonl"


def append_event(kind: str, **fields: Any) -> dict:
    """Append one event line. Events never carry prompt or command text,
    only identifiers, counts and levels."""
    event = {"ts": time.time(), "kind": kind, **fields}
    path = events_path()
    try:
        if path.exists() and path.stat().st_size > EVENTS_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        pass
    return event


def read_events(since: float = 0.0) -> list:
    events = []
    try:
        lines = events_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("ts", 0) > since:
            events.append(event)
    return events


def session_key(pid: int, start: int | str) -> str:
    return f"{int(pid)}-{int(start)}"


def parse_key(key: str) -> tuple:
    pid, _, start = key.partition("-")
    return int(pid), int(start)
