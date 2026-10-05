"""The hook as Claude Code runs it: a python3 subprocess fed JSON on stdin.
The SessionStart case spawns a real (sandboxed) watchdog and checks the hook
returns without waiting on it: a child holding the hook's stdout would make
Claude Code wait for the hook timeout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

from fixtures import HOOKS, SandboxTestCase

import rg_common as common
import rg_pressure as pressure
import rg_watchdog_ctl as ctl

HOOK = HOOKS / "resource_guard_hook.py"


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
class HookSubprocessTests(SandboxTestCase):
    def run_hook(self, payload, timeout=20):
        result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                                text=True, timeout=timeout, env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_should_return_quickly_while_detached_watchdog_runs_then_exits(self):
        self.user_config({"idle_exit_seconds": 0, "host_check": {"enabled": False}})
        self.proc.meminfo(total=100, available=60)
        started = time.monotonic()
        self.run_hook({"hook_event_name": "SessionStart", "source": "startup"})
        self.assertLess(time.monotonic() - started, 5.0)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not pressure.status_path().exists():
            time.sleep(0.1)
        self.assertTrue(pressure.status_path().exists(), ctl.log_path().read_text() if ctl.log_path().exists() else "")
        while time.monotonic() < deadline and ctl.alive():
            time.sleep(0.1)
        self.assertFalse(ctl.alive())
        self.assertIn("watchdog exiting: idle", ctl.log_path().read_text())

    def test_should_deny_heavy_work_from_background_session(self):
        self.claude(100, start=500)
        self.claude(200, start=600)
        self.cc_session(100, 500)
        self.cc_session(200, 600)
        common.atomic_write_json(common.state_dir() / "prompts" / "200-600.json", {"ts": time.time()})
        common.atomic_write_json(pressure.status_path(), {"ts": time.time() + 3600, "level": "critical",
                                                          "reasons": ["psi_memory_full=12.0"]})
        self.user_config({"gate_wait_seconds": 0.2, "gate_poll_seconds": 0.1})
        os.environ["CLAUDE_PID"] = "100"
        out = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                             "tool_input": {"command": "docker compose run --rm app rspec"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_should_exit_zero_on_garbage(self):
        result = subprocess.run([sys.executable, str(HOOK)], input="{oops", capture_output=True, text=True,
                                timeout=20)
        self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
