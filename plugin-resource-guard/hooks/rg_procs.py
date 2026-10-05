"""
/proc scanning: who is a Claude Code session, which processes are its Bash
work, and signalling a process only if it is still the one we looked at.

Bash-tool shells (and everything they start, reparented `nohup ... &` jobs
included) inherit CLAUDE_PID=<session pid> in their environment. MCP and LSP
servers don't, which is what keeps the freeze away from them.

A PID alone is never trusted: every signal re-reads the start time (field 22
of /proc/<pid>/stat, in clock ticks) and compares it to the one recorded, then
sends through a pidfd where the platform has one, so a reused PID can't be
hit.
"""

from __future__ import annotations

import fnmatch
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path

import rg_common as common

PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4

# Helper processes of the claude binary that are not sessions themselves.
NON_SESSION_ARGS = ("daemon", "bg-pty-host", "--bg-pty-host", "bg-spare", "--bg-spare")

# Processes whose command line runs a plugin hook script. Freezing one would
# stall the session that is waiting on the hook.
HOOK_MARKERS = ("/hooks/",)

# Claude Code sets these for hooks (and whatever a hook starts), never for
# Bash-tool shells: the reliable way to tell a hook from work, wherever the
# hook script lives (--plugin-dir checkouts, settings.json hooks).
HOOK_ENV_KEYS = ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR")


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    state: str
    start: int
    rss_kb: int = 0
    sid: int = 0
    uid: int | None = None
    cmdline: list = field(default_factory=list)
    exe: str = ""


def parse_stat(text: str) -> dict | None:
    """Parse /proc/<pid>/stat. comm sits in parentheses and may contain
    spaces or ')' itself, so split after the LAST ')'."""
    left, right = text.find("("), text.rfind(")")
    if left < 0 or right < left:
        return None
    fields = text[right + 2:].split()
    if len(fields) < 20:
        return None
    try:
        return {
            "comm": text[left + 1:right],
            "state": fields[0],
            "ppid": int(fields[1]),
            "sid": int(fields[3]),
            "start": int(fields[19]),
            "rss_kb": int(fields[21]) * PAGE_KB if len(fields) > 21 else 0,
        }
    except ValueError:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _read_uid(pid_dir: Path) -> int | None:
    text = _read_text(pid_dir / "status") or ""
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return None


def _read_cmdline(pid_dir: Path) -> list:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _read_exe(pid_dir: Path) -> str:
    try:
        return os.readlink(pid_dir / "exe")
    except OSError:
        return ""


def read_proc(pid: int, root: Path | None = None) -> Proc | None:
    root = Path(root) if root is not None else common.proc_root()
    pid_dir = root / str(pid)
    stat = parse_stat(_read_text(pid_dir / "stat") or "")
    if stat is None:
        return None
    return Proc(
        pid=int(pid),
        ppid=stat["ppid"],
        comm=stat["comm"],
        state=stat["state"],
        start=stat["start"],
        rss_kb=stat["rss_kb"],
        sid=stat["sid"],
        uid=_read_uid(pid_dir),
        cmdline=_read_cmdline(pid_dir),
        exe=_read_exe(pid_dir),
    )


def scan(root: Path | None = None) -> dict:
    """Every readable process as {pid: Proc}. Processes that exit mid-scan
    are skipped, not errors."""
    root = Path(root) if root is not None else common.proc_root()
    procs = {}
    try:
        entries = os.listdir(root)
    except OSError:
        return procs
    for name in entries:
        if name.isdigit():
            proc = read_proc(int(name), root)
            if proc is not None:
                procs[proc.pid] = proc
    return procs


def read_environ(pid: int, root: Path | None = None) -> dict:
    root = Path(root) if root is not None else common.proc_root()
    try:
        raw = (root / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for part in raw.split(b"\0"):
        key, sep, value = part.partition(b"=")
        if sep:
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def start_of(pid: int, root: Path | None = None) -> int | None:
    root = Path(root) if root is not None else common.proc_root()
    stat = parse_stat(_read_text(root / str(pid) / "stat") or "")
    return stat["start"] if stat else None


def is_claude_exe(proc: Proc, execpath: str | None = None) -> bool:
    """True for any process running the Claude Code binary: native installs
    run .../claude/versions/<ver>, npm installs run node with the CLI script."""
    execpath = execpath if execpath is not None else os.environ.get("CLAUDE_CODE_EXECPATH", "")
    if proc.exe and (proc.exe == execpath or "/claude/versions/" in proc.exe):
        return True
    if proc.cmdline:
        if os.path.basename(proc.cmdline[0]) == "claude" or "/claude/versions/" in proc.cmdline[0]:
            return True
        if os.path.basename(proc.cmdline[0]).startswith("node"):
            return any("claude-code/cli" in arg for arg in proc.cmdline[1:3])
    return False


def is_session_process(proc: Proc) -> bool:
    if not is_claude_exe(proc):
        return False
    return not any(arg in NON_SESSION_ARGS for arg in proc.cmdline[1:3])


def claude_ancestor(pid: int, root: Path | None = None, max_depth: int = 8) -> Proc | None:
    """Walk up from pid to the nearest Claude Code session process, reading
    only the processes on the chain (a hook can't afford a full scan)."""
    current = read_proc(pid, root)
    for _ in range(max_depth):
        if current is None or current.pid <= 1:
            return None
        if is_session_process(current):
            return current
        current = read_proc(current.ppid, root)
    return None


def self_and_ancestors(root: Path | None = None, max_depth: int = 64) -> tuple:
    """This process and every ancestor. The CLI runs inside a Bash-tool
    shell that carries CLAUDE_PID, so without this it would count its own
    parent shell as freezable work and stop the command that called it."""
    out, pid = [], os.getpid()
    while pid > 1 and pid not in out and len(out) < max_depth:
        out.append(pid)
        proc = read_proc(pid, root)
        if proc is None:
            break
        pid = proc.ppid
    return tuple(out)


def _matches_any(name: str, patterns: list) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _excluded(proc: Proc, never_freeze: list) -> bool:
    if is_claude_exe(proc):
        return True
    names = {proc.comm}
    if proc.cmdline:
        names.add(os.path.basename(proc.cmdline[0]))
    if any(_matches_any(name, never_freeze) for name in names):
        return True
    return any(marker in arg and "/plugins/" in arg for arg in proc.cmdline[:3] for marker in HOOK_MARKERS)


def _depth(pid: int, procs: dict) -> int:
    depth, seen = 0, set()
    while pid in procs and pid not in seen and depth < 64:
        seen.add(pid)
        pid = procs[pid].ppid
        depth += 1
    return depth


def work_pids(procs: dict, session_pid: int, cfg: dict, root: Path | None = None,
              self_pids: tuple = (), uid: int | None = None, session_start: int | None = None) -> list:
    """PIDs of the session's Bash-tool work, parents before children.

    A process counts when its environ has CLAUDE_PID=<session_pid> and it is
    ours. It is dropped, together with everything under it, when it or any
    ancestor is a claude binary (teammates, nested `claude -p`), a hook, a
    never-freeze command (git, ssh, gpg, pinentry: they hold locks other
    sessions wait on), or a detached daemon: a session leader re-parented
    away from the session (`dbus-daemon --fork` and friends). Those are
    long-lived services that merely inherited the variable, not work.

    With `session_start`, a process older than the session is dropped too:
    it belongs to an earlier session that had the same PID."""
    uid = os.getuid() if uid is None else uid
    never_freeze = cfg.get("never_freeze_commands", [])
    wanted = str(session_pid)
    candidates, hooks = {}, set()
    for pid, proc in procs.items():
        if pid <= 1 or pid == session_pid or pid in self_pids or proc.uid != uid:
            continue
        if session_start is not None and proc.start < session_start:
            continue
        env = read_environ(pid, root)
        if env.get("CLAUDE_PID") == wanted:
            candidates[pid] = proc
            if any(key in env for key in HOOK_ENV_KEYS):
                hooks.add(pid)

    verdicts = {}

    def excluded(pid: int) -> bool:
        if pid in verdicts:
            return verdicts[pid]
        chain, cur, result = [], pid, False
        while cur in candidates and cur not in verdicts and cur not in chain:
            chain.append(cur)
            proc = candidates[cur]
            detached = proc.sid == proc.pid and proc.ppid != session_pid and proc.ppid not in candidates
            if detached or cur in hooks or _excluded(proc, never_freeze):
                result = True
                break
            cur = candidates[cur].ppid
        else:
            result = verdicts.get(cur, False)
        for item in chain:
            verdicts[item] = result
        return result

    kept = [pid for pid in candidates if not excluded(pid)]
    return sorted(kept, key=lambda pid: (_depth(pid, procs), pid))


def descendants(procs: dict, root_pid: int) -> list:
    children = {}
    for proc in procs.values():
        children.setdefault(proc.ppid, []).append(proc.pid)
    out, stack = [], list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def tree_rss_kb(procs: dict, root_pid: int) -> int:
    pids = [root_pid, *descendants(procs, root_pid)]
    return sum(procs[pid].rss_kb for pid in pids if pid in procs)


FAKE_SIGNALS_ENV = "RESOURCE_GUARD_FAKE_SIGNALS"


def _logging_sender(pid: int, sig: int) -> None:
    """Test mode: record "pid signal" lines in a file instead of signalling,
    so a test (or a hook it runs as a subprocess) built on a fake /proc can
    never hit a real process that happens to share a PID."""
    with open(os.environ[FAKE_SIGNALS_ENV], "a") as fh:
        fh.write(f"{pid} {int(sig)}\n")


def signal_verified(pid: int, start: int, sig: int, root: Path | None = None, sender=None) -> bool:
    """Send sig only if pid still has the recorded start time. Returns True
    when the signal was delivered.

    With pidfds the descriptor is opened first and the start time checked
    after: the descriptor pins one process, so a PID recycled before the open
    fails the check instead of receiving the signal. `sender(pid, sig)` is
    the seam tests use instead of real signals."""
    if pid <= 1 or pid == os.getpid():
        return False
    if sender is None and os.environ.get(FAKE_SIGNALS_ENV):
        sender = _logging_sender
    try:
        if sender is not None:
            if start_of(pid, root) != start:
                return False
            sender(pid, sig)
        elif hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            fd = os.pidfd_open(pid)
            try:
                if start_of(pid, root) != start:
                    return False
                signal.pidfd_send_signal(fd, sig)
            finally:
                os.close(fd)
        else:
            if start_of(pid, root) != start:
                return False
            os.kill(pid, sig)
    except OSError:
        return False
    return True
