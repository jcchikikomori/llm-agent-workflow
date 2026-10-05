"""Real signals against a real process tree this test spawns. Only processes
started here carry CLAUDE_PID=<this test's pid>, so nothing else on the
machine can be selected."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from fixtures import SandboxTestCase

import rg_actions as actions
import rg_common as common
import rg_procs as procs
import rg_sessions as sessions


def proc_state(pid):
    with open(f"/proc/{pid}/stat") as fh:
        stat = procs.parse_stat(fh.read())
    return stat["state"] if stat else None


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.isdir("/proc/self"), "needs Linux /proc")
class RealSignalTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("RESOURCE_GUARD_PROC_ROOT")
        os.environ.pop("RESOURCE_GUARD_FAKE_SIGNALS")
        me = os.getpid()
        # Like a Bash-tool shell: CLAUDE_PID, and none of the variables that
        # mark a hook process (the sandbox sets CLAUDE_PLUGIN_ROOT).
        env = {k: v for k, v in os.environ.items() if k not in procs.HOOK_ENV_KEYS}
        env["CLAUDE_PID"] = str(me)
        self.child = subprocess.Popen(["sh", "-c", "sleep 30 & wait"], env=env)
        self.addCleanup(self._kill_child)
        self.assertTrue(wait_for(lambda: len(self._tree()) == 2), "sh and sleep never showed up")
        self.session = sessions.Session(pid=me, start=procs.start_of(me))

    def _tree(self):
        return [pid for pid in procs.descendants(procs.scan(), self.child.pid)] + [self.child.pid]

    def _kill_child(self):
        for pid in self._tree():
            try:
                os.kill(pid, 9)
                os.kill(pid, 18)
            except OSError:
                pass
        self.child.wait(5)

    def test_should_stop_and_continue_real_tree(self):
        result = actions.freeze_session(self.session, cfg=common.load_config(), targets={self.session.key})
        tree = sorted(self._tree())
        self.assertEqual(sorted(result["procs"]), tree)
        self.assertTrue(wait_for(lambda: all(proc_state(pid) == "T" for pid in tree)))

        resumed = actions.resume_session(self.session.key)
        self.assertEqual(resumed["procs"], 2)
        self.assertTrue(wait_for(lambda: all(proc_state(pid) in ("S", "R") for pid in tree)))

    def test_should_use_pidfd_and_refuse_wrong_start(self):
        pid = self.child.pid
        self.assertFalse(procs.signal_verified(pid, procs.start_of(pid) + 1, 19))
        self.assertTrue(procs.signal_verified(pid, procs.start_of(pid), 19))
        self.assertTrue(wait_for(lambda: proc_state(pid) == "T"))
        self.assertTrue(procs.signal_verified(pid, procs.start_of(pid), 18))


if __name__ == "__main__":
    unittest.main()
