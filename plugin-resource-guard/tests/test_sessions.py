from __future__ import annotations

import json
import unittest

from fixtures import SandboxTestCase

import rg_procs as procs
import rg_sessions as sessions


class CcSessionsTests(SandboxTestCase):
    def test_should_keep_live_sessions_with_matching_proc_start(self):
        self.claude(100, start=500)
        self.cc_session(100, 500, session_id="a", kind="bg", cwd="/w")
        found = sessions.cc_sessions()
        self.assertEqual([(s.key, s.session_id, s.kind, s.cwd) for s in found], [("100-500", "a", "bg", "/w")])

    def test_should_drop_dead_and_reused_pids(self):
        self.claude(100, start=501)
        self.cc_session(100, 500)
        self.cc_session(200, 500)
        self.assertEqual(sessions.cc_sessions(), [])

    def test_should_skip_malformed_session_files(self):
        (self.claude_home / "sessions" / "x.json").write_text(json.dumps({"pid": "nope"}))
        (self.claude_home / "sessions" / "y.json").write_text("{")
        self.assertEqual(sessions.cc_sessions(), [])

    def test_should_return_empty_without_sessions_dir(self):
        (self.claude_home / "sessions").rmdir()
        self.assertEqual(sessions.cc_sessions(), [])

    def test_should_add_unlisted_session_processes_from_scan(self):
        self.claude(100, start=500)
        self.claude(101, start=600)
        self.proc.add(102, comm="claude", cmdline=("claude", "-p"), exe="/opt/claude/versions/9.9.9",
                      environ={"CLAUDE_PID": "100"})
        self.proc.add(103, comm="claude", cmdline=("claude", "daemon", "run"), exe="/opt/claude/versions/9.9.9")
        self.cc_session(100, 500)
        found = sessions.cc_sessions(procs=procs.scan())
        self.assertEqual([(s.key, s.kind) for s in found], [("100-500", "interactive"), ("101-600", "unknown")])

    def test_should_attach_prompt_times(self):
        self.claude(100, start=500)
        self.cc_session(100, 500)
        sessions.touch_prompt("100-500", ts=42.0)
        self.assertEqual(sessions.cc_sessions()[0].last_prompt_at, 42.0)

    def test_should_ignore_corrupt_prompt_files(self):
        sessions.prompts_dir().mkdir(parents=True)
        (sessions.prompts_dir() / "1-2.json").write_text('{"ts": "x"}')
        self.assertEqual(sessions.prompt_times(), {})


class ForegroundTests(unittest.TestCase):
    def s(self, pid, prompt=0.0, kind="interactive", updated=0.0):
        return sessions.Session(pid=pid, start=1, kind=kind, last_prompt_at=prompt, updated_at=updated)

    def test_should_protect_latest_prompter(self):
        result = sessions.foreground([self.s(1, 100.0), self.s(2, 200.0)], now=400.0, grace=60)
        self.assertEqual(result, {"2-1"})

    def test_should_keep_previous_foreground_inside_grace(self):
        result = sessions.foreground([self.s(1, 100.0), self.s(2, 200.0)], now=230.0, grace=60)
        self.assertEqual(result, {"1-1", "2-1"})

    def test_should_fall_back_to_latest_updated_interactive(self):
        found = [self.s(1, updated=5.0), self.s(2, updated=9.0), self.s(3, kind="bg", updated=99.0)]
        self.assertEqual(sessions.foreground(found, now=10.0, grace=60), {"2-1"})

    def test_should_protect_nothing_without_interactive_sessions(self):
        self.assertEqual(sessions.foreground([self.s(3, kind="bg")], now=1.0, grace=60), set())


class OwnSessionTests(SandboxTestCase):
    def test_should_use_claude_pid_env(self):
        self.claude(100, start=500)
        self.assertEqual(sessions.own_session(env={"CLAUDE_PID": "100"}), (100, 500))

    def test_should_walk_ancestors_without_env(self):
        self.claude(100, start=500)
        self.proc.add(300, ppid=100)
        self.assertEqual(sessions.own_session(env={}, parent_pid=300), (100, 500))

    def test_should_fall_back_when_env_pid_is_dead(self):
        self.claude(100, start=500)
        self.proc.add(300, ppid=100)
        self.assertEqual(sessions.own_session(env={"CLAUDE_PID": "999"}, parent_pid=300), (100, 500))

    def test_should_return_none_outside_claude(self):
        self.proc.add(300, ppid=1)
        self.assertIsNone(sessions.own_session(env={}, parent_pid=300))


if __name__ == "__main__":
    unittest.main()
