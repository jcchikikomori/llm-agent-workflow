"""The POSIX sh docker shim, run for real against a fake docker binary that
prints the arguments it received."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fixtures import PLUGIN, FakeProc

SHIMS = PLUGIN / "shims"
FAKE = '#!/bin/sh\nprintf "%s" "${0##*/}"; for a do printf "\\n%s" "$a"; done; echo\n'


class ShimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rg-shim-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in ("docker", "docker-compose"):
            (self.bin / name).write_text(FAKE)
            (self.bin / name).chmod(0o755)
        # The shim walks up from $PPID, which is this test process: a fake
        # /proc decides what it finds there, never the real process tree.
        self.proc = FakeProc(self.tmp / "proc")
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.me = os.getpid()

    def run_shim(self, *args, tool="docker", claude_pid="4242", path=None, extra_env=None):
        env = {"PATH": path or f"{SHIMS}:{self.bin}:/usr/bin:/bin", "RESOURCE_GUARD_STATE_DIR": str(self.state),
               "HOME": str(self.tmp)}
        if claude_pid:
            # Bash work: the real /proc, so the client's own start time reads.
            env.update(CLAUDE_PID=claude_pid, CLAUDE_SESSION_ID="sid-1")
        else:
            env["RESOURCE_GUARD_PROC_ROOT"] = str(self.proc.root)
        env.update(extra_env or {})
        result = subprocess.run([str(SHIMS / tool), *args], env=env, capture_output=True, text=True, timeout=10)
        return result

    def as_claude(self, pid, ppid=1, start=777, node=False):
        if node:
            return self.proc.add(pid, ppid=ppid, start=start, comm="node", exe="/usr/bin/node",
                                 cmdline=("node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"))
        return self.proc.add(pid, ppid=ppid, start=start, comm="2.1.289", exe="/home/u/.local/share/claude/versions/2.1",
                             cmdline=("claude",))

    def caps(self, *lines):
        (self.state / "shim.conf").write_text("\n".join(lines) + "\n")

    def argv(self, *args, **kw):
        result = self.run_shim(*args, **kw)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.rstrip("\n").split("\n")
        return lines[0], lines[1:]

    def labels(self, argv):
        return {argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
                for i, arg in enumerate(argv) if arg == "--label"}

    def test_should_label_plain_run_right_after_subcommand(self):
        tool, argv = self.argv("run", "--rm", "img", "sh", "-c", 'echo "a b"')
        self.assertEqual(tool, "docker")
        self.assertEqual(argv[0], "run")
        self.assertEqual(argv[1], "--label")
        self.assertEqual(argv[-4:], ["img", "sh", "-c", 'echo "a b"'])
        labels = self.labels(argv)
        self.assertEqual(labels["dev.claude.pid"], "4242")
        self.assertEqual(labels["dev.claude.session"], "sid-1")
        self.assertTrue(labels["dev.claude.client"].isdigit())
        self.assertTrue(labels["dev.claude.client_start"].isdigit())

    def test_should_skip_global_flags_with_values(self):
        _, argv = self.argv("-l", "debug", "--context", "x", "run", "img")
        self.assertEqual(argv[:5], ["-l", "debug", "--context", "x", "run"])
        self.assertEqual(argv[5], "--label")

    def test_should_label_container_create_and_compose_run(self):
        _, argv = self.argv("container", "create", "img")
        self.assertEqual(argv[:3], ["container", "create", "--label"])
        _, argv = self.argv("compose", "-f", "a.yml", "-p", "proj", "run", "--rm", "app", "rspec")
        self.assertEqual(argv[:6], ["compose", "-f", "a.yml", "-p", "proj", "run"])
        self.assertEqual(argv[6], "--label")
        self.assertEqual(argv[-3:], ["--rm", "app", "rspec"])

    def test_should_label_standalone_docker_compose(self):
        tool, argv = self.argv("--project-directory", "/x", "run", "app", tool="docker-compose")
        self.assertEqual(tool, "docker-compose")
        self.assertEqual(argv[:4], ["--project-directory", "/x", "run", "--label"])

    def test_should_leave_other_commands_untouched(self):
        for args in (["ps", "-a"], ["compose", "up", "-d"], ["exec", "-it", "c", "run"], ["image", "run"]):
            _, argv = self.argv(*args)
            self.assertEqual(argv, args)

    def test_should_pass_through_outside_claude(self):
        _, argv = self.argv("run", "img", claude_pid="")
        self.assertEqual(argv, ["run", "img"])
        self.proc.add(self.me, ppid=9001, exe="/usr/bin/zsh")
        self.proc.add(9001, ppid=9002, exe="/usr/bin/tmux")
        self.proc.add(9002, ppid=9003, exe="/usr/bin/sshd")
        self.proc.add(9003, ppid=9004, exe="/usr/bin/sshd")
        self.as_claude(9004)  # five levels up: too far to be its server
        _, argv = self.argv("run", "img", claude_pid="")
        self.assertEqual(argv, ["run", "img"])

    def test_should_label_and_cap_a_session_server(self):
        self.as_claude(self.me, start=777)
        self.caps("default 2g", "image mcp/sonarqube* 1536m")
        _, argv = self.argv("run", "-i", "--rm", "-e", "SONARQUBE_TOKEN", "-v", "a:/b", "-p8080:80", "-dp", "81:81",
                            "--network", "host", "--name", "sq", "mcp/sonarqube", "--flag", "-m", "x", claude_pid="")
        labels = self.labels(argv)
        self.assertEqual((labels["dev.claude.role"], labels["dev.claude.pid"], labels["dev.claude.pid_start"]),
                         ("server", str(self.me), "777"))
        self.assertNotIn("dev.claude.session", labels)
        image = argv.index("mcp/sonarqube")
        self.assertEqual(argv[image - 2:image], ["--memory", "1536m"])
        self.assertEqual(argv[image:], ["mcp/sonarqube", "--flag", "-m", "x"])

    def test_should_use_the_default_cap_and_honour_none(self):
        self.as_claude(self.me)
        self.caps("default 2g", "image ghcr.io/rvben/rumdl* none")
        _, argv = self.argv("run", "-i", "--rm", "mempalace:gpu", claude_pid="")
        self.assertEqual(argv[-3:], ["--memory", "2g", "mempalace:gpu"])
        _, argv = self.argv("run", "--rm", "-i", "ghcr.io/rvben/rumdl:latest", "server", claude_pid="")
        self.assertNotIn("--memory", argv)
        self.assertEqual(self.labels(argv)["dev.claude.role"], "server")

    def test_should_keep_a_limit_the_server_config_sets(self):
        self.as_claude(self.me)
        self.caps("default 2g")
        for limit in (["-m", "1g"], ["--memory", "1g"], ["--memory=1g"], ["-m1g"]):
            _, argv = self.argv("run", *limit, "img", claude_pid="")
            self.assertEqual(argv.count("2g"), 0, limit)

    def test_should_keep_the_cap_before_end_of_options(self):
        self.as_claude(self.me)
        self.caps("default 512m", "image img 1g")
        _, argv = self.argv("container", "run", "--rm", "--", "img", "arg", claude_pid="")
        self.assertEqual(argv[-5:], ["--memory", "1g", "--", "img", "arg"])
        _, argv = self.argv("run", "--rm", "--", claude_pid="")
        self.assertEqual(argv[-2:], ["--rm", "--"])

    def test_should_read_short_flags_letter_by_letter(self):
        self.as_claude(self.me)
        self.caps("default 2g")
        for flags in (["-w/data"], ["-v/a:/data"], ["-ePATH=/usr/bin"], ["-it", "-w", "/data"], ["-itw/data"],
                      ["-dp", "81:81"]):
            _, argv = self.argv("run", *flags, "img", "node", "-m", "x", claude_pid="")
            self.assertEqual(argv[-6:], ["--memory", "2g", "img", "node", "-m", "x"], flags)

    def test_should_keep_a_limit_bundled_in_short_flags(self):
        self.as_claude(self.me)
        self.caps("default 2g")
        for flags in (["-dm", "512m"], ["-itm1g"], ["--memory-reservation", "3g"], ["--memory-swap=4g"]):
            _, argv = self.argv("run", *flags, "img", claude_pid="")
            self.assertNotIn("2g", argv, flags)

    def test_should_reject_cap_shapes_docker_would_refuse(self):
        self.as_claude(self.me)
        for value in ("0", "0512m", "g", "1g2", "kkk", "1gg", "none"):
            self.caps(f"default {value}")
            _, argv = self.argv("run", "img", claude_pid="")
            self.assertNotIn("--memory", argv, value)

    def test_should_pass_non_create_commands_straight_through(self):
        self.as_claude(self.me)
        self.caps("default 2g")
        _, argv = self.argv("ps", "--filter", "name=x", claude_pid="")
        self.assertEqual(argv, ["ps", "--filter", "name=x"])
        _, argv = self.argv("logs", "-f", "web")
        self.assertEqual(argv, ["logs", "-f", "web"])

    def test_should_reach_claude_through_a_wrapper_script(self):
        self.proc.add(self.me, ppid=9100, exe="/usr/bin/bash", cmdline=("bash", "/p/scripts/run-ruby-tool.sh"))
        self.as_claude(9100, node=True)
        self.caps("default 2g")
        _, argv = self.argv("compose", "run", "--rm", "-T", "app", "ruby-lsp", claude_pid="")
        labels = self.labels(argv)
        self.assertEqual((labels["dev.claude.role"], labels["dev.claude.pid"]), ("server", "9100"))
        self.assertNotIn("--memory", argv)  # compose run has no --memory flag

    def test_should_treat_bash_work_that_dropped_claude_pid_as_unknown(self):
        self.proc.add(self.me, ppid=9200, exe="/usr/bin/zsh", environ={"CLAUDE_PID": "9200"})
        self.as_claude(9200)
        _, argv = self.argv("run", "img", claude_pid="")
        self.assertEqual(argv, ["run", "img"])

    def test_should_skip_caps_without_shim_conf_or_with_bad_values(self):
        self.as_claude(self.me)
        _, argv = self.argv("run", "img", claude_pid="")
        self.assertNotIn("--memory", argv)
        self.caps("default lots")
        _, argv = self.argv("run", "img", claude_pid="")
        self.assertNotIn("--memory", argv)

    def test_should_treat_a_hook_s_container_as_session_machinery(self):
        self.caps("default 2g")
        for key in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR"):
            _, argv = self.argv("run", "--rm", "mempalace", extra_env={key: "/p"})
            labels = self.labels(argv)
            self.assertEqual((labels["dev.claude.role"], labels["dev.claude.pid"]), ("server", "4242"), key)
            self.assertEqual(argv[-3:], ["--memory", "2g", "mempalace"], key)

    def test_should_cap_with_the_conf_session_start_writes(self):
        import resource_guard_hook as hook

        cfg = json.loads((PLUGIN / "config" / "defaults.json").read_text())
        (self.state / "shim.conf").write_text(hook.shim_conf(cfg))
        self.as_claude(self.me)
        for image, cap in (("mempalace:gpu", "3g"), ("ghcr.io/rvben/rumdl:0.1", "512m"), ("mcp/sonarqube", "2g")):
            _, argv = self.argv("run", "-i", "--rm", image, claude_pid="")
            self.assertEqual(argv[-3:], ["--memory", cap, image])

    def test_should_not_cap_bash_work(self):
        self.as_claude(self.me)
        self.caps("default 2g")
        _, argv = self.argv("run", "img")
        self.assertNotIn("--memory", argv)
        self.assertNotIn("dev.claude.role", self.labels(argv))

    def test_should_skip_other_shim_dirs_and_fail_without_real_binary(self):
        other = self.tmp / "other-shims"
        shutil.copytree(SHIMS, other)
        shutil.copytree(PLUGIN / "scripts", self.tmp / "scripts")
        tool, _ = self.argv("ps", path=f"{SHIMS}:{other}:{self.bin}")
        self.assertEqual(tool, "docker")
        result = self.run_shim("ps", path=f"{SHIMS}:{other}")
        self.assertEqual(result.returncode, 127)
        self.assertIn("found no real binary", result.stderr)

    def test_should_preserve_exit_code(self):
        (self.bin / "docker").write_text("#!/bin/sh\nexit 3\n")
        self.assertEqual(self.run_shim("run", "img").returncode, 3)

    @unittest.skipUnless(shutil.which("dash"), "dash not installed")
    def test_should_run_under_dash(self):
        env = {"PATH": f"{SHIMS}:{self.bin}:/usr/bin:/bin", "CLAUDE_PID": str(os.getpid())}
        script = PLUGIN / "scripts" / "docker-shim.sh"
        result = subprocess.run(["dash", str(script), "docker", "run", "img"], env=env, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"dev.claude.pid_start=", result.stdout)

    @unittest.skipUnless(shutil.which("dash"), "dash not installed")
    def test_should_cap_a_server_under_dash(self):
        self.as_claude(self.me)
        self.caps("default 2g")
        env = {"PATH": f"{SHIMS}:{self.bin}:/usr/bin:/bin", "RESOURCE_GUARD_PROC_ROOT": str(self.proc.root),
               "RESOURCE_GUARD_STATE_DIR": str(self.state), "HOME": str(self.tmp)}
        script = PLUGIN / "scripts" / "docker-shim.sh"
        result = subprocess.run(["dash", str(script), "docker", "run", "-i", "img"], env=env, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dev.claude.role=server\n-i\n--memory\n2g\nimg", result.stdout)


if __name__ == "__main__":
    unittest.main()
