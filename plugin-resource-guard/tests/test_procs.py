from __future__ import annotations

import os
import signal
import unittest

from fixtures import UID, SandboxTestCase

import rg_common as common
import rg_procs as procs

SESSION = 100
ENV = {"CLAUDE_PID": str(SESSION)}


class ParseStatTests(unittest.TestCase):
    def test_should_split_after_last_paren_when_comm_has_parens(self):
        text = "7 (a) (b) S 3 " + " ".join(["0"] * 17) + " 4242 0 10"
        stat = procs.parse_stat(text)
        self.assertEqual((stat["comm"], stat["ppid"], stat["start"]), ("a) (b", 3, 4242))
        self.assertEqual(stat["sid"], 0)
        self.assertEqual(stat["rss_kb"], 10 * procs.PAGE_KB)

    def test_should_reject_truncated_or_garbage_stat(self):
        self.assertIsNone(procs.parse_stat("garbage"))
        self.assertIsNone(procs.parse_stat("1 (x) S 0 1"))
        self.assertIsNone(procs.parse_stat("1 (x) S zz " + " ".join(["0"] * 20)))


class ScanTests(SandboxTestCase):
    def test_should_read_process_fields(self):
        self.proc.add(5, ppid=2, comm="ruby", start=77, cmdline=("ruby", "-e", "x"), exe="/usr/bin/ruby")
        proc = procs.read_proc(5)
        self.assertEqual((proc.ppid, proc.comm, proc.start, proc.uid), (2, "ruby", 77, UID))
        self.assertEqual(proc.cmdline, ["ruby", "-e", "x"])
        self.assertEqual(proc.exe, "/usr/bin/ruby")

    def test_should_skip_vanished_and_non_numeric_entries(self):
        self.proc.add(5)
        (self.proc.root / "self").mkdir()
        (self.proc.root / "9").mkdir()
        self.assertEqual(sorted(procs.scan()), [5])

    def test_should_return_empty_scan_for_missing_root(self):
        self.assertEqual(procs.scan(self.tmp / "absent"), {})

    def test_should_tolerate_missing_status_cmdline_and_exe(self):
        self.proc.add(6, exe=None)
        for name in ("status", "cmdline"):
            (self.proc.root / "6" / name).unlink()
        proc = procs.read_proc(6)
        self.assertEqual((proc.uid, proc.cmdline, proc.exe), (None, [], ""))

    def test_should_parse_environ(self):
        self.proc.add(5, environ={"A": "1", "CLAUDE_PID": "9"})
        self.assertEqual(procs.read_environ(5), {"A": "1", "CLAUDE_PID": "9"})
        self.assertEqual(procs.read_environ(404), {})


class ClaudeDetectionTests(SandboxTestCase):
    def proc_with(self, **kw):
        defaults = {"pid": 9, "ppid": 1, "comm": "x", "state": "S", "start": 1}
        defaults.update(kw)
        return procs.Proc(**defaults)

    def test_should_detect_native_and_npm_installs(self):
        self.assertTrue(procs.is_claude_exe(self.proc_with(exe="/home/u/.local/share/claude/versions/2.1.289")))
        self.assertTrue(procs.is_claude_exe(self.proc_with(cmdline=["claude"])))
        npm = self.proc_with(cmdline=["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])
        self.assertTrue(procs.is_claude_exe(npm))
        self.assertFalse(procs.is_claude_exe(self.proc_with(cmdline=["node", "server.js"], exe="/usr/bin/node")))

    def test_should_match_execpath_exactly(self):
        self.assertTrue(procs.is_claude_exe(self.proc_with(exe="/x/cc"), execpath="/x/cc"))

    def test_should_not_count_daemon_helpers_as_sessions(self):
        daemon = self.proc_with(cmdline=["/opt/claude/versions/1", "daemon", "run"])
        pty = self.proc_with(cmdline=["claude", "bg-pty-host", "--bg-pty-host"])
        self.assertFalse(procs.is_session_process(daemon))
        self.assertFalse(procs.is_session_process(pty))
        self.assertTrue(procs.is_session_process(self.proc_with(cmdline=["claude", "--resume"])))

    def test_should_walk_up_to_session_process(self):
        self.claude(SESSION)
        self.proc.add(200, ppid=SESSION, comm="sh")
        self.proc.add(201, ppid=200, comm="python3")
        self.assertEqual(procs.claude_ancestor(201).pid, SESSION)

    def test_should_return_none_without_claude_ancestor(self):
        self.proc.add(200, ppid=1)
        self.assertIsNone(procs.claude_ancestor(200))
        self.assertIsNone(procs.claude_ancestor(999))


class WorkPidTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.claude(SESSION)
        self.cfg = common.load_config()

    def scan(self):
        return procs.scan()

    def test_should_pick_bash_tree_parent_first(self):
        self.proc.add(300, ppid=SESSION, environ=ENV)
        self.proc.add(301, ppid=300, comm="rspec", environ=ENV)
        self.proc.add(302, ppid=301, comm="ruby", environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [300, 301, 302])

    def test_should_include_reparented_background_job(self):
        self.proc.add(400, ppid=1, comm="sleep", environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [400])

    def test_should_drop_detached_daemons_and_their_children(self):
        self.proc.add(410, ppid=118, comm="dbus-daemon", environ=ENV, sid=410)
        self.proc.add(411, ppid=410, comm="helper", environ=ENV, sid=410)
        self.proc.add(300, ppid=SESSION, environ=ENV, sid=300)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [300])

    def test_should_skip_mcp_servers_and_other_sessions(self):
        self.proc.add(310, ppid=SESSION, comm="node", environ={"CLAUDE_CODE_SESSION_ID": "x"})
        self.proc.add(311, ppid=1, comm="sh", environ={"CLAUDE_PID": "555"})
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [])

    def test_should_drop_nested_claude_and_its_subtree(self):
        self.proc.add(320, ppid=SESSION, environ=ENV)
        self.proc.add(321, ppid=320, comm="claude", cmdline=("claude", "-p"), environ=ENV,
                      exe="/opt/claude/versions/9.9.9")
        self.proc.add(322, ppid=321, comm="node", environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [320])

    def test_should_drop_never_freeze_commands_and_children(self):
        self.proc.add(330, ppid=SESSION, environ=ENV)
        self.proc.add(331, ppid=330, comm="git", cmdline=("git", "commit"), environ=ENV)
        self.proc.add(332, ppid=331, comm="gpg", environ=ENV)
        self.proc.add(333, ppid=330, comm="pinentry-curses", environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [330])

    def test_should_drop_hook_processes(self):
        hook = ("python3", "/home/u/.claude/plugins/cache/x/hooks/h.py")
        self.proc.add(340, ppid=SESSION, comm="python3", cmdline=hook, environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [])

    def test_should_drop_hooks_by_environment_wherever_they_live(self):
        hook_env = dict(ENV, CLAUDE_PLUGIN_ROOT="/src/plugin", CLAUDE_PROJECT_DIR="/w")
        self.proc.add(342, ppid=SESSION, comm="bash", cmdline=("bash", "/src/plugin/save.sh"), environ=hook_env)
        self.proc.add(343, ppid=342, comm="python3", environ=hook_env)
        self.proc.add(344, ppid=SESSION, environ=dict(ENV, CLAUDE_PROJECT_DIR="/w"))
        self.proc.add(345, ppid=SESSION, environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [345])

    def test_should_drop_processes_older_than_the_session(self):
        self.proc.add(346, ppid=1, start=400, environ=ENV)
        self.proc.add(347, ppid=SESSION, start=600, environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg, session_start=500), [347])
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [346, 347])

    def test_should_match_never_freeze_globs(self):
        self.proc.add(348, ppid=SESSION, comm="ssh-add", environ=ENV)
        self.proc.add(349, ppid=SESSION, comm="gpg2", environ=ENV)
        self.proc.add(352, ppid=SESSION, comm="rsync", environ=ENV)
        self.proc.add(353, ppid=352, comm="ssh", environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [])

    def test_should_skip_other_uid_self_and_init(self):
        self.proc.add(350, ppid=SESSION, environ=ENV, uid=UID + 1)
        self.proc.add(1, ppid=0, comm="init", environ=ENV)
        self.proc.add(351, ppid=SESSION, environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg, self_pids=(351,)), [])

    def test_should_survive_parent_cycles(self):
        self.proc.add(360, ppid=361, environ=ENV)
        self.proc.add(361, ppid=360, environ=ENV)
        self.assertEqual(procs.work_pids(self.scan(), SESSION, self.cfg), [360, 361])


class SelfChainTests(SandboxTestCase):
    def test_should_list_self_and_ancestors_up_to_init(self):
        me = os.getpid()
        self.proc.add(me, ppid=7000)
        self.proc.add(7000, ppid=7001)
        self.proc.add(7001, ppid=1)
        self.assertEqual(procs.self_and_ancestors(), (me, 7000, 7001))

    def test_should_stop_at_unreadable_ancestor(self):
        self.proc.add(os.getpid(), ppid=7002)
        self.assertEqual(procs.self_and_ancestors(), (os.getpid(), 7002))


class TreeTests(SandboxTestCase):
    def test_should_sum_tree_rss(self):
        self.claude(SESSION)
        self.proc.add(300, ppid=SESSION, rss_pages=10)
        self.proc.add(301, ppid=300, rss_pages=5)
        self.proc.add(302, ppid=1, rss_pages=99)
        expected = (1000 + 10 + 5) * procs.PAGE_KB
        self.assertEqual(procs.tree_rss_kb(procs.scan(), SESSION), expected)


class SignalTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.proc.add(700, start=55)

    def sender(self, pid, sig):
        self.sent.append((pid, sig))

    def test_should_signal_when_start_matches(self):
        self.assertTrue(procs.signal_verified(700, 55, signal.SIGSTOP, sender=self.sender))
        self.assertEqual(self.sent, [(700, signal.SIGSTOP)])

    def test_should_refuse_reused_pid(self):
        self.assertFalse(procs.signal_verified(700, 56, signal.SIGSTOP, sender=self.sender))
        self.assertEqual(self.sent, [])

    def test_should_refuse_init_and_self(self):
        import os

        self.assertFalse(procs.signal_verified(1, 55, signal.SIGSTOP, sender=self.sender))
        self.assertFalse(procs.signal_verified(os.getpid(), 55, signal.SIGSTOP, sender=self.sender))

    def test_should_log_instead_of_signalling_in_fake_mode(self):
        self.assertTrue(procs.signal_verified(700, 55, signal.SIGCONT))
        self.assertEqual(self.logged_signals(), [(700, int(signal.SIGCONT))])

    def test_should_report_failure_when_sender_raises(self):
        def gone(pid, sig):
            raise ProcessLookupError(pid)

        self.assertFalse(procs.signal_verified(700, 55, signal.SIGSTOP, sender=gone))


if __name__ == "__main__":
    unittest.main()
