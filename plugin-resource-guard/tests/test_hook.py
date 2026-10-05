"""The hook entrypoint, run in-process with stdin/stdout swapped."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shlex
import time
import unittest
from unittest import mock

from fixtures import SandboxTestCase

import resource_guard_hook as hook
import rg_actions as actions
import rg_common as common
import rg_gate as hook_gate
import rg_pressure as pressure
import rg_sessions as sessions
import rg_watchdog_ctl as ctl

ME, OTHER = "100-500", "200-600"


class HookTestCase(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.claude(100, start=500)
        self.claude(200, start=600)
        self.cc_session(100, 500, cwd="/w/mine")
        self.cc_session(200, 600, cwd="/w/other")
        os.environ["CLAUDE_PID"] = "100"
        self.user_config({"gate_wait_seconds": 0.3, "gate_poll_seconds": 0.1})
        # Pretend a watchdog runs, so no test spawns a real one.
        fd = os.open(str(ctl.lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)

    def run_hook(self, payload, raw=None):
        stdin = io.StringIO(raw if raw is not None else json.dumps(payload))
        stdout = io.StringIO()
        with mock.patch("sys.stdin", stdin), contextlib.redirect_stdout(stdout):
            self.assertEqual(hook.main(), 0)
        text = stdout.getvalue().strip()
        return json.loads(text) if text else None

    def level(self, level, reasons=("mem_available_pct=8.0",)):
        common.atomic_write_json(pressure.status_path(), {"ts": time.time() + 3600, "level": level,
                                                          "reasons": list(reasons)})

    def prompt(self, key, ts):
        sessions.touch_prompt(key, ts=ts)


class EntryTests(HookTestCase):
    def test_should_ignore_bad_input(self):
        self.assertIsNone(self.run_hook(None, raw="not json"))
        self.assertIsNone(self.run_hook(None, raw="[1]"))
        self.assertIsNone(self.run_hook(None, raw=""))
        self.assertIsNone(self.run_hook({"hook_event_name": "Unknown"}))

    def test_should_do_nothing_when_disabled(self):
        os.environ["RESOURCE_GUARD_DISABLE"] = "1"
        self.level("critical")
        self.assertIsNone(self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "Agent"}))

    def test_should_do_nothing_on_unsupported_platform(self):
        with mock.patch.object(hook.sys, "platform", "darwin"):
            self.assertIsNone(self.run_hook({"hook_event_name": "SessionStart"}))
        self.proc.osrelease("4.4.0-19041-Microsoft")
        self.assertFalse(hook.supported())

    def test_should_fail_open_and_log_errors(self):
        with mock.patch.dict(hook.HANDLERS, {"SessionStart": mock.Mock(side_effect=RuntimeError("boom"))}):
            self.assertIsNone(self.run_hook({"hook_event_name": "SessionStart"}))
        self.assertIn("RuntimeError: boom", (common.state_dir() / "hook-errors.log").read_text())


class SessionStartTests(HookTestCase):
    def test_should_put_shim_on_path_once_and_link_cli(self):
        env_file = self.tmp / "env.sh"
        os.environ["CLAUDE_ENV_FILE"] = str(env_file)
        self.run_hook({"hook_event_name": "SessionStart", "source": "startup"})
        self.run_hook({"hook_event_name": "SessionStart", "source": "resume"})
        lines = env_file.read_text().splitlines()
        self.assertEqual(lines, [f'export PATH={shlex.quote(str(common.plugin_root() / "shims"))}:"$PATH"'])
        link = common.state_dir() / "bin" / "resource-guard"
        self.assertEqual(os.readlink(link), str(common.plugin_root() / "scripts" / "resource_guard.py"))

    def test_should_keep_a_stable_shims_link_for_claude_s_own_path(self):
        link = common.state_dir() / "shims"
        os.symlink(self.tmp / "old-version" / "shims", link)
        self.run_hook({"hook_event_name": "SessionStart", "source": "startup"})
        self.assertEqual(os.readlink(link), str(common.plugin_root() / "shims"))
        self.assertTrue((link / ".resource-guard-shim").exists())

    def test_should_leave_a_real_directory_where_the_link_goes(self):
        (common.state_dir() / "shims").mkdir()
        (common.state_dir() / "shims" / "mine").write_text("x")
        self.run_hook({"hook_event_name": "SessionStart", "source": "startup"})
        self.assertFalse((common.state_dir() / "shims").is_symlink())
        self.assertEqual(os.listdir(common.state_dir() / "shims"), ["mine"])
        self.assertEqual([p for p in os.listdir(common.state_dir()) if p.endswith(".tmp")], [])

    def test_should_write_shim_conf_from_server_caps(self):
        self.run_hook({"hook_event_name": "SessionStart", "source": "startup"})
        lines = (common.state_dir() / "shim.conf").read_text().splitlines()
        self.assertEqual([line for line in lines if not line.startswith("#")],
                         ["image ghcr.io/rvben/rumdl* 512m", "image *mempalace* 3g", "default 2g"])

    def test_should_not_rewrite_an_unchanged_shim_conf(self):
        self.run_hook({"hook_event_name": "SessionStart", "source": "startup"})
        with mock.patch.object(common, "atomic_write_text") as write:
            self.run_hook({"hook_event_name": "SessionStart", "source": "resume"})
        write.assert_not_called()

    def test_should_be_quiet_when_calm_and_uncrowded(self):
        self.proc.meminfo(total=100, available=60)
        self.assertIsNone(self.run_hook({"hook_event_name": "SessionStart"}))

    def test_should_warn_when_crowded_or_loaded(self):
        self.claude(300, start=700)
        self.cc_session(300, 700)
        self.level("elevated", ["host_available_pct=10.4"])
        out = self.run_hook({"hook_event_name": "SessionStart"})
        self.assertIn("3 Claude sessions are running", out["systemMessage"])
        self.assertIn("load is elevated (host_available_pct=10.4)", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], out["systemMessage"])

    def test_should_stay_quiet_after_compaction(self):
        self.level("hard")
        self.assertIsNone(self.run_hook({"hook_event_name": "SessionStart", "source": "compact"}))

    def test_should_list_session_footprints_from_watchdog_status(self):
        self.claude(300, start=700)
        self.cc_session(300, 700)
        common.atomic_write_json(pressure.status_path(), {"ts": time.time(), "level": "ok", "reasons": [], "sessions": [
            {"key": ME, "cwd": "/w/mine", "tree_rss_kb": 512 * 1024},
            {"key": OTHER, "cwd": "/w/other/", "tree_rss_kb": 3 * 1024 * 1024}]})
        out = self.run_hook({"hook_event_name": "SessionStart"})
        self.assertIn("sessions by memory: other 3.0 GB, mine 512 MB", out["systemMessage"])

    def test_should_add_server_containers_to_a_session_s_footprint(self):
        self.claude(300, start=700)
        self.cc_session(300, 700)
        servers = [{"name": "sq", "role": "server", "mem_bytes": 3 * 1024 ** 3},
                   {"name": "suite", "role": "work", "mem_bytes": 9 * 1024 ** 3}, "junk"]
        common.atomic_write_json(pressure.status_path(), {"ts": time.time(), "level": "ok", "reasons": [], "sessions": [
            {"key": ME, "cwd": "/w/mine", "tree_rss_kb": 512 * 1024, "containers": servers},
            {"key": OTHER, "cwd": "/w/other/", "tree_rss_kb": 3 * 1024 * 1024}, "junk"]})
        out = self.run_hook({"hook_event_name": "SessionStart"})
        self.assertIn("sessions by memory: mine 3.5 GB, other 3.0 GB", out["systemMessage"])

    def test_should_mention_hung_watchdog(self):
        with mock.patch.object(ctl, "ensure_watchdog", return_value="hung"):
            out = self.run_hook({"hook_event_name": "SessionStart"})
        self.assertIn("watchdog has stopped reporting", out["systemMessage"])

    def test_should_mention_crash_looping_watchdog(self):
        with mock.patch.object(ctl, "ensure_watchdog", return_value="gave_up"):
            out = self.run_hook({"hook_event_name": "SessionStart"})
        self.assertIn("watchdog keeps crashing", out["systemMessage"])


class ShimConfTests(unittest.TestCase):
    def conf(self, caps):
        return [line for line in hook.shim_conf({"server_caps": caps}).splitlines() if not line.startswith("#")]

    def test_should_put_the_most_literal_glob_first(self):
        caps = {"default": "1g", "images": {"*": "4g", "mcp/*": "2g", "mcp/sonarqube:latest": "1536m"}}
        self.assertEqual(self.conf(caps), ["image mcp/sonarqube:latest 1536m", "image mcp/* 2g", "image * 4g",
                                           "default 1g"])

    def test_should_keep_none_and_drop_what_docker_would_refuse(self):
        caps = {"default": "5m", "images": {"a*": "none", "b*": "6m", "c*": "6291455", "d*": "lots", "e*": True,
                                            "f g": "1g", "": "1g", "h*": 1073741824, "i*": "2g\n", "j*": None}}
        # "2g\n" comes out trimmed: a newline never reaches shim.conf.
        self.assertEqual(self.conf(caps), ["image a* none", "image b* 6m", "image h* 1073741824", "image i* 2g"])

    def test_should_take_only_ascii_values_and_printable_globs(self):
        caps = {"images": {"a*": "\uff11\uff12g", "b*": "0512m", "c\x00*": "1g", "d\u00e9*": "1g", "e\t*": "1g",
                           "f*": "1G"}}
        self.assertEqual(self.conf(caps), ["image f* 1G"])

    def test_should_write_no_caps_when_server_caps_is_off(self):
        self.assertEqual(self.conf(False), [])
        self.assertEqual(self.conf({"default": "none", "images": []}), ["default none"])


class PromptTests(HookTestCase):
    def test_should_ignore_continuations(self):
        self.assertIsNone(self.run_hook({"hook_event_name": "UserPromptSubmit", "is_continuation": True}))
        self.assertEqual(sessions.prompt_times(), {})

    def test_should_record_prompt_and_stay_quiet(self):
        self.assertIsNone(self.run_hook({"hook_event_name": "UserPromptSubmit"}))
        self.assertIn(ME, sessions.prompt_times())

    def test_should_resume_own_frozen_work(self):
        self.proc.add(301, ppid=100, start=31)
        actions.save_frozen({"sessions": {ME: {"pid": 100, "start": 500, "procs": [[301, 31]], "containers": []}}})
        out = self.run_hook({"hook_event_name": "UserPromptSubmit"})
        self.assertIn("resumed this session's frozen work", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(actions.frozen_keys(), set())
        self.assertEqual(self.logged_signals(), [(301, 18)])

    def test_should_hand_resume_to_watchdog_when_lock_is_busy(self):
        actions.save_frozen({"sessions": {ME: {"pid": 100, "start": 500, "procs": [], "containers": []}}})
        with mock.patch.object(actions, "resume_session", side_effect=common.LockTimeout("x")):
            self.run_hook({"hook_event_name": "UserPromptSubmit"})
        self.assertEqual(len(list((common.state_dir() / "requests").glob("*-resume-100-500.json"))), 1)

    def test_should_report_what_happened_to_other_sessions_once(self):
        common.append_event("freeze", session=OTHER, procs=2, containers=["suite"])
        common.append_event("held-by-idle", level="critical")
        out = self.run_hook({"hook_event_name": "UserPromptSubmit"})
        self.assertIn("froze other (pid 200): 2 processes, suite", out["systemMessage"])
        self.assertIn("idle servers hold it", out["systemMessage"])
        self.assertIsNone(self.run_hook({"hook_event_name": "UserPromptSubmit"}))

    def test_should_describe_resumes_and_orphans(self):
        common.append_event("resume", session=OTHER)
        common.append_event("orphan-stopped", session="999-1", containers=["old"])
        message = self.run_hook({"hook_event_name": "UserPromptSubmit"})["systemMessage"]
        self.assertIn("resumed other (pid 200)", message)
        self.assertIn("stopped orphaned containers of pid 999: old", message)

    def test_should_skip_hook_outside_claude(self):
        os.environ["CLAUDE_PID"] = "999"
        with mock.patch.object(hook.os, "getppid", return_value=999):
            self.assertIsNone(self.run_hook({"hook_event_name": "UserPromptSubmit"}))

    def test_should_give_claude_the_same_news_and_flag_failures(self):
        common.append_event("unpause-failed", session=OTHER, containers=["suite"])
        with mock.patch.object(ctl, "ensure_watchdog", return_value="hung"):
            out = self.run_hook({"hook_event_name": "UserPromptSubmit"})
        self.assertIn("could not unpause suite of other (pid 200)", out["systemMessage"])
        self.assertIn("watchdog has stopped reporting", out["systemMessage"])
        self.assertIn("resource-guard since the last prompt: could not unpause",
                      out["hookSpecificOutput"]["additionalContext"])


class PreToolTests(HookTestCase):
    def pre(self, tool="Bash", **tool_input):
        return self.run_hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})

    def test_should_pass_light_and_relief_work_without_reading_load(self):
        self.level("hard")
        self.assertIsNone(self.pre(command="git status"))
        self.assertIsNone(self.pre(command="docker stop x"))

    def test_should_deny_heavy_work_in_background_session_at_critical(self):
        self.prompt(OTHER, ts=time.time())
        self.level("critical")
        out = self.pre(command="bundle exec rspec")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("critical: mem_available_pct=8.0", out["systemMessage"])

    def test_should_ask_in_foreground_at_critical(self):
        self.prompt(ME, ts=time.time())
        self.level("hard")
        self.assertEqual(self.pre("Agent")["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_should_pass_after_wait_when_load_drops(self):
        self.prompt(OTHER, ts=time.time())
        self.level("critical")
        with mock.patch.object(pressure, "current_level", side_effect=[("critical", [], "w"), ("elevated", [], "w")]):
            self.assertIsNone(self.pre("Workflow"))

    def test_should_soft_wait_then_pass_when_elevated(self):
        self.prompt(OTHER, ts=time.time())
        self.level("elevated")
        started = time.monotonic()
        self.assertIsNone(self.pre(command="npm test"))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)

    def test_should_stop_turn_after_deny_burst(self):
        self.prompt(OTHER, ts=time.time())
        self.level("critical")
        self.pre(command="make")
        self.pre(command="make")
        self.assertEqual(self.pre(command="make")["continue"], False)

    def test_should_cap_the_wait_below_the_hook_timeout(self):
        self.prompt(OTHER, ts=time.time())
        self.level("critical")
        self.user_config({"gate_wait_seconds": 100})
        with mock.patch.object(hook_gate, "wait_for_calm", return_value="ok") as wait:
            self.assertIsNone(self.pre(command="make"))
        self.assertEqual(wait.call_args[0][1], hook.MAX_GATE_WAIT)

    def test_should_pass_when_gate_is_off_or_session_unknown(self):
        self.level("hard")
        self.user_config({"gate": False})
        self.assertIsNone(self.pre("Agent"))
        self.user_config({"gate_wait_seconds": 0.1})
        os.environ.pop("CLAUDE_PID")
        with mock.patch.object(hook.os, "getppid", return_value=1):
            self.assertEqual(self.pre("Agent")["hookSpecificOutput"]["permissionDecision"], "ask")


class PostToolTests(HookTestCase):
    def post(self):
        return self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Bash"})

    def test_should_tell_claude_once_about_its_freeze(self):
        self.proc.meminfo(total=100, available=60)
        common.append_event("freeze", session=ME, procs=1, containers=[])
        common.append_event("resume", session=ME)
        common.append_event("freeze", session=OTHER, procs=1, containers=[])
        context = self.post()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("froze this session's running Bash work", context)
        self.assertIn("resumed this session's frozen work", context)
        self.assertIsNone(self.post())

    def test_should_nudge_when_elevated_at_most_every_interval(self):
        self.level("elevated", ["psi_io_full=22.0"])
        self.assertIn("machine load is elevated (psi_io_full=22.0)",
                      self.post()["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.post())

    def test_should_skip_outside_claude(self):
        os.environ.pop("CLAUDE_PID")
        with mock.patch.object(hook.os, "getppid", return_value=1):
            self.assertIsNone(self.post())

    def test_should_run_the_dead_man_switch_mid_turn(self):
        with mock.patch.object(ctl, "ensure_watchdog", return_value="running") as ensure:
            self.post()
        ensure.assert_called_once_with()


class SessionEndTests(HookTestCase):
    def test_should_only_leave_a_request(self):
        self.assertIsNone(self.run_hook({"hook_event_name": "SessionEnd"}))
        self.assertEqual(len(list((common.state_dir() / "requests").glob("*-end-100-500.json"))), 1)


if __name__ == "__main__":
    unittest.main()
