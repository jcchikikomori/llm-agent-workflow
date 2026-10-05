from __future__ import annotations

import fcntl
import os
import subprocess
import unittest
import unittest.mock

from fixtures import SandboxTestCase

import rg_actions as actions
import rg_common as common
import rg_watchdog_ctl as ctl


class FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))


class CtlTestCase(SandboxTestCase):
    def hold_lock(self):
        fd = os.open(str(ctl.lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)


class AliveTests(CtlTestCase):
    def test_should_be_dead_when_lock_is_free(self):
        self.assertFalse(ctl.alive())

    def test_should_be_alive_while_lock_is_held(self):
        self.hold_lock()
        self.assertTrue(ctl.alive())

    def test_should_report_hung_only_when_alive_with_old_heartbeat(self):
        common.atomic_write_json(common.state_dir() / "status.json", {"ts": 1000.0})
        self.assertFalse(ctl.hung(now=1500.0))
        self.hold_lock()
        os.utime(ctl.lock_path(), (900.0, 900.0))
        self.assertTrue(ctl.hung(now=1500.0))
        self.assertEqual(ctl.ensure_watchdog(now=1500.0, popen=FakePopen()), "hung")
        self.assertFalse(ctl.hung(now=1010.0))
        self.assertEqual(ctl.heartbeat_age(now=1010.0), 10.0)

    def test_should_not_call_a_just_started_watchdog_hung(self):
        common.atomic_write_json(common.state_dir() / "status.json", {"ts": 1000.0})
        self.hold_lock()
        os.utime(ctl.lock_path(), (1450.0, 1450.0))
        self.assertFalse(ctl.hung(now=1500.0))

    def test_should_have_no_heartbeat_without_status(self):
        self.assertIsNone(ctl.heartbeat_age())


class SpawnTests(CtlTestCase):
    def test_should_strip_claude_env(self):
        self.assertEqual(ctl.clean_env({"CLAUDE_PID": "1", "CLAUDE_X": "y", "PATH": "/bin"}), {"PATH": "/bin"})

    def test_should_spawn_detached_with_clean_env_and_closed_stdio(self):
        popen = FakePopen()
        os.environ["CLAUDE_PID"] = "123"
        self.assertEqual(ctl.ensure_watchdog(now=1000.0, popen=popen), "spawned")
        argv, kwargs = popen.calls[0]
        self.assertTrue(argv[1].endswith("scripts/watchdog.py"))
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], "/")
        self.assertNotIn("CLAUDE_PID", kwargs["env"])
        self.assertEqual(kwargs["umask"], 0o077)

    def test_should_not_spawn_when_running(self):
        self.hold_lock()
        popen = FakePopen()
        self.assertEqual(ctl.ensure_watchdog(popen=popen), "running")
        self.assertEqual(popen.calls, [])

    def test_should_throttle_and_give_up_on_crash_loop(self):
        popen = FakePopen()
        self.assertEqual(ctl.ensure_watchdog(now=1000.0, popen=popen), "spawned")
        self.assertEqual(ctl.ensure_watchdog(now=1010.0, popen=popen), "throttled")
        self.assertEqual(ctl.ensure_watchdog(now=1040.0, popen=popen), "spawned")
        self.assertEqual(ctl.ensure_watchdog(now=1080.0, popen=popen), "spawned")
        self.assertEqual(ctl.ensure_watchdog(now=1120.0, popen=popen), "gave_up")
        self.assertEqual(ctl.ensure_watchdog(now=1120.0, popen=popen, force=True), "spawned")
        self.assertEqual(ctl.ensure_watchdog(now=1700.0, popen=popen), "spawned")

    def test_should_forget_crashes_after_clean_exit(self):
        popen = FakePopen()
        for now in (1000.0, 1040.0, 1080.0):
            ctl.ensure_watchdog(now=now, popen=popen)
        ctl.clear_spawn_history()
        ctl.clear_spawn_history()
        self.assertEqual(ctl.ensure_watchdog(now=1120.0, popen=popen), "spawned")

    def freeze_one(self):
        self.claude(100, start=500)
        self.proc.add(300, ppid=100, start=530)
        actions.save_frozen({"sessions": {"100-500": {"pid": 100, "start": 500, "procs": [[300, 530]],
                                                      "containers": []}}})

    def test_should_leave_resuming_to_a_freshly_spawned_watchdog(self):
        self.freeze_one()
        self.assertEqual(ctl.ensure_watchdog(now=1000.0, popen=FakePopen()), "spawned")
        self.assertEqual((actions.frozen_keys(), self.logged_signals()), ({"100-500"}, []))

    def test_should_resume_frozen_work_itself_when_it_cannot_respawn(self):
        self.freeze_one()
        ctl.ensure_watchdog(now=1000.0, popen=FakePopen())
        self.assertEqual(ctl.ensure_watchdog(now=1010.0, popen=FakePopen()), "throttled")
        self.assertEqual(actions.frozen_keys(), set())
        self.assertEqual(self.logged_signals(), [(300, 18)])

    def test_should_not_raise_when_locks_are_busy(self):
        self.freeze_one()
        ctl.ensure_watchdog(now=1000.0, popen=FakePopen())
        with common.locked(common.actions_lock()), common.locked(common.state_dir() / "spawn.lock"):
            with unittest.mock.patch.object(common, "locked", side_effect=common.LockTimeout("busy")):
                self.assertEqual(ctl.ensure_watchdog(now=1010.0, popen=FakePopen()), "throttled")
        self.assertEqual(actions.frozen_keys(), {"100-500"})

    def test_should_rotate_big_log(self):
        ctl.log_path().write_bytes(b"x" * (ctl.LOG_MAX_BYTES + 1))
        ctl.spawn(popen=FakePopen())
        self.assertTrue(ctl.log_path().with_name("watchdog.log.1").exists())

    def test_should_post_requests(self):
        ctl.post_request("end", "1-2")
        requests = list((common.state_dir() / "requests").glob("*.json"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(common.read_json(requests[0])["kind"], "end")


if __name__ == "__main__":
    unittest.main()
