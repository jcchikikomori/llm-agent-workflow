"""Shared test fixtures: fake /proc, /sys and ~/.claude trees, plus env
isolation so no test ever reads or signals the live machine by accident."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
HOOKS = PLUGIN / "hooks"
SCRIPTS = PLUGIN / "scripts"
for _path in (HOOKS, SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

ENV_KEYS = (
    "RESOURCE_GUARD_STATE_DIR",
    "RESOURCE_GUARD_CONFIG",
    "RESOURCE_GUARD_DISABLE",
    "RESOURCE_GUARD_PROC_ROOT",
    "RESOURCE_GUARD_SYS_ROOT",
    "RESOURCE_GUARD_CLAUDE_HOME",
    "RESOURCE_GUARD_WSLCONFIG",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PID",
    "CLAUDE_CODE_EXECPATH",
    "DOCKER_HOST",
    "WSL_DISTRO_NAME",
    "RESOURCE_GUARD_FAKE_SIGNALS",
    "CLAUDE_ENV_FILE",
)

UID = os.getuid()


class FakeProc:
    """Builds a /proc-shaped tree. stat lines carry real field positions:
    field 4 ppid, field 22 starttime, field 24 rss (pages)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def meminfo(self, total=16_000_000, available=8_000_000, swap_total=12_000_000, swap_free=10_000_000):
        lines = [
            f"MemTotal:       {total} kB",
            "MemFree:          100000 kB",
            f"MemAvailable:   {available} kB",
            f"SwapTotal:      {swap_total} kB",
            f"SwapFree:       {swap_free} kB",
        ]
        (self.root / "meminfo").write_text("\n".join(lines) + "\n")

    def psi(self, resource, some_avg10=0.0, some_total=0, full_avg10=0.0, full_total=0, full=True):
        (self.root / "pressure").mkdir(exist_ok=True)
        lines = [f"some avg10={some_avg10:.2f} avg60=0.00 avg300=0.00 total={some_total}"]
        if full:
            lines.append(f"full avg10={full_avg10:.2f} avg60=0.00 avg300=0.00 total={full_total}")
        (self.root / "pressure" / resource).write_text("\n".join(lines) + "\n")

    def osrelease(self, text):
        (self.root / "sys" / "kernel").mkdir(parents=True, exist_ok=True)
        (self.root / "sys" / "kernel" / "osrelease").write_text(text + "\n")

    def add(self, pid, ppid=1, comm="sh", state="S", start=1000, cmdline=("sh",), environ=None,
            exe="/usr/bin/sh", rss_pages=256, uid=UID, sid=0):
        pid_dir = self.root / str(pid)
        pid_dir.mkdir(parents=True, exist_ok=True)
        # fields after ')': state ppid pgrp session tty tpgid flags minflt cminflt
        # majflt cmajflt utime stime cutime cstime prio nice threads itreal start vsize rss
        rest = [state, str(ppid), "0", str(sid)] + ["0"] * 15 + [str(start), "0", str(rss_pages)] + ["0"] * 5
        (pid_dir / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n")
        (pid_dir / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (pid_dir / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in cmdline) + b"\0")
        env = environ or {}
        (pid_dir / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()))
        exe_link = pid_dir / "exe"
        if exe_link.is_symlink():
            exe_link.unlink()
        if exe:
            os.symlink(exe, exe_link)
        return pid

    def remove(self, pid):
        pid_dir = self.root / str(pid)
        for child in pid_dir.iterdir():
            child.unlink()
        pid_dir.rmdir()

    def set_state(self, pid, state):
        stat = (self.root / str(pid) / "stat").read_text()
        head, _, tail = stat.rpartition(") ")
        parts = tail.split(" ")
        parts[0] = state
        (self.root / str(pid) / "stat").write_text(head + ") " + " ".join(parts))


class SandboxTestCase(unittest.TestCase):
    """Points every root override at a fresh temp tree and restores the
    environment afterwards."""

    def setUp(self):
        super().setUp()
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.tmp = Path(tempfile.mkdtemp(prefix="rg-test-"))
        self.addCleanup(self._cleanup_tmp)
        self.proc = FakeProc(self.tmp / "proc")
        self.sys_root = self.tmp / "sys"
        self.claude_home = self.tmp / "claude"
        self.state = self.tmp / "state"
        (self.claude_home / "sessions").mkdir(parents=True)
        os.environ["RESOURCE_GUARD_PROC_ROOT"] = str(self.proc.root)
        os.environ["RESOURCE_GUARD_SYS_ROOT"] = str(self.sys_root)
        os.environ["RESOURCE_GUARD_CLAUDE_HOME"] = str(self.claude_home)
        os.environ["RESOURCE_GUARD_STATE_DIR"] = str(self.state)
        os.environ["RESOURCE_GUARD_CONFIG"] = str(self.tmp / "user-config.json")
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN)
        os.environ["CLAUDE_CODE_EXECPATH"] = "/opt/claude/versions/9.9.9"
        # No test may signal a real PID or talk to the real Docker daemon.
        self.signal_log = self.tmp / "signals.log"
        os.environ["RESOURCE_GUARD_FAKE_SIGNALS"] = str(self.signal_log)
        os.environ["DOCKER_HOST"] = f"unix://{self.tmp}/no-docker.sock"

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def logged_signals(self):
        if not self.signal_log.exists():
            return []
        return [tuple(int(x) for x in line.split()) for line in self.signal_log.read_text().splitlines()]

    def _cleanup_tmp(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def user_config(self, data):
        (self.tmp / "user-config.json").write_text(json.dumps(data))

    def cc_session(self, pid, start, session_id="sid", kind="interactive", status="idle", cwd="/work", updated=0):
        data = {"pid": pid, "procStart": str(start), "sessionId": session_id, "kind": kind,
                "status": status, "cwd": cwd, "updatedAt": updated}
        (self.claude_home / "sessions" / f"{pid}.json").write_text(json.dumps(data))

    def claude(self, pid, start=500, ppid=1):
        """A live Claude Code session process in the fake /proc."""
        return self.proc.add(pid, ppid=ppid, comm="claude", start=start, cmdline=("claude",),
                             exe="/opt/claude/versions/9.9.9", rss_pages=1000)
