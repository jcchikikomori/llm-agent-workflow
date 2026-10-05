"""The POSIX sh docker shim, run for real against a fake docker binary that
prints the arguments it received."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fixtures import PLUGIN

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

    def run_shim(self, *args, tool="docker", claude_pid="4242", path=None):
        env = {"PATH": path or f"{SHIMS}:{self.bin}:/usr/bin:/bin"}
        if claude_pid:
            env.update(CLAUDE_PID=claude_pid, CLAUDE_SESSION_ID="sid-1")
        result = subprocess.run([str(SHIMS / tool), *args], env=env, capture_output=True, text=True, timeout=10)
        return result

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


if __name__ == "__main__":
    unittest.main()
