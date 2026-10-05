from __future__ import annotations

import signal
import unittest

from fake_docker import FakeEngine, container
from fixtures import SandboxTestCase

import rg_actions as actions
import rg_common as common
import rg_docker as docker
import rg_sessions as sessions

ENV = {"CLAUDE_PID": "100"}
KEY = "100-500"
OWNED = {"dev.claude.pid": "100", "dev.claude.pid_start": "500", "dev.claude.client": "301",
         "dev.claude.client_start": "531"}


class ActionTestCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.claude(100, start=500)
        self.session = sessions.Session(pid=100, start=500)
        self.cfg = common.load_config()
        self.signals = []

    def sender(self, pid, sig):
        self.signals.append((pid, sig))
        if sig == signal.SIGSTOP:
            self.proc.set_state(pid, "T")
        elif sig == signal.SIGCONT:
            self.proc.set_state(pid, "S")

    def engine(self, items):
        return FakeEngine(self.tmp / "d.sock", items)

    def freeze(self, api=None, mode="enforce", sessions_list=None):
        found = api.list_containers() if api else []
        attrs = docker.attribute(found, sessions_list or [self.session], {}, {})
        return actions.freeze_session(self.session, cfg=self.cfg, targets={KEY}, docker=api, containers=found,
                                      attrs=attrs, mode=mode, reason="test", sender=self.sender)


class FreezeTests(ActionTestCase):
    def test_should_stop_work_parent_first_and_record_it(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        self.proc.add(301, ppid=300, start=531, environ=ENV)
        result = self.freeze()
        self.assertEqual(result["procs"], [300, 301])
        self.assertEqual(self.signals, [(300, signal.SIGSTOP), (301, signal.SIGSTOP)])
        self.assertEqual(actions.load_frozen()["sessions"][KEY]["procs"], [[300, 530], [301, 531]])
        self.assertEqual(common.read_events()[-1]["kind"], "freeze")

    def test_should_leave_already_stopped_processes_alone(self):
        self.proc.add(300, ppid=100, start=530, state="T", environ=ENV)
        self.assertEqual(self.freeze()["procs"], [])
        self.assertEqual(actions.frozen_keys(), set())

    def test_should_catch_processes_forked_during_freeze(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        original = self.sender

        def forking_sender(pid, sig):
            original(pid, sig)
            if pid == 300:
                self.proc.add(302, ppid=300, start=532, environ=ENV)

        self.sender = forking_sender
        self.assertEqual(self.freeze()["procs"], [300, 302])

    def test_should_skip_processes_whose_signal_fails(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        self.sender = lambda pid, sig: (_ for _ in ()).throw(PermissionError())
        self.assertEqual(self.freeze()["procs"], [])

    def test_should_pause_owned_containers_and_record_client(self):
        self.proc.add(301, start=531, cmdline=("docker", "run", "img"))
        items = [container("c1", "suite", labels=OWNED), container("c2", "db", image="mysql:8", labels=OWNED),
                 container("c3", "lsp")]
        with self.engine(items) as engine:
            result = self.freeze(api=docker.DockerAPI(engine.socket_path))
            self.assertEqual(result["containers"], ["suite"])
            self.assertEqual(items[0]["State"], "paused")
            self.assertEqual(items[2]["State"], "running")
        record = actions.load_frozen()["sessions"][KEY]["containers"][0]
        self.assertEqual(record, {"id": "c1", "name": "suite", "client": [301, 531], "client_alive": True})

    def test_should_not_record_container_daemon_refused(self):
        with self.engine([container("c1", "suite", labels=OWNED)]) as engine:
            api = docker.DockerAPI(engine.socket_path)
            found = api.list_containers()
            engine.status_override[("POST", "/containers/c1/pause")] = 409
            attrs = docker.attribute(found, [self.session], {}, {})
            result = actions.freeze_session(self.session, cfg=self.cfg, targets={KEY}, docker=api,
                                            containers=found, attrs=attrs, sender=self.sender)
            engine.status_override[("POST", "/containers/c1/pause")] = 500
            again = actions.freeze_session(self.session, cfg=self.cfg, targets={KEY}, docker=api,
                                           containers=found, attrs=attrs, sender=self.sender)
        self.assertEqual((result["containers"], again["containers"]), ([], []))

    def test_should_only_report_in_observe_mode(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        with self.engine([container("c1", "suite", labels=OWNED)]) as engine:
            items = engine.containers
            result = self.freeze(api=docker.DockerAPI(engine.socket_path), mode="observe")
        self.assertEqual((result["procs"], result["containers"]), ([300], ["suite"]))
        self.assertEqual(self.signals, [])
        self.assertEqual(items[0]["State"], "running")
        self.assertEqual(actions.frozen_keys(), set())
        self.assertEqual(common.read_events()[-1]["kind"], "would-freeze")


class FreezeSafetyTests(ActionTestCase):
    def test_should_record_stopped_processes_even_when_freeze_dies_midway(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        self.proc.add(301, ppid=300, start=531, environ=ENV)

        def dying(pid, sig):
            if pid == 301:
                raise SystemExit(0)  # SIGTERM arriving between two signals
            self.sender(pid, sig)

        with self.assertRaises(SystemExit):
            actions.freeze_session(self.session, cfg=self.cfg, targets={KEY}, sender=dying)
        self.assertEqual(actions.load_frozen()["sessions"][KEY]["procs"], [[300, 530], [301, 531]])

    def test_should_skip_containers_without_a_docker_client(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        found = [docker.Container(id="c1", name="suite", image="img", state="running", labels=OWNED)]
        attrs = docker.attribute(found, [self.session], {}, {})
        result = actions.freeze_session(self.session, cfg=self.cfg, targets={KEY}, docker=None, containers=found,
                                        attrs=attrs, sender=self.sender)
        self.assertEqual((result["procs"], result["containers"]), ([300], []))
        self.assertEqual(actions.load_frozen()["sessions"][KEY]["containers"], [])

    def test_should_skip_a_session_that_became_foreground_while_waiting(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        result = actions.freeze_session(self.session, cfg=self.cfg, targets={KEY}, sender=self.sender,
                                        still_target=lambda: False)
        self.assertTrue(result["skipped"])
        self.assertEqual((self.signals, actions.frozen_keys()), ([], set()))

    def test_should_drop_unsignalled_processes_from_the_record(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        result = actions.freeze_session(self.session, cfg=self.cfg, targets={KEY},
                                        sender=lambda pid, sig: (_ for _ in ()).throw(OSError("gone")))
        self.assertEqual((result["procs"], actions.frozen_keys()), ([], set()))


class ResumeTests(ActionTestCase):
    def test_should_give_up_on_a_container_after_repeated_unpause_failures(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        with self.engine([container("c1", "suite", labels=OWNED)]) as engine:
            self.freeze(api=docker.DockerAPI(engine.socket_path))
        for _ in range(actions.UNPAUSE_ATTEMPTS - 1):
            self.assertEqual(actions.resume_session(KEY, docker=None, sender=self.sender)["pending"], ["suite"])
        result = actions.resume_session(KEY, docker=None, sender=self.sender)
        self.assertEqual((result["pending"], result["abandoned"]), ([], ["suite"]))
        self.assertEqual(actions.frozen_keys(), set())
        self.assertEqual(common.read_events()[-1]["kind"], "unpause-failed")

    def test_should_stop_resuming_at_the_deadline(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        self.freeze()
        self.assertEqual(actions.resume_all(sender=self.sender, deadline=0.0), [])
        self.assertEqual(actions.frozen_keys(), {KEY})

    def test_should_pass_the_stop_grace_to_orphan_stops(self):
        self.proc.add(301, start=531, cmdline=("docker", "run", "img"))
        items = [container("c1", "suite", labels=OWNED)]
        with self.engine(items) as engine:
            api = docker.DockerAPI(engine.socket_path)
            self.freeze(api=api)
            self.proc.remove(301)
            actions.resume_session(KEY, docker=api, sender=self.sender, stop_grace=2)
            self.assertIn(("POST", "/containers/c1/stop?t=2"), engine.calls)

    def test_should_unpause_then_continue_children_first(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        self.proc.add(301, ppid=300, start=531, environ=ENV, cmdline=("docker", "run", "img"))
        items = [container("c1", "suite", labels=OWNED)]
        with self.engine(items) as engine:
            api = docker.DockerAPI(engine.socket_path)
            self.freeze(api=api)
            self.signals.clear()
            result = actions.resume_session(KEY, docker=api, sender=self.sender)
            self.assertEqual(items[0]["State"], "running")
            self.assertEqual(engine.calls[-1], ("POST", "/containers/c1/unpause"))
        self.assertEqual(self.signals, [(301, signal.SIGCONT), (300, signal.SIGCONT)])
        self.assertEqual((result["procs"], result["containers"], result["orphans_stopped"]), (2, ["suite"], []))
        self.assertEqual(actions.frozen_keys(), set())

    def test_should_stop_container_whose_client_died_while_frozen(self):
        self.proc.add(301, start=531, cmdline=("docker", "run", "img"))
        items = [container("c1", "suite", labels=OWNED)]
        with self.engine(items) as engine:
            api = docker.DockerAPI(engine.socket_path)
            self.freeze(api=api)
            self.proc.remove(301)
            result = actions.resume_session(KEY, docker=api, sender=self.sender)
        self.assertEqual(result["orphans_stopped"], ["suite"])
        self.assertEqual(items[0]["State"], "exited")
        self.assertEqual(common.read_events()[-1]["kind"], "orphan-stopped")

    def test_should_not_stop_detached_container_without_live_client(self):
        items = [container("c1", "suite", labels=OWNED)]
        with self.engine(items) as engine:
            api = docker.DockerAPI(engine.socket_path)
            self.freeze(api=api)
            result = actions.resume_session(KEY, docker=api, sender=self.sender)
        self.assertEqual(result["orphans_stopped"], [])
        self.assertEqual(items[0]["State"], "running")

    def test_should_keep_containers_pending_when_daemon_is_down(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        with self.engine([container("c1", "suite", labels=OWNED)]) as engine:
            self.freeze(api=docker.DockerAPI(engine.socket_path))
        result = actions.resume_session(KEY, docker=None, sender=self.sender)
        self.assertEqual((result["procs"], result["pending"]), (1, ["suite"]))
        entry = actions.load_frozen()["sessions"][KEY]
        self.assertEqual((entry["procs"], [c["id"] for c in entry["containers"]]), ([], ["c1"]))

    def test_should_report_unknown_session(self):
        self.assertEqual(actions.resume_session("9-9", sender=self.sender), {"key": "9-9", "resumed": False})

    def test_should_resume_everything(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        self.freeze()
        results = actions.resume_all(sender=self.sender)
        self.assertEqual([r["key"] for r in results], [KEY])
        self.assertEqual(actions.frozen_keys(), set())

    def test_should_skip_reused_pid_on_resume(self):
        self.proc.add(300, ppid=100, start=530, environ=ENV)
        self.freeze()
        self.proc.add(300, ppid=1, start=99)
        self.signals.clear()
        self.assertEqual(actions.resume_session(KEY, sender=self.sender)["procs"], 0)
        self.assertEqual(self.signals, [])

    def test_should_read_corrupt_frozen_file_as_empty(self):
        actions.frozen_path().write_text('{"sessions": [1]}')
        self.assertEqual(actions.load_frozen(), {"sessions": {}})


if __name__ == "__main__":
    unittest.main()
