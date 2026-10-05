"""
Which Claude Code sessions are alive, and which one the user is typing in.

A session is keyed by (claude pid, procStart), never by session ID: /clear,
resume and fork mint a new session ID inside the same process.

Claude Code keeps ~/.claude/sessions/<pid>.json per live process (pid,
procStart, sessionId, kind, status, cwd). That file format is undocumented,
so it is read defensively, and a /proc scan backs it up when it is missing.

Foreground = the session with the most recent real prompt. The one it took
over from keeps a short grace period, so flipping between two terminals
doesn't freeze the session the user just left.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import rg_common as common
import rg_procs as procs_mod


@dataclass
class Session:
    pid: int
    start: int
    session_id: str = ""
    cwd: str = ""
    kind: str = "interactive"
    status: str = ""
    updated_at: float = 0.0
    last_prompt_at: float = 0.0

    @property
    def key(self) -> str:
        return common.session_key(self.pid, self.start)


def prompts_dir() -> Path:
    return common.state_dir() / "prompts"


def touch_prompt(key: str, ts: float | None = None) -> None:
    common.atomic_write_json(prompts_dir() / f"{key}.json", {"ts": ts if ts is not None else time.time()})


def prompt_times() -> dict:
    times = {}
    directory = prompts_dir()
    if not directory.is_dir():
        return times
    for path in directory.glob("*.json"):
        data = common.read_json(path, {})
        if isinstance(data, dict) and isinstance(data.get("ts"), (int, float)):
            times[path.stem] = float(data["ts"])
    return times


def _from_file(data: dict) -> Session | None:
    try:
        return Session(
            pid=int(data["pid"]),
            start=int(data["procStart"]),
            session_id=str(data.get("sessionId", "")),
            cwd=str(data.get("cwd", "")),
            kind=str(data.get("kind", "interactive")),
            status=str(data.get("status", "")),
            updated_at=float(data.get("updatedAt") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def cc_sessions(procs: dict | None = None, root: Path | None = None) -> list:
    """Live sessions. With a full process scan (`procs`), session processes
    that have no file are added, except nested ones started from another
    session's Bash tool (they carry CLAUDE_PID)."""
    found = {}
    directory = common.claude_home() / "sessions"
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        session = _from_file(common.read_json(path, {}) or {})
        if session is None:
            continue
        if procs is not None:
            live_start = procs[session.pid].start if session.pid in procs else None
        else:
            live_start = procs_mod.start_of(session.pid, root)
        if live_start == session.start:
            found[session.key] = session

    if procs is not None:
        for proc in procs.values():
            key = common.session_key(proc.pid, proc.start)
            if key in found or not procs_mod.is_session_process(proc):
                continue
            if "CLAUDE_PID" in procs_mod.read_environ(proc.pid, root):
                continue
            found[key] = Session(pid=proc.pid, start=proc.start, kind="unknown")

    times = prompt_times()
    for key, session in found.items():
        session.last_prompt_at = times.get(key, 0.0)
    return sorted(found.values(), key=lambda s: s.pid)


def foreground(sessions: list, now: float, grace: float) -> set:
    """Keys of the sessions the user is working in, which must not be frozen.

    With prompt history: the latest prompter, plus the previous one while the
    switch is younger than `grace`. Without any (fresh install): the most
    recently updated interactive session, so the guard never starts by
    freezing the terminal in front of the user."""
    prompted = sorted((s for s in sessions if s.last_prompt_at > 0), key=lambda s: s.last_prompt_at, reverse=True)
    if prompted:
        protected = {prompted[0].key}
        if len(prompted) > 1 and now - prompted[0].last_prompt_at < grace:
            protected.add(prompted[1].key)
        return protected
    interactive = sorted((s for s in sessions if s.kind == "interactive"), key=lambda s: s.updated_at, reverse=True)
    return {interactive[0].key} if interactive else set()


def own_session(env: dict | None = None, root: Path | None = None, parent_pid: int | None = None) -> tuple | None:
    """(pid, start) of the session a hook runs under: CLAUDE_PID when the
    environment has it, else the nearest claude ancestor of the hook."""
    env = os.environ if env is None else env
    raw = env.get("CLAUDE_PID", "")
    if raw.isdigit():
        start = procs_mod.start_of(int(raw), root)
        if start is not None:
            return int(raw), start
    ancestor = procs_mod.claude_ancestor(parent_pid if parent_pid is not None else os.getppid(), root)
    return (ancestor.pid, ancestor.start) if ancestor else None
