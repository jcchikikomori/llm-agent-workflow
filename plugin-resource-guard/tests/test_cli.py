from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import time
import unittest
from unittest import mock

from fake_docker import FakeEngine, container
from fixtures import SandboxTestCase

import resource_guard as cli
import rg_actions as actions
import rg_common as common
import rg_pressure as pressure
import rg_sessions as sessions
import rg_watchdog_ctl as ctl

ENV100 = {"CLAUDE_PID": "100"}
LABELS = {"dev.claude.pid": "100", "dev.claude.pid_start": "500"}


class CliTestCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.claude(100, start=500)
        self.claude(200, start=600)
        self.cc_session(100, 500, session_id="aaaabbbb-1", cwd="/w/one")
        self.cc_session(200, 600, session_id="ccccdddd-2", cwd="/w/two", kind="bg")
        self.proc.add(301, ppid=100, start=531, environ=ENV100, comm="rspec", rss_pages=2560)
        self.proc.meminfo(total=16_000_000, available=8_000_000)
        sessions.touch_prompt("200-600", ts=time.time())
        cli.SLEEP = lambda s: None

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def hold_watchdog_lock(self):
        fd = os.open(str(ctl.lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)


class FormatTests(unittest.TestCase):
    def test_should_format_sizes(self):
        self.assertEqual(cli.fmt_kb(None), "-")
        self.assertEqual(cli.fmt_kb(512), "512 KB")
        self.assertEqual(cli.fmt_kb(2048), "2.0 MB")
        self.assertEqual(cli.fmt_kb(3 * 1024 * 1024), "3.0 GB")


class StatusTests(CliTestCase):
    def test_should_show_direct_reading_without_watchdog(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("level:      ok  [direct]", out)
        self.assertIn("watchdog:   not running", out)
        self.assertIn("frozen:     nothing", out)

    def test_should_show_watchdog_view(self):
        self.hold_watchdog_lock()
        self.proc.osrelease("6.18.33.2-microsoft-standard-WSL2")
        common.atomic_write_json(pressure.status_path(), {
            "ts": time.time(), "pid": 4242, "level": "critical", "reasons": ["psi_memory_full=11.0"],
            "metrics": {"psi_memory_full": 11.0, "swap_free_pct": None},
            "host": {"total_kb": 32 * 1024 * 1024, "available_kb": 3 * 1024 * 1024,
                     "commit_total_kb": 58 * 1024 * 1024, "commit_free_kb": 6 * 1024 * 1024}})
        actions.save_frozen({"sessions": {"200-600": {"procs": [], "containers": []}}})
        self.user_config({"gate": False})
        _, out, _ = self.run_cli("status")
        self.assertIn("level:      critical (psi_memory_full=11.0)  [watchdog]", out)
        self.assertIn("host:       3.0 GB available of 32.0 GB, commit 6.0 GB free of 58.0 GB", out)
        self.assertIn("watchdog:   running (pid 4242", out)
        self.assertIn("freeze:     observe, gate off", out)
        self.assertIn("frozen:     200-600", out)
        self.assertIn("wsl:        WSL2", out)


class SessionsTests(CliTestCase):
    def test_should_list_sessions_with_flags_and_work(self):
        _, out, _ = self.run_cli("sessions")
        lines = out.splitlines()
        self.assertTrue(lines[0].lstrip().startswith("PID"))
        self.assertIn("--i", lines[1])
        self.assertIn("1p 10.0 MB", lines[1])
        self.assertIn("F-b", lines[2])
        self.assertIn("unattributed containers: 0", out)

    def test_should_emit_json_with_containers(self):
        with FakeEngine(self.tmp / "d.sock", [container("c1", "suite", labels=LABELS), container("c2", "mcp")]) as e:
            os.environ["DOCKER_HOST"] = f"unix://{e.socket_path}"
            code, out, _ = self.run_cli("sessions", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["sessions"][0]["containers"], [{"name": "suite", "state": "running", "mem_kb": 0}])
        self.assertEqual(data["unattributed_containers"], ["mcp"])
        self.assertFalse(data["sessions"][0]["foreground"])

    def test_should_count_session_servers_in_the_baseline(self):
        cgroup = self.sys_root / "fs/cgroup/docker/c1"
        cgroup.mkdir(parents=True)
        (cgroup / "memory.current").write_text(str(300 * 1024 * 1024))
        server = dict(LABELS, **{"dev.claude.role": "server"})
        with FakeEngine(self.tmp / "d.sock", [container("c1", "sonarqube", labels=server)]) as e:
            os.environ["DOCKER_HOST"] = f"unix://{e.socket_path}"
            _, raw, _ = self.run_cli("sessions", "--json")
            _, out, _ = self.run_cli("sessions")
        row = json.loads(raw)["sessions"][0]
        self.assertEqual((row["containers"], row["server_kb"]), ([], 300 * 1024))
        self.assertEqual(row["servers"], [{"name": "sonarqube", "state": "running", "mem_kb": 300 * 1024}])
        self.assertEqual(row["baseline_kb"], row["tree_kb"] - row["work_kb"] + 300 * 1024)
        self.assertIn("sonarqube(300.0 MB, server)", out)
        self.assertIn("unattributed containers: 0", out)

    def test_should_report_docker_errors(self):
        with FakeEngine(self.tmp / "d.sock", []) as e:
            e.status_override[("GET", "/containers/json")] = 500
            os.environ["DOCKER_HOST"] = f"unix://{e.socket_path}"
            _, out, _ = self.run_cli("sessions")
        self.assertIn("docker: unavailable (list returned 500)", out)


class FreezeResumeTests(CliTestCase):
    def test_should_freeze_by_pid_and_resume_by_pid(self):
        code, out, _ = self.run_cli("freeze", "100")
        self.assertEqual((code, out.strip()), (0, "froze 100-500: 1 processes, containers: none"))
        self.assertEqual(self.logged_signals(), [(301, 19)])
        code, out, _ = self.run_cli("resume", "100")
        self.assertIn("resumed 100-500: 1 processes", out)

    def test_should_freeze_others_but_not_foreground(self):
        _, out, _ = self.run_cli("freeze", "--others")
        self.assertIn("froze 100-500", out)
        self.assertNotIn("200-600", out)

    def test_should_freeze_by_session_id_prefix_and_resume_all(self):
        self.run_cli("freeze", "aaaa")
        _, out, _ = self.run_cli("resume", "--all")
        self.assertIn("resumed 100-500", out)
        _, out, _ = self.run_cli("resume", "--all")
        self.assertEqual(out.strip(), "nothing frozen")

    def test_should_report_unknown_targets(self):
        self.assertEqual(self.run_cli("freeze", "nope")[0], 1)
        self.assertEqual(self.run_cli("resume", "100")[0], 1)
        self.assertEqual(self.run_cli("stop", "nope")[0], 1)

    def test_should_say_when_nothing_to_freeze(self):
        sessions.touch_prompt("100-500", ts=time.time() + 100)
        sessions.touch_prompt("200-600", ts=time.time() + 50)
        self.assertEqual(self.run_cli("freeze", "--others")[1].strip(), "nothing to freeze")

    def test_should_refuse_to_freeze_or_stop_its_own_session(self):
        os.environ["CLAUDE_PID"] = "100"
        code, _, err = self.run_cli("freeze", "100")
        self.assertEqual(code, 1)
        self.assertIn("refusing to freeze the session this command runs in", err)
        self.assertIn("refusing to stop", self.run_cli("stop", "100", "--yes")[2])
        self.assertEqual(self.logged_signals(), [])
        self.assertIn("froze 100-500: 1 processes", self.run_cli("freeze", "100", "--include-self")[1])

    def test_should_leave_its_own_session_out_of_freeze_others(self):
        os.environ["CLAUDE_PID"] = "100"
        self.assertEqual(self.run_cli("freeze", "--others")[1].strip(), "nothing to freeze")

    def test_should_never_count_its_own_shell_as_work(self):
        me = os.getpid()
        self.proc.add(me, ppid=301, start=900, environ=ENV100)
        self.run_cli("freeze", "100")
        self.assertEqual(self.logged_signals(), [])

    def test_should_explain_lock_contention(self):
        with mock.patch.object(actions, "freeze_session", side_effect=common.LockTimeout("x")):
            code, _, err = self.run_cli("freeze", "100")
        self.assertEqual(code, 2)
        self.assertIn("holds the lock", err)


class StopTests(CliTestCase):
    def test_should_require_yes(self):
        code, out, _ = self.run_cli("stop", "100")
        self.assertEqual(code, 1)
        self.assertIn("would stop 1 processes", out)
        self.assertEqual(self.logged_signals(), [])

    def test_should_terminate_work_and_stop_owned_containers(self):
        items = [container("c1", "suite", state="paused", labels=LABELS)]
        with FakeEngine(self.tmp / "d.sock", items) as e:
            os.environ["DOCKER_HOST"] = f"unix://{e.socket_path}"
            code, out, _ = self.run_cli("stop", "100", "--yes")
            self.assertEqual(items[0]["State"], "exited")
        self.assertEqual(code, 0)
        self.assertEqual(self.logged_signals(), [(301, 15), (301, 18), (301, 9)])
        self.assertIn("containers: suite", out)

    def test_should_keep_the_session_s_servers_running_on_stop(self):
        items = [container("c1", "sonarqube", labels=dict(LABELS, **{"dev.claude.role": "server"}))]
        with FakeEngine(self.tmp / "d.sock", items) as e:
            os.environ["DOCKER_HOST"] = f"unix://{e.socket_path}"
            code, out, _ = self.run_cli("stop", "100", "--yes")
            self.assertEqual(items[0]["State"], "running")
        self.assertEqual(code, 0)
        self.assertIn("containers: none", out)


class DoctorTests(CliTestCase):
    def test_should_pass_on_plain_linux(self):
        self.proc.psi("memory")
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("[ ok ] platform: Linux", out)
        self.assertIn("[ ok ] PSI available", out)
        self.assertIn("[warn] no docker daemon socket found", out)

    def test_should_fail_on_wsl1_and_flag_missing_psi(self):
        self.proc.osrelease("4.4.0-19041-Microsoft")
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("[fail] WSL1", out)
        self.assertIn("no /proc/pressure", out)

    def shim_paths(self, *paths):
        for pid, start, path in zip((100, 200), (500, 600), paths):
            self.claude(pid, start=start, environ=None if path is None else {"PATH": path})

    def test_should_ask_for_the_shims_on_claude_s_own_path(self):
        self.shim_paths("/usr/bin:/mnt/c/Windows", "/usr/bin")
        with mock.patch.dict(os.environ, {"SHELL": "/usr/bin/zsh", "HOME": str(self.tmp)}):
            _, out, _ = self.run_cli("doctor")
        self.assertIn("[warn] 2 of 2 sessions start MCP/LSP servers without the shims", out)
        shims = str(common.state_dir() / "shims")
        self.assertIn(f'add `export PATH="$HOME{shims[len(str(self.tmp)):]}:$PATH"` to ~/.zshrc, then restart Claude Code',
                      out)

    def test_should_accept_the_stable_link_or_any_shim_directory(self):
        marked = self.tmp / "elsewhere"
        marked.mkdir()
        (marked / ".resource-guard-shim").write_text("")
        os.symlink(common.plugin_root() / "shims", common.state_dir() / "shims")
        self.shim_paths(f"{common.state_dir() / 'shims'}:/usr/bin", f"relative:{marked}:/usr/bin")
        _, out, _ = self.run_cli("doctor")
        self.assertIn("[ ok ] session MCP/LSP containers are labeled and capped: shims on PATH in all 2 sessions", out)
        self.assertNotIn("doesn't lead to the shims", out)

    def test_should_flag_a_dangling_shims_link(self):
        os.symlink(self.tmp / "removed-version" / "shims", common.state_dir() / "shims")
        self.shim_paths(f"{common.state_dir() / 'shims'}:/usr/bin", f"{common.state_dir() / 'shims'}")
        _, out, _ = self.run_cli("doctor")
        self.assertIn(f"[warn] {common.state_dir() / 'shims'} doesn't lead to the shims", out)
        self.assertIn("[warn] 2 of 2 sessions start MCP/LSP servers without the shims", out)

    def test_should_name_an_absolute_path_and_any_shell_rc(self):
        self.shim_paths("/usr/bin", None)
        with mock.patch.dict(os.environ, {"SHELL": "/usr/bin/fish", "HOME": "/nowhere"}):
            _, out, _ = self.run_cli("doctor")
        self.assertIn("[warn] 1 of 1 sessions", out)
        self.assertIn(f'`export PATH="{common.state_dir() / "shims"}:$PATH"` to your shell rc file', out)

    def test_should_skip_the_shims_check_without_a_readable_environ(self):
        _, out, _ = self.run_cli("doctor")
        self.assertNotIn("shims", out)

    def test_should_fail_off_linux(self):
        with mock.patch.object(cli.sys, "platform", "darwin"):
            self.assertEqual(self.run_cli("doctor")[0], 1)

    def test_should_flag_bypassed_shim_and_give_wsl_advice(self):
        self.proc.add(302, ppid=100, start=532, environ=ENV100, cmdline=("docker", "run", "img"))
        self.proc.osrelease("6.18.33.2-microsoft-standard-WSL2")
        cfg_path = self.tmp / "w.wslconfig"
        cfg_path.write_text("[wsl2]\nmemory=16GB\nswap=12GB\n")
        os.environ["RESOURCE_GUARD_WSLCONFIG"] = str(cfg_path)
        with FakeEngine(self.tmp / "d.sock", [container("c9", "unlabeled")]) as e:
            os.environ["DOCKER_HOST"] = f"unix://{e.socket_path}"
            _, out, _ = self.run_cli("doctor")
        self.assertIn("the shim is bypassed", out)
        self.assertIn("autoMemoryReclaim=gradual", out)
        link = common.state_dir() / "bin" / "resource-guard"
        self.assertIn(f"[info] escape hatch from PowerShell: wsl.exe -d <distro> -e {link} resume --all", out)

    def test_should_report_unreachable_docker(self):
        with FakeEngine(self.tmp / "d.sock", []) as e:
            e.status_override[("GET", "/containers/json")] = 500
            os.environ["DOCKER_HOST"] = f"unix://{e.socket_path}"
            _, out, _ = self.run_cli("doctor")
        self.assertIn("docker unreachable", out)


class WatchdogCommandTests(CliTestCase):
    def test_should_start_and_report_status(self):
        with mock.patch.object(ctl, "ensure_watchdog", return_value="spawned") as ensure:
            self.assertEqual(self.run_cli("watchdog", "start")[1].strip(), "watchdog: spawned")
        ensure.assert_called_once_with(force=True)
        self.assertEqual(self.run_cli("watchdog", "status")[1].strip(), "watchdog: not running")
        self.hold_watchdog_lock()
        common.atomic_write_json(pressure.status_path(), {"ts": time.time() - 5})
        self.assertIn("watchdog: running, heartbeat 5s ago", self.run_cli("watchdog", "status")[1])

    def test_should_stop_only_a_real_watchdog(self):
        self.assertEqual(self.run_cli("watchdog", "stop")[1].strip(), "watchdog: not running")
        self.hold_watchdog_lock()
        self.proc.add(900, start=90, cmdline=("python3", "/x/scripts/watchdog.py"))
        common.atomic_write_json(pressure.status_path(), {"ts": time.time(), "pid": 900})
        _, out, _ = self.run_cli("watchdog", "stop")
        self.assertEqual(out.strip(), "watchdog: killed; frozen work resumed")
        self.assertEqual(self.logged_signals(), [(900, 15), (900, 9)])

    def test_should_report_clean_stop(self):
        self.proc.add(900, start=90, cmdline=("python3", "/x/scripts/watchdog.py"))
        common.atomic_write_json(pressure.status_path(), {"ts": time.time(), "pid": 900})
        with mock.patch.object(ctl, "alive", side_effect=[True, False]):
            self.assertIn("stopped", self.run_cli("watchdog", "stop")[1])


if __name__ == "__main__":
    unittest.main()
