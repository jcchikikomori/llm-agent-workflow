#!/usr/bin/env python3
"""Tests for the commit-guard GUI-pinentry bypass and per-project exception.

Run with either:

  python3 -m unittest discover -s plugin-commit-guard/tests
  python3 -m pytest plugin-commit-guard/tests

End-to-end tests run the real hook against a throwaway git repo. HOME, GNUPGHOME,
the state dir and git's global config are all pointed at temp dirs, so the
developer's own keys, exceptions and ~/.gitconfig never leak in.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK_DIR = Path(__file__).resolve().parents[1] / "hooks"
HOOK = HOOK_DIR / "commit_guard_hook.py"
EXCEPTION_SCRIPT = HOOK_DIR / "project_exception.py"

sys.path.insert(0, str(HOOK_DIR))
import commit_guard_hook as guard  # noqa: E402


def hit(subcommand, *args, cwd="/repo"):
    op = guard.classify(subcommand, list(args))
    return (op, cwd, None, None, subcommand, list(args))


class EditorFreeTest(unittest.TestCase):
    def test_commit_with_message_is_editor_free(self):
        for args in (["-m", "x"], ["-am", "x"], ["-F", "msg.txt"], ["--message=x"],
                     ["--amend", "--no-edit"], ["-C", "HEAD"], ["--fixup", "abc"]):
            with self.subTest(args=args):
                self.assertTrue(guard.editor_free("commit", args))

    def test_commit_needing_editor_is_not(self):
        for args in ([], ["--amend"], ["-m", "x", "-e"], ["-c", "HEAD"],
                     ["--fixup=reword:abc"], ["--fixup=amend:abc"]):
            with self.subTest(args=args):
                self.assertFalse(guard.editor_free("commit", args))

    def test_other_subcommands(self):
        cases = [
            ("tag", ["-s", "v1", "-m", "x"], True),
            ("tag", ["-s", "v1"], False),
            ("merge", ["--no-edit", "feature"], True),
            ("merge", ["feature"], False),
            ("merge", ["--continue"], False),
            ("cherry-pick", ["abc"], True),
            ("cherry-pick", ["--continue"], False),
            ("revert", ["--no-edit", "abc"], True),
            ("revert", ["abc"], False),
            ("rebase", ["main"], True),
            ("rebase", ["-i", "main"], False),
            ("rebase", ["--continue"], False),
            ("am", ["patch.mbox"], True),
            ("am", ["-i", "patch.mbox"], False),
            ("pull", ["--rebase"], True),
            ("pull", ["--rebase=interactive"], False),
            ("pull", ["--no-ff"], False),
            ("status", [], False),
        ]
        for subcommand, args, expected in cases:
            with self.subTest(subcommand=subcommand, args=args):
                self.assertEqual(guard.editor_free(subcommand, args), expected)


class GuiPinentryTest(unittest.TestCase):
    def test_linux_gui_needs_a_display(self):
        self.assertTrue(guard.gui_pinentry_usable(
            "/usr/bin/pinentry-gnome3", {"DISPLAY": ":0"}, "linux"))
        self.assertTrue(guard.gui_pinentry_usable(
            "/usr/bin/pinentry-qt", {"WAYLAND_DISPLAY": "wayland-0"}, "linux"))
        self.assertFalse(guard.gui_pinentry_usable(
            "/usr/bin/pinentry-gnome3", {}, "linux"))

    def test_tty_flavours_never_count(self):
        for program in ("/usr/bin/pinentry-tty", "/usr/bin/pinentry-curses",
                        "/usr/bin/pinentry-emacs", "/usr/bin/pinentry", None, ""):
            with self.subTest(program=program):
                self.assertFalse(guard.gui_pinentry_usable(
                    program, {"DISPLAY": ":0"}, "linux"))

    def test_wsl_windows_pinentry(self):
        self.assertTrue(guard.gui_pinentry_usable(
            "/mnt/c/Program Files (x86)/GnuPG/bin/pinentry-basic.exe", {}, "linux"))
        self.assertTrue(guard.gui_pinentry_usable(
            "/home/u/bin/pinentry-wsl-ps1.sh", {}, "linux"))
        self.assertFalse(guard.gui_pinentry_usable(
            "/mnt/c/tools/pinentry-tty.exe", {}, "linux"))

    def test_native_windows(self):
        self.assertTrue(guard.gui_pinentry_usable(
            "C:\\Program Files (x86)\\GnuPG\\bin\\pinentry.exe", {}, "win32"))

    def test_macos(self):
        self.assertTrue(guard.gui_pinentry_usable(
            "/opt/homebrew/bin/pinentry-mac", {}, "darwin"))
        self.assertFalse(guard.gui_pinentry_usable(
            "/opt/homebrew/bin/pinentry-mac", {"SSH_CONNECTION": "1 2 3 4"}, "darwin"))
        # Homebrew's plain `pinentry` is curses.
        self.assertFalse(guard.gui_pinentry_usable(
            "/opt/homebrew/bin/pinentry", {}, "darwin"))


class PinentryProgramTest(unittest.TestCase):
    def test_agent_conf_wins_and_symlink_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "pinentry-gnome3"
            real.write_text("")
            link = Path(tmp) / "pinentry"
            link.symlink_to(real)
            (Path(tmp) / "gpg-agent.conf").write_text(
                "# comment\npinentry-program {0}\npinentry-timeout 60\n".format(link))
            with mock.patch.dict(os.environ, {"GNUPGHOME": tmp}):
                self.assertEqual(guard.pinentry_program(), str(real))

    def test_falls_back_to_gpgconf(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = mock.Mock(stdout="gpg:OpenPGP:/usr/bin/gpg\n"
                                    "pinentry:Passphrase Entry:/opt/pinentry-qt\n")
            with mock.patch.dict(os.environ, {"GNUPGHOME": tmp}), \
                    mock.patch.object(guard.subprocess, "run", return_value=fake):
                self.assertEqual(guard.pinentry_program(), "/opt/pinentry-qt")

    def test_no_gpgconf_means_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"GNUPGHOME": tmp}), \
                    mock.patch.object(guard.subprocess, "run", side_effect=OSError):
                self.assertIsNone(guard.pinentry_program())


class ExceptionFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(guard, "EXCEPTIONS_DIR", Path(self.tmp.name) / "exc")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, project, payload):
        path = guard.exception_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))

    def test_round_trip(self):
        project = os.path.realpath(self.tmp.name)
        self.assertIsNone(guard.read_exception(project))
        self.write(project, {"project": project, "decision": "allow"})
        self.assertEqual(guard.read_exception(project), "allow")

    def test_mismatched_project_is_ignored(self):
        project = os.path.realpath(self.tmp.name)
        self.write(project, {"project": "/somewhere/else", "decision": "allow"})
        self.assertIsNone(guard.read_exception(project))

    def test_garbage_is_ignored(self):
        project = os.path.realpath(self.tmp.name)
        for payload in ("not json", "[]", {"project": project, "decision": "yes"}):
            with self.subTest(payload=payload):
                self.write(project, payload)
                self.assertIsNone(guard.read_exception(project))


class Repo:
    """Throwaway repo plus an isolated HOME / GNUPGHOME / state dir."""

    def __init__(self, tmp, gpgsign=True, pinentry="pinentry-gnome3"):
        self.tmp = Path(tmp)
        self.dir = self.tmp / "project"
        self.dir.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.gnupg = self.tmp / "gnupg"
        self.gnupg.mkdir()
        self.state = self.tmp / "state"
        program = self.tmp / pinentry
        program.write_text("")
        (self.gnupg / "gpg-agent.conf").write_text("pinentry-program {0}\n".format(program))
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "GNUPGHOME": str(self.gnupg),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "COMMIT_GUARD_MODE": "delegate",
            "COMMIT_GUARD_STATE_DIR": str(self.state),
            "COMMIT_GUARD_TOKEN_FILE": str(self.tmp / "token"),
            "DISPLAY": ":0",
        }
        self.git("init", "-q")
        if gpgsign:
            self.git("config", "commit.gpgsign", "true")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.dir, env=self.env, check=True,
                       capture_output=True)

    def hook(self, command, **env):
        payload = json.dumps({"tool_name": "Bash", "cwd": str(self.dir),
                              "tool_input": {"command": command}})
        return subprocess.run([sys.executable, str(HOOK)], input=payload, text=True,
                              capture_output=True, env={**self.env, **env})

    def record(self, *args):
        return subprocess.run([sys.executable, str(EXCEPTION_SCRIPT), "--dir",
                               str(self.dir), *args], text=True,
                              capture_output=True, env=self.env)


class EndToEndTest(unittest.TestCase):
    COMMAND = 'git commit -m "feat: x"'

    def repo(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Repo(tmp.name, **kwargs)

    def test_first_signed_commit_asks_once(self):
        repo = self.repo()
        result = repo.hook(self.COMMAND)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ASK ONCE FOR THIS PROJECT", result.stderr)
        self.assertIn("project_exception.py", result.stderr)
        self.assertIn("pinentry-gnome3", result.stderr)

    def test_allow_lets_it_through_and_persists(self):
        repo = self.repo()
        recorded = repo.record("--decision", "allow")
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        files = list((repo.state / "exceptions").glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text())
        self.assertEqual(data["decision"], "allow")
        self.assertEqual(data["project"], os.path.realpath(repo.dir))
        self.assertEqual(repo.hook(self.COMMAND).returncode, 0)
        # Persisted, not single-use: the next commit also runs.
        self.assertEqual(repo.hook('git commit -m "fix: y"').returncode, 0)

    def test_allow_from_a_subdirectory_keys_on_the_repo_root(self):
        repo = self.repo()
        (repo.dir / "sub").mkdir()
        subprocess.run([sys.executable, str(EXCEPTION_SCRIPT), "--dir",
                        str(repo.dir / "sub"), "--decision", "allow"],
                       check=True, capture_output=True, env=repo.env)
        self.assertEqual(repo.hook(self.COMMAND).returncode, 0)

    def test_delegate_decision_hands_off_without_asking_again(self):
        repo = self.repo()
        repo.record("--decision", "delegate")
        result = repo.hook(self.COMMAND)
        self.assertEqual(result.returncode, 2)
        self.assertIn("DELEGATED", result.stderr)
        self.assertNotIn("ASK ONCE", result.stderr)

    def test_editor_commands_always_delegate_even_when_allowed(self):
        repo = self.repo()
        repo.record("--decision", "allow")
        for command in ("git commit", "git commit --amend", "git rebase -i HEAD~2"):
            with self.subTest(command=command):
                result = repo.hook(command)
                self.assertEqual(result.returncode, 2)
                self.assertIn("DELEGATED", result.stderr)

    def test_tty_pinentry_delegates(self):
        repo = self.repo(pinentry="pinentry-tty")
        repo.record("--decision", "allow")
        result = repo.hook(self.COMMAND)
        self.assertEqual(result.returncode, 2)
        self.assertIn("DELEGATED", result.stderr)

    def test_gui_pinentry_without_display_delegates(self):
        repo = self.repo()
        repo.record("--decision", "allow")
        env = dict(repo.env)
        env.pop("DISPLAY")
        repo.env = env
        result = repo.hook(self.COMMAND)
        self.assertEqual(result.returncode, 2)
        self.assertIn("DELEGATED", result.stderr)

    def test_unsigned_repo_is_unchanged(self):
        repo = self.repo(gpgsign=False)
        repo.record("--decision", "allow")
        result = repo.hook(self.COMMAND)
        self.assertEqual(result.returncode, 2)
        self.assertIn("DELEGATED", result.stderr)

    def test_explicit_dash_s_counts_as_signing(self):
        repo = self.repo(gpgsign=False)
        repo.record("--decision", "allow")
        self.assertEqual(repo.hook('git commit -S -m "x"').returncode, 0)

    def test_ssh_signing_is_out_of_scope(self):
        repo = self.repo()
        repo.git("config", "gpg.format", "ssh")
        repo.record("--decision", "allow")
        self.assertIn("DELEGATED", repo.hook(self.COMMAND).stderr)

    def test_pinentry_off_disables_the_bypass(self):
        repo = self.repo()
        repo.record("--decision", "allow")
        result = repo.hook(self.COMMAND, COMMIT_GUARD_PINENTRY="off")
        self.assertIn("DELEGATED", result.stderr)

    def test_forced_gui_skips_detection(self):
        repo = self.repo(pinentry="pinentry-tty")
        result = repo.hook(self.COMMAND, COMMIT_GUARD_PINENTRY="gui")
        self.assertIn("ASK ONCE", result.stderr)

    def test_deny_mode_never_asks(self):
        repo = self.repo()
        repo.record("--decision", "allow")
        result = repo.hook(self.COMMAND, COMMIT_GUARD_MODE="deny")
        self.assertEqual(result.returncode, 2)
        self.assertIn("DENIED", result.stderr)

    def test_allow_overrides_an_earlier_handoff_ledger(self):
        repo = self.repo()
        repo.record("--decision", "delegate")
        self.assertIn("DELEGATED", repo.hook(self.COMMAND).stderr)
        repo.record("--decision", "allow")
        self.assertEqual(repo.hook(self.COMMAND).returncode, 0)

    def test_remove_and_show(self):
        repo = self.repo()
        self.assertEqual(repo.record("--show").stdout.strip(), "unset")
        repo.record("--decision", "allow")
        self.assertEqual(repo.record("--show").stdout.strip(), "allow")
        repo.record("--remove")
        self.assertEqual(repo.record("--show").stdout.strip(), "unset")
        self.assertIn("ASK ONCE", repo.hook(self.COMMAND).stderr)

    def test_record_rejects_missing_dir(self):
        repo = self.repo()
        result = subprocess.run([sys.executable, str(EXCEPTION_SCRIPT), "--dir",
                                 str(repo.tmp / "nope"), "--decision", "allow"],
                                text=True, capture_output=True, env=repo.env)
        self.assertEqual(result.returncode, 64)

    def test_read_only_git_still_passes(self):
        repo = self.repo()
        self.assertEqual(repo.hook("git status").returncode, 0)


class OpaqueHeredocTest(unittest.TestCase):
    def test_heredoc_body_with_backticks_is_not_opaque(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            command = "cat > notes.md <<'EOF'\nuse `git commit` and $(date)\nEOF"
            self.assertEqual(repo.hook(command).returncode, 0)

    def test_real_opaque_git_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            result = repo.hook('$(which git) commit -m "x"')
            self.assertEqual(result.returncode, 2)
            self.assertIn("BLOCKED", result.stderr)


if __name__ == "__main__":
    unittest.main()
