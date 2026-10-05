from __future__ import annotations

import contextlib
import fcntl
import io
import os
import unittest
from unittest import mock

from fake_docker import FakeEngine, container
from fixtures import SandboxTestCase

import rg_actions as actions
import rg_common as common
import rg_docker as docker
import rg_pressure as pressure
import rg_sessions as sessions
import rg_watchdog_ctl as ctl
import watchdog as wd

FG, BG1, BG2 = "100-500", "200-600", "300-700"


class FakeSampler:
    """Feeds (mem_available_pct, psi_memory_full_pct) pairs as 5 s samples."""

    def __init__(self):
        self.queue = []
        self.ts = 1000.0
        self.full_total = 0
        self.hosts = []

    def push(self, *pairs):
        self.queue.extend(pairs)

    def __call__(self, host=None):
        self.hosts.append(host)
        mem, psi = self.queue.pop(0) if self.queue else (50.0, 0.0)
        self.ts += 5.0
        self.full_total += int(psi / 100 * 5_000_000)
        return pressure.Sample(
            ts=self.ts,
            meminfo={"MemTotal": 1000, "MemAvailable": int(mem * 10)},
            psi={"memory": {"some": {"avg10": psi, "total": self.full_total},
                            "full": {"avg10": psi, "total": self.full_total}}},
            host=host,
        )


class WatchdogTestCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = common.deep_merge(common.load_config(), {"freeze_mode": "enforce"})
        self.sampler = FakeSampler()
        self.engine = None
        self.now = 1000.0
        for pid, start in ((100, 500), (200, 600), (300, 700)):
            self.claude(pid, start=start)
            self.cc_session(pid, start, cwd=f"/w/{pid}")
            self.proc.add(pid + 1, ppid=pid, start=start + 1, environ={"CLAUDE_PID": str(pid)}, comm="rspec")
        sessions.touch_prompt(FG, ts=900.0)

    def make(self, **kw):
        options = dict(sampler=self.sampler, docker_factory=self.docker_factory, host_probe=lambda timeout: None,
                       wsl_info={"wsl": False, "version": None, "distro": ""}, config_loader=lambda: self.cfg)
        options.update(kw)
        return wd.Watchdog(**options)

    def docker_factory(self):
        return docker.DockerAPI(self.engine.socket_path) if self.engine else None

    def ticks(self, dog, count, step=5.0):
        status = None
        for _ in range(count):
            self.now += step
            status = dog.tick(self.now)
        return status

    def kinds(self):
        return [event["kind"] for event in common.read_events()]


class StatusTests(WatchdogTestCase):
    def test_should_write_status_with_sessions_and_foreground(self):
        status = self.ticks(self.make(), 1)
        self.assertEqual(status["level"], "ok")
        self.assertEqual(common.read_json(pressure.status_path())["level"], "ok")
        rows = {row["key"]: row for row in status["sessions"]}
        self.assertEqual(sorted(rows), [FG, BG1, BG2])
        self.assertTrue(rows[FG]["foreground"])
        self.assertEqual(rows[BG1]["work_procs"], 1)
        self.assertEqual(rows[BG1]["cwd"], "/w/200")

    def test_should_tick_faster_when_elevated(self):
        dog = self.make()
        self.assertEqual(dog.interval(), 5.0)
        self.sampler.push((15.0, 0.0))
        self.ticks(dog, 1)
        self.assertEqual(dog.interval(), 2.0)


class EscalationTests(WatchdogTestCase):
    def test_should_freeze_one_background_session_after_confirmed_critical(self):
        dog = self.make()
        self.sampler.push((8.0, 0.0), (8.0, 0.0))
        self.ticks(dog, 1)
        self.assertEqual(actions.frozen_keys(), set())
        self.ticks(dog, 1)
        self.assertEqual(len(actions.frozen_keys()), 1)
        self.assertNotIn(FG, actions.frozen_keys())

    def test_should_freeze_next_only_while_stall_persists(self):
        dog = self.make()
        self.sampler.push((8.0, 20.0), (8.0, 20.0), (8.0, 20.0), (8.0, 20.0))
        self.ticks(dog, 2)
        self.ticks(dog, 2, step=6.0)
        self.assertEqual(actions.frozen_keys(), {BG1, BG2})

    def test_should_stop_escalating_when_memory_is_low_but_calm(self):
        dog = self.make()
        self.sampler.push(*[(8.0, 0.0)] * 6)
        self.ticks(dog, 2)
        self.ticks(dog, 4, step=6.0)
        self.assertEqual(len(actions.frozen_keys()), 1)
        self.assertEqual(self.kinds().count("held-by-idle"), 1)

    def test_should_report_nothing_to_freeze(self):
        for pid in (201, 301):
            self.proc.remove(pid)
        dog = self.make()
        self.sampler.push((8.0, 20.0), (8.0, 20.0), (8.0, 20.0))
        self.ticks(dog, 3, step=11.0)
        self.assertIn("nothing-to-freeze", self.kinds())

    def test_should_freeze_everyone_but_foreground_when_hard(self):
        dog = self.make()
        self.sampler.push((3.0, 0.0))
        self.ticks(dog, 1)
        self.assertEqual(actions.frozen_keys(), {BG1, BG2})

    def test_should_respect_target_allowlist(self):
        self.cfg["target_pids"] = [200]
        dog = self.make()
        self.sampler.push((3.0, 0.0))
        self.ticks(dog, 1)
        self.assertEqual(actions.frozen_keys(), {BG1})

    def test_should_pause_owned_containers(self):
        labels = {"dev.claude.pid": "200", "dev.claude.pid_start": "600"}
        items = [container("c1", "suite", labels=labels), container("c2", "mcp")]
        with FakeEngine(self.tmp / "d.sock", items) as engine:
            self.engine = engine
            self.cfg["target_pids"] = [100, 200]
            dog = self.make()
            self.sampler.push((3.0, 0.0))
            status = self.ticks(dog, 1)
            self.assertEqual([i["State"] for i in items], ["paused", "running"])
        row = next(r for r in status["sessions"] if r["key"] == BG1)
        self.assertEqual(row["containers"], [{"name": "suite", "state": "running", "mem_bytes": None, "role": "work"}])

    def test_should_leave_a_session_with_only_servers_alone(self):
        for pid in (201, 301):
            self.proc.remove(pid)
        labels = {"dev.claude.pid": "200", "dev.claude.pid_start": "600", "dev.claude.role": "server"}
        items = [container("c1", "sonarqube", labels=labels)]
        with FakeEngine(self.tmp / "d.sock", items) as engine:
            self.engine = engine
            dog = self.make()
            self.sampler.push((3.0, 0.0))
            status = self.ticks(dog, 1)
            self.assertEqual(items[0]["State"], "running")
        self.assertEqual(actions.frozen_keys(), set())
        row = next(r for r in status["sessions"] if r["key"] == BG1)
        self.assertEqual(row["containers"][0]["role"], "server")


class SafetyTests(WatchdogTestCase):
    def shared_container(self):
        labels = {"dev.claude.pid": "200", "dev.claude.pid_start": "600"}
        self.proc.add(302, ppid=300, start=702, environ={"CLAUDE_PID": "300"},
                      cmdline=("docker", "exec", "suite", "rspec"))
        return [container("c1", "suite", labels=labels)]

    def test_should_not_pause_a_container_a_running_session_uses(self):
        items = self.shared_container()
        with FakeEngine(self.tmp / "d.sock", items) as engine:
            self.engine = engine
            self.sampler.push((8.0, 0.0), (8.0, 0.0))
            self.ticks(self.make(), 2)
            self.assertEqual(len(actions.frozen_keys()), 1)
            self.assertEqual(items[0]["State"], "running")

    def test_should_pause_a_shared_container_when_hard_freezes_both_users(self):
        items = self.shared_container()
        with FakeEngine(self.tmp / "d.sock", items) as engine:
            self.engine = engine
            self.sampler.push((3.0, 0.0))
            self.ticks(self.make(), 1)
            self.assertEqual(actions.frozen_keys(), {BG1, BG2})
            self.assertEqual(items[0]["State"], "paused")

    def test_should_skip_a_session_the_user_typed_in_while_freezing(self):
        real = actions.freeze_session

        def racing(session, **kw):
            sessions.touch_prompt(session.key, ts=self.now + 1)
            return real(session, **kw)

        self.sampler.push((3.0, 0.0))
        with mock.patch.object(actions, "freeze_session", side_effect=racing):
            self.ticks(self.make(), 1)
        self.assertEqual((actions.frozen_keys(), self.logged_signals()), (set(), []))

    def test_should_drop_containers_once_docker_goes_away(self):
        labels = {"dev.claude.pid": "200", "dev.claude.pid_start": "600"}
        dog = self.make()
        with FakeEngine(self.tmp / "d.sock", [container("c1", "suite", labels=labels)]) as engine:
            self.engine = engine
            self.ticks(dog, 1)
        self.engine = None
        self.sampler.push((3.0, 0.0))
        self.ticks(dog, 1)
        self.assertEqual(actions.frozen_keys(), {BG1, BG2})
        self.assertEqual(actions.load_frozen()["sessions"][BG1]["containers"], [])

    def test_should_not_let_one_stuck_session_block_resuming_the_others(self):
        stuck = {"id": "c1", "name": "suite", "client": None, "client_alive": False}
        actions.save_frozen({"sessions": {
            BG1: {"pid": 200, "start": 600, "procs": [], "containers": [stuck]},
            BG2: {"pid": 300, "start": 700, "procs": [[301, 701]], "containers": []}}})
        self.sampler.push(*[(40.0, 0.0)] * 6)
        self.ticks(self.make(), 5, step=21.0)
        self.assertEqual(actions.frozen_keys(), {BG1})
        self.assertIn((301, 18), self.logged_signals())


class ResumeTests(WatchdogTestCase):
    def freeze_bg1(self, dog):
        self.cfg["target_pids"] = [100, 200]
        self.sampler.push((3.0, 0.0))
        self.ticks(dog, 1)
        self.assertEqual(actions.frozen_keys(), {BG1})

    def test_should_resume_after_calm_samples(self):
        dog = self.make()
        self.freeze_bg1(dog)
        self.sampler.push(*[(40.0, 0.0)] * 3)
        self.ticks(dog, 2)
        self.assertEqual(actions.frozen_keys(), {BG1})
        self.ticks(dog, 1, step=11.0)
        self.assertEqual(actions.frozen_keys(), set())

    def test_should_not_resume_while_memory_keeps_falling(self):
        dog = self.make()
        self.freeze_bg1(dog)
        self.sampler.push((40.0, 0.0), (35.0, 0.0), (30.0, 0.0), (25.0, 0.0))
        self.ticks(dog, 4, step=10.0)
        self.assertEqual(actions.frozen_keys(), {BG1})

    def test_should_back_off_session_that_tipped_load_again(self):
        dog = self.make()
        self.freeze_bg1(dog)
        self.sampler.push(*[(40.0, 0.0)] * 3)
        self.ticks(dog, 3, step=8.0)
        self.assertEqual(actions.frozen_keys(), set())
        self.sampler.push((3.0, 0.0))
        self.ticks(dog, 1)
        self.assertEqual(actions.frozen_keys(), {BG1})
        self.sampler.push(*[(40.0, 0.0)] * 4)
        self.ticks(dog, 4, step=8.0)
        self.assertEqual(actions.frozen_keys(), {BG1})
        self.sampler.push(*[(40.0, 0.0)] * 3)
        self.ticks(dog, 3, step=20.0)
        self.assertEqual(actions.frozen_keys(), set())

    def test_should_resume_on_request(self):
        dog = self.make()
        self.freeze_bg1(dog)
        ctl.post_request("resume", BG1)
        self.sampler.push((3.0, 0.0))
        self.ticks(dog, 1)
        self.assertIn("resume", self.kinds())

    def test_should_resume_work_of_dead_session(self):
        dog = self.make()
        self.freeze_bg1(dog)
        self.proc.remove(200)
        (self.claude_home / "sessions" / "200.json").unlink()
        self.sampler.push((3.0, 0.0))
        self.ticks(dog, 1)
        self.assertEqual(actions.frozen_keys(), set())
        self.assertIn((201, 18), self.logged_signals())


class ObserveTests(WatchdogTestCase):
    def test_should_only_log_in_observe_mode(self):
        self.cfg["freeze_mode"] = "observe"
        dog = self.make()
        self.sampler.push((3.0, 0.0), (3.0, 0.0))
        status = self.ticks(dog, 2)
        self.assertEqual(actions.frozen_keys(), set())
        self.assertEqual(self.logged_signals(), [])
        self.assertEqual(self.kinds().count("would-freeze"), 2)
        self.assertTrue(next(r for r in status["sessions"] if r["key"] == BG1)["would_freeze"])
        self.sampler.push(*[(40.0, 0.0)] * 6)
        self.ticks(dog, 6, step=21.0)
        self.assertEqual(self.kinds().count("would-resume"), 2)


class LifecycleTests(WatchdogTestCase):
    def test_should_probe_host_on_wsl_and_use_it(self):
        host = {"total_kb": 100, "available_kb": 3, "commit_total_kb": 100, "commit_free_kb": 50}
        dog = self.make(host_probe=lambda timeout: host, wsl_info={"wsl": True, "version": 2, "distro": "U"})
        status = self.ticks(dog, 1)
        self.assertEqual(status["level"], "hard")
        self.assertEqual(status["reasons"], ["host_available_pct=3.0"])
        self.ticks(dog, 1)
        self.assertEqual(self.sampler.hosts[-1], host)

    def test_should_exit_when_idle(self):
        for pid in (100, 200, 300):
            self.proc.remove(pid)
        self.cfg["idle_exit_seconds"] = 10
        dog = self.make()
        self.assertNotIn("exit", self.ticks(dog, 1))
        self.assertEqual(self.ticks(dog, 2)["exit"], "idle")

    def test_should_resume_everything_and_exit_when_disabled(self):
        dog = self.make()
        self.sampler.push((3.0, 0.0))
        self.ticks(dog, 1)
        self.cfg["enabled"] = False
        self.assertEqual(self.ticks(dog, 1), {"exit": "disabled"})
        self.assertEqual(actions.frozen_keys(), set())


class FailureTests(WatchdogTestCase):
    def test_should_keep_going_when_docker_list_fails(self):
        with FakeEngine(self.tmp / "d.sock", []) as engine:
            engine.status_override[("GET", "/containers/json")] = 500
            self.engine = engine
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = self.ticks(self.make(), 1)
        self.assertEqual(status["level"], "ok")
        self.assertIn("docker unavailable: list returned 500", out.getvalue())

    def test_should_skip_actions_when_lock_is_busy(self):
        dog = self.make()
        self.sampler.push((3.0, 0.0))
        with mock.patch.object(actions, "freeze_session", side_effect=common.LockTimeout("x")):
            self.ticks(dog, 1)
        self.assertEqual(actions.frozen_keys(), set())

    def test_should_keep_requests_and_dead_sessions_for_next_tick_when_lock_is_busy(self):
        dog = self.make()
        actions.save_frozen({"sessions": {BG1: {"pid": 200, "start": 600, "procs": [], "containers": []},
                                          "999-1": {"pid": 999, "start": 1, "procs": [], "containers": []}}})
        ctl.post_request("end", BG1)
        self.sampler.push(*[(40.0, 0.0)] * 4)
        with mock.patch.object(actions, "resume_session", side_effect=common.LockTimeout("x")):
            self.ticks(dog, 4, step=21.0)
        self.assertEqual(actions.frozen_keys(), {BG1, "999-1"})
        self.assertEqual(len(list((common.state_dir() / "requests").glob("*-end-*.json"))), 1)
        self.ticks(dog, 1)
        self.assertEqual(actions.frozen_keys(), set())

    def test_should_ignore_malformed_requests(self):
        ctl.post_request("bogus", BG1)
        (common.state_dir() / "requests" / "junk.json").write_text("{")
        (common.state_dir() / "requests" / "list.json").write_text("[1]")
        self.ticks(self.make(), 1)
        self.assertEqual(list((common.state_dir() / "requests").glob("*.json")), [])


class RunTests(WatchdogTestCase):
    def test_should_resume_leftovers_run_and_clean_up(self):
        actions.save_frozen({"sessions": {BG1: {"pid": 200, "start": 600, "procs": [[201, 601]], "containers": []}}})
        common.atomic_write_json(ctl.spawns_path(), [1.0])
        dog = self.make()
        self.sampler.push((3.0, 0.0))
        self.assertEqual(wd.run(dog, max_ticks=2, sleep=lambda s: None), 0)
        self.assertEqual(actions.frozen_keys(), set())
        self.assertFalse(ctl.spawns_path().exists())
        self.assertIn("watchdog-start", [e.get("reason") for e in common.read_events()])
        self.assertIn("watchdog-exit", [e.get("reason") for e in common.read_events()])

    def test_should_survive_a_failing_tick(self):
        dog = self.make()

        def boom(now=None):
            raise RuntimeError("bad tick")

        dog.tick = boom
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wd.run(dog, max_ticks=2, sleep=lambda s: None), 0)
        self.assertIn("tick failed: RuntimeError: bad tick", out.getvalue())

    def test_should_step_aside_after_repeated_failing_ticks(self):
        dog = self.make()
        calls = []

        def boom(now=None):
            calls.append(now)
            raise RuntimeError("bad tick")

        dog.tick = boom
        common.atomic_write_json(ctl.spawns_path(), [1.0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wd.run(dog, max_ticks=50, sleep=lambda s: None), 0)
        self.assertEqual(len(calls), wd.MAX_FAILED_TICKS)
        self.assertIn("watchdog exiting: failing", out.getvalue())
        self.assertTrue(ctl.spawns_path().exists())

    def test_should_treat_signal_as_clean_exit(self):
        dog = self.make()

        def stop(seconds):
            raise SystemExit(0)

        common.atomic_write_json(ctl.spawns_path(), [1.0])
        self.assertEqual(wd.run(dog, max_ticks=5, sleep=stop), 0)
        self.assertFalse(ctl.spawns_path().exists())

    def test_should_exit_at_once_when_another_instance_runs(self):
        fd = os.open(str(ctl.lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        self.assertEqual(wd.run(self.make(), max_ticks=1, sleep=lambda s: None), 0)
        self.assertFalse(pressure.status_path().exists())


if __name__ == "__main__":
    unittest.main()
