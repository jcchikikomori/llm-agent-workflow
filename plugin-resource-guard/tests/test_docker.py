from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from types import SimpleNamespace

from fake_docker import FakeEngine, container
from fixtures import SandboxTestCase

import rg_common as common
import rg_docker as docker
import rg_procs as procs
import rg_sessions as sessions

LABELS = {"dev.claude.pid": "100", "dev.claude.pid_start": "500", "dev.claude.client": "300",
          "dev.claude.client_start": "77"}


class SocketPathTests(SandboxTestCase):
    def test_should_honour_unix_docker_host(self):
        self.assertEqual(docker.socket_path({"DOCKER_HOST": "unix:///tmp/d.sock"}, self.tmp), ("/tmp/d.sock", False))

    def test_should_use_cli_for_tcp_host(self):
        self.assertEqual(docker.socket_path({"DOCKER_HOST": "tcp://1.2.3.4:2375"}, self.tmp), (None, True))

    def test_should_default_to_standard_socket(self):
        self.assertEqual(docker.socket_path({}, self.tmp), ("/var/run/docker.sock", False))

    def write_context(self, name, host):
        (self.tmp / ".docker").mkdir(exist_ok=True)
        (self.tmp / ".docker" / "config.json").write_text(json.dumps({"currentContext": name}))
        meta = self.tmp / ".docker" / "contexts" / "meta" / hashlib.sha256(name.encode()).hexdigest()
        meta.mkdir(parents=True)
        (meta / "meta.json").write_text(json.dumps({"Endpoints": {"docker": {"Host": host}}}))

    def test_should_follow_current_context(self):
        self.write_context("desktop", "unix:///run/desktop.sock")
        self.assertEqual(docker.socket_path({}, self.tmp), ("/run/desktop.sock", False))

    def test_should_use_cli_for_remote_context(self):
        self.write_context("remote", "ssh://box")
        self.assertEqual(docker.socket_path({}, self.tmp), (None, True))

    def test_should_return_no_client_without_socket(self):
        self.assertIsNone(docker.client({"DOCKER_HOST": f"unix://{self.tmp}/absent.sock"}, self.tmp))
        self.assertIsInstance(docker.client({"DOCKER_HOST": "tcp://x:1"}, self.tmp), docker.DockerCLI)


class ApiTests(SandboxTestCase):
    def engine(self, containers):
        return FakeEngine(self.tmp / "d.sock", containers)

    def test_should_list_running_containers(self):
        items = [container("abc", "web", labels={"k": "v"}, created=12)]
        with self.engine(items) as engine:
            found = docker.DockerAPI(engine.socket_path).list_containers()
        self.assertEqual(found, [docker.Container(id="abc", name="web", image="img", state="running",
                                                  labels={"k": "v"}, created=12.0)])

    def test_should_pause_unpause_and_stop(self):
        with self.engine([container("abc", "web")]) as engine:
            api = docker.DockerAPI(engine.socket_path)
            self.assertTrue(api.pause("abc"))
            self.assertFalse(api.pause("abc"))
            self.assertTrue(api.unpause("abc"))
            self.assertTrue(api.stop("abc", grace=3))
            self.assertFalse(api.unpause("missing"))
            self.assertEqual(engine.calls[-2], ("POST", "/containers/abc/stop?t=3"))

    def test_should_raise_unavailable_on_server_error(self):
        with self.engine([container("abc", "web")]) as engine:
            engine.status_override[("POST", "/containers/abc/pause")] = 500
            engine.status_override[("GET", "/containers/json")] = 500
            api = docker.DockerAPI(engine.socket_path)
            with self.assertRaises(docker.DockerUnavailable):
                api.pause("abc")
            with self.assertRaises(docker.DockerUnavailable):
                api.list_containers()

    def test_should_raise_unavailable_without_daemon(self):
        with self.assertRaises(docker.DockerUnavailable):
            docker.DockerAPI(str(self.tmp / "absent.sock"), timeout=0.5).list_containers()


class CliTests(unittest.TestCase):
    def runner(self, returncode=0, stdout="", raises=None):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            run.timeouts.append(kwargs.get("timeout"))
            if raises:
                raise raises
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="boom")

        run.calls = calls
        run.timeouts = []
        return run

    def test_should_parse_ps_json_lines(self):
        line = json.dumps({"ID": "abc", "Names": "web", "Image": "img", "State": "running", "Labels": "a=1,b=2,junk"})
        cli = docker.DockerCLI(runner=self.runner(stdout=line + "\nnot json\n"))
        self.assertEqual(cli.list_containers()[0].labels, {"a": "1", "b": "2"})

    def test_should_map_actions_to_cli_commands(self):
        run = self.runner()
        cli = docker.DockerCLI(runner=run)
        self.assertTrue(cli.pause("abc") and cli.unpause("abc") and cli.stop("abc", grace=4))
        self.assertEqual(run.calls[-1], ["docker", "stop", "-t", "4", "abc"])
        self.assertEqual(run.timeouts, [5.0, 5.0, 9.0])

    def test_should_report_failures(self):
        self.assertFalse(docker.DockerCLI(runner=self.runner(returncode=1)).pause("abc"))
        timeout = subprocess.TimeoutExpired("docker", 5)
        with self.assertRaises(docker.DockerUnavailable):
            docker.DockerCLI(runner=self.runner(raises=timeout)).list_containers()


class CgroupTests(SandboxTestCase):
    def test_should_read_memory_current_from_either_layout(self):
        first = self.sys_root / "fs/cgroup/docker/aaa"
        first.mkdir(parents=True)
        (first / "memory.current").write_text("1024\n")
        (first / "memory.stat").write_text("anon 900\ninactive_file 1000\nactive_file 5\n")
        second = self.sys_root / "fs/cgroup/system.slice/docker-bbb.scope"
        second.mkdir(parents=True)
        (second / "memory.current").write_text("2048\n")
        self.assertEqual(docker.cgroup_mem("aaa"), 24)
        self.assertEqual(docker.cgroup_mem("bbb"), 2048)
        self.assertIsNone(docker.cgroup_mem("ccc"))


class InvocationTests(unittest.TestCase):
    def test_should_parse_run_names_and_exec_targets(self):
        self.assertEqual(docker.docker_invocation(["docker", "run", "--name", "x", "img"]), ("run", "x"))
        self.assertEqual(docker.docker_invocation(["/usr/bin/docker", "-l", "debug", "container", "create",
                                                   "--name=y", "img"]), ("run", "y"))
        self.assertEqual(docker.docker_invocation(["docker", "run", "img"]), ("run", None))
        self.assertEqual(docker.docker_invocation(["docker", "exec", "-it", "-u", "root", "c1", "sh"]), ("exec", "c1"))

    def test_should_read_run_options_only_up_to_the_image(self):
        argv = ["docker", "run", "-it", "-e", "FOO=1", "-p8080:80", "--network", "host", "-dp", "81:81",
                "--rm", "--name", "n", "img", "--name", "later"]
        self.assertEqual(docker.docker_invocation(argv), ("run", "n"))
        self.assertEqual(docker.docker_invocation(["docker", "run", "alpine", "prog", "--name", "api"]), ("run", None))
        self.assertEqual(docker.docker_invocation(["docker", "run", "--env=A=1", "--", "img"]), ("run", None))
        self.assertEqual(docker.docker_invocation(["docker", "run", "--name"]), ("run", None))

    def test_should_ignore_other_commands(self):
        self.assertIsNone(docker.docker_invocation(["docker", "ps"]))
        self.assertIsNone(docker.docker_invocation(["docker", "exec", "-it"]))
        self.assertIsNone(docker.docker_invocation(["docker"]))
        self.assertIsNone(docker.docker_invocation(["podman", "run", "x"]))
        self.assertIsNone(docker.docker_invocation([]))


class AttributionTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.s1 = sessions.Session(pid=100, start=500)
        self.s2 = sessions.Session(pid=200, start=600)
        self.cfg = common.load_config()

    def c(self, cid, name, labels=None, state="running", image="img"):
        return docker.Container(id=cid, name=name, image=image, state=state, labels=labels or {})

    def test_should_attribute_by_shim_labels(self):
        attrs = docker.attribute([self.c("a", "x", LABELS)], [self.s1, self.s2], {}, {})
        self.assertEqual(attrs["a"], docker.Attribution(owner="100-500", refs={"100-500"}, client=(300, 77), via="label"))

    def test_should_reject_label_from_reused_session_pid(self):
        labels = dict(LABELS, **{"dev.claude.pid_start": "499"})
        self.assertIsNone(docker.attribute([self.c("a", "x", labels)], [self.s1], {}, {})["a"].owner)

    def test_should_leave_unlabeled_lsp_container_unattributed(self):
        labels = {"com.docker.compose.service": "app", "com.docker.compose.oneoff": "True"}
        self.assertEqual(docker.attribute([self.c("a", "rto-app-run-1", labels)], [self.s1], {}, {})["a"].via, "none")

    def test_should_attribute_named_run_and_exec_references(self):
        self.proc.add(301, cmdline=("docker", "run", "--name", "review", "img"), start=88)
        self.proc.add(401, cmdline=("docker", "exec", "-it", "review", "bash"))
        self.proc.add(402, cmdline=("docker", "exec", "fedcba987654", "ls"))
        containers = [self.c("abcdef1234567890", "review"), self.c("fedcba9876543210", "other")]
        work = {"100-500": [301], "200-600": [401, 402, 999]}
        attrs = docker.attribute(containers, [self.s1, self.s2], procs.scan(), work)
        self.assertEqual(attrs["abcdef1234567890"].owner, "100-500")
        self.assertEqual(attrs["abcdef1234567890"].client, (301, 88))
        self.assertEqual(attrs["abcdef1234567890"].refs, {"100-500", "200-600"})
        self.assertEqual(attrs["fedcba9876543210"].refs, {"200-600"})
        self.assertIsNone(attrs["fedcba9876543210"].owner)

    def test_should_never_claim_a_container_through_an_id_prefix(self):
        self.proc.add(301, cmdline=("docker", "run", "--name", "db", "postgres"), start=88)
        self.proc.add(401, cmdline=("docker", "exec", "abcdef12", "ls"))
        self.proc.add(402, cmdline=("docker", "exec", "abcdef123456", "ls"))
        containers = [self.c("db0123456789abcd", "mempalace"), self.c("abcdef1234567890", "one"),
                      self.c("abcdef123456ffff", "two")]
        attrs = docker.attribute(containers, [self.s1, self.s2], procs.scan(), {"100-500": [301], "200-600": [401, 402]})
        self.assertEqual([(a.owner, a.refs) for a in attrs.values()], [(None, set())] * 3)

    def test_should_pause_only_owned_unshared_one_offs(self):
        mine = docker.Attribution(owner="100-500", refs={"100-500"})
        shared = docker.Attribution(owner="100-500", refs={"100-500", "200-600"})
        targets = {"100-500"}
        self.assertEqual(docker.pausable(self.c("a", "x"), mine, targets, self.cfg), (True, "ok"))
        self.assertEqual(docker.pausable(self.c("a", "x"), shared, targets, self.cfg)[1], "shared with a running session")
        self.assertEqual(docker.pausable(self.c("a", "x"), docker.Attribution(), targets, self.cfg)[1], "unattributed")
        self.assertEqual(docker.pausable(self.c("a", "x", state="paused"), mine, targets, self.cfg)[1], "paused")
        self.assertEqual(docker.pausable(self.c("a", "x"), mine, {"200-600"}, self.cfg)[0], False)

    def test_should_never_pause_compose_services_or_protected_images(self):
        mine = docker.Attribution(owner="100-500", refs={"100-500"})
        service = self.c("a", "rto-db-1", {"com.docker.compose.service": "db", "com.docker.compose.oneoff": "False"})
        one_off = self.c("b", "rto-app-run-1", {"com.docker.compose.service": "app", "com.docker.compose.oneoff": "True"})
        db = self.c("c", "scratch", image="mysql:8")
        self.assertEqual(docker.pausable(service, mine, {"100-500"}, self.cfg)[1], "compose service")
        self.assertEqual(docker.pausable(one_off, mine, {"100-500"}, self.cfg), (True, "ok"))
        self.assertEqual(docker.pausable(db, mine, {"100-500"}, self.cfg)[1], "never_pause *mysql*")


if __name__ == "__main__":
    unittest.main()
