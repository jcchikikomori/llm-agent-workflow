"""
Watchdog lifecycle seen from the outside: is it alive, start it, and the
dead-man switch.

Liveness is the watchdog's flock on watchdog.lock, not its heartbeat: under a
real thrash the watchdog can stall for a minute and still be fine, and a
heartbeat rule would make every hook resume everything at exactly the wrong
moment. Only "the lock can be taken" means dead.

When it is dead, the caller starts a new one, which resumes whatever is
still frozen as it starts. Only when no new watchdog can be started (spawn
throttle, crash loop) does the caller resume everything itself, within a
short deadline: hooks have hard timeouts, and what is left stays recorded
for the next hook.
"""

from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import sys
import time

import rg_actions as actions
import rg_common as common
import rg_docker as docker_mod

SPAWN_SPACING = 30.0
CRASH_WINDOW = 600.0
CRASH_LIMIT = 3
HUNG_AFTER = 120.0
HOOK_RESUME_BUDGET = 6.0
HOOK_STOP_GRACE = 2
LOG_MAX_BYTES = 1024 * 1024


def lock_path():
    return common.state_dir() / "watchdog.lock"


def log_path():
    return common.state_dir() / "watchdog.log"


def spawns_path():
    return common.state_dir() / "spawns.json"


def alive() -> bool:
    fd = os.open(str(lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return True
        raise
    finally:
        os.close(fd)
    return False


def heartbeat_age(now: float | None = None) -> float | None:
    status = common.read_json(common.state_dir() / "status.json", None)
    if not isinstance(status, dict) or "ts" not in status:
        return None
    return (now if now is not None else time.time()) - status["ts"]


def hung(now: float | None = None) -> bool:
    """Alive but silent for HUNG_AFTER. The watchdog touches its lock file as
    it starts, so a status.json left by a previous one doesn't count."""
    now = time.time() if now is None else now
    age = heartbeat_age(now)
    if age is None or age <= HUNG_AFTER or not alive():
        return False
    try:
        return now - os.stat(lock_path()).st_mtime > HUNG_AFTER
    except OSError:
        return True


def clean_env(env: dict) -> dict:
    """The watchdog must not look like Bash work of any session: drop every
    CLAUDE_* variable (CLAUDE_PID above all) before it starts."""
    return {key: value for key, value in env.items() if not key.startswith("CLAUDE_")}


def spawn(popen=subprocess.Popen) -> None:
    log = log_path()
    if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
        os.replace(log, log.with_name(log.name + ".1"))
    fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        popen(
            [sys.executable, str(common.plugin_root() / "scripts" / "watchdog.py")],
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            cwd="/",
            env=clean_env(os.environ),
            umask=0o077,
        )
    finally:
        os.close(fd)


def ensure_watchdog(now: float | None = None, popen=subprocess.Popen, force: bool = False) -> str:
    """'running', 'hung', 'spawned', 'throttled' or 'gave_up'. Safe to call
    from every hook: when the watchdog is alive it costs one flock probe, and
    it never raises on lock contention."""
    if alive():
        return "hung" if hung(now) else "running"
    now = time.time() if now is None else now
    state = _spawn_unless_throttled(now, popen, force)
    if state != "spawned" and actions.frozen_keys():
        try:
            actions.resume_all(docker=docker_mod.client(), reason="watchdog-dead", stop_grace=HOOK_STOP_GRACE,
                               deadline=time.time() + HOOK_RESUME_BUDGET)
        except common.LockTimeout:
            pass
    return state


def _spawn_unless_throttled(now: float, popen, force: bool) -> str:
    try:
        with common.locked(common.state_dir() / "spawn.lock", timeout=2.0):
            if alive():
                return "running"
            spawns = [t for t in common.read_json(spawns_path(), []) or [] if isinstance(t, (int, float))]
            spawns = [t for t in spawns if now - t < CRASH_WINDOW]
            if not force:
                if len(spawns) >= CRASH_LIMIT:
                    return "gave_up"
                if spawns and now - spawns[-1] < SPAWN_SPACING:
                    return "throttled"
            spawns.append(now)
            common.atomic_write_json(spawns_path(), spawns)
            spawn(popen)
    except common.LockTimeout:
        return "throttled"  # another hook is starting it right now
    return "spawned"


def clear_spawn_history() -> None:
    """Called by the watchdog on a clean exit, so ordinary idle exits never
    add up to a 'crash loop'."""
    try:
        spawns_path().unlink()
    except OSError:
        pass


def post_request(kind: str, key: str) -> None:
    common.atomic_write_json(common.state_dir() / "requests" / f"{time.time():.6f}-{kind}-{key}.json",
                             {"kind": kind, "key": key, "ts": time.time()})
