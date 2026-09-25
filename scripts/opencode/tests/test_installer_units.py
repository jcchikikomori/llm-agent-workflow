#!/usr/bin/env python3
# Installer unit loop Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.5)
# Generated: 2026-09-25 (Task 1.3a) | fail-closed branches of the loop; the F8 journeys live in test_setup_opencode.py
"""Integration tests for the fail-closed branches of setup-opencode.sh's install and uninstall loops.

Each test runs the real installer in a subprocess, never on a TTY:

  bash setup-opencode.sh --global [--plugin ID]... [--force] [--allow-repo DIR]... [--uninstall]

Every branch that refuses a write or a delete has a case here that fails if the branch is removed: the tracker and
plan checks before any write, BLOCKED and FAILED units, the conflict rules, backups behind the guard, the guard
asked again right before each write, and the guarded uninstall.

  python3 -m unittest discover -s scripts/opencode/tests
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import unittest
from pathlib import Path

from _support import (REPO_ROOT, RUN_TIMEOUT_SECONDS, SETUP_SCRIPT, SKILLS_MD_ORIGIN, Sandbox, link_scope, run_convert,
                      run_setup, snapshot)

# Harness: one _support.Sandbox per test (or per subTest); the scope root is <sandbox home>/.config/opencode. Real git
# for every repo fixture. Where a test must change the world between `plan` and a write, a python3 test double in
# the sandbox bin/ runs an action script (or fails) right before the Nth `convert.py guard` call on a given path,
# then execs the real interpreter. One test puts an `mv` double in bin/ to make the dir swap fail. The TTY tests
# drive the installer through a pty (os.openpty) and end every answer script with EOF (^D), so a run that asks one
# question too many reads EOF instead of hanging.

EXIT_OK = 0
EXIT_FAILED = 1
TRACKER_NAME = ".opencode-setup-tracker"
NO_VALUE = "-"
DUMMY_HASH = "sha256:" + "1" * 64
DOTFILES_CLEAN_ORIGIN = "https://example.invalid/dotfiles.git"
TTY_TIMEOUT_SECONDS = 60
EOF_KEYS = b"\x04" * 4
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
PAYLOAD_DIR = "llm-agent-workflow/commit-guard"
TS_REL = "plugins/opencode-commit-guard.ts"
COMMAND_REL = "commands/commit-guard.md"
GH_COMMAND_REL = "commands/gh-issue-to-pr.md"
COMMIT_GUARD = ("--global", "--plugin", "commit-guard")
SOURCES = {
    TS_REL: REPO_ROOT / "plugin-commit-guard" / "plugins" / "opencode-commit-guard.ts",
    COMMAND_REL: REPO_ROOT / "plugin-commit-guard" / "commands" / "commit-guard.md",
    GH_COMMAND_REL: REPO_ROOT / "plugin-gh-issue-to-pr" / "commands" / "gh-issue-to-pr.md",
}
USER_TEXT = "the user's own file\n"
# Task 3.1 renames claude-attribution to ai-attribution; its dir stays plugin-attribution (the dir-derived name).
ATTRIBUTION_ID = "claude-attribution"
ATTRIBUTION_TS = "plugins/opencode-claude-attribution.ts"
ATTRIBUTION_COMMAND = "commands/claude-attribution.md"
ALIAS_WARN = f"WARN: use {ATTRIBUTION_ID}"
REAL_IDS = ("claude-attribution, commit-guard, dev, env-guard, gh-issue-to-pr, markdown-format, markdown-lsp, "
            "memory-guard, mempalace-docker, qa, ruby-lsp, token-saver, wandavision")
V1_TRACKER_HEADER = ("# setup-opencode.sh tracker v1", "# installed_at: 2026-01-02T03:04:05Z",
                     "# repo_root: /srv/llm-agent-workflow", "# scope: project")

PYTHON_STUB = """\
#!/usr/bin/env bash
# Test double for python3: when $STUB_LIST_EXIT is set, `convert.py list` prints nothing and exits with it. At the Nth
# `convert.py guard` call whose last argument ends with $STUB_GUARD_SUFFIX, run $STUB_ACTION once (STUB_MODE=action)
# or exit 1 the way a crashed guard would (STUB_MODE=fail); else exec python3.
if [[ "${2:-}" == list && -n "${STUB_LIST_EXIT:-}" ]]; then exit "$STUB_LIST_EXIT"; fi
last="${@: -1}"
if [[ "${2:-}" == guard && "$last" == *"$STUB_GUARD_SUFFIX" ]]; then
  count=$(( $(cat "$STUB_COUNT" 2>/dev/null || echo 0) + 1 ))
  printf '%s\\n' "$count" > "$STUB_COUNT"
  if [[ "$count" == "$STUB_GUARD_NTH" && "$STUB_MODE" == fail ]]; then exit 1; fi
  if [[ "$count" == "$STUB_GUARD_NTH" ]]; then bash "$STUB_ACTION"; fi
fi
exec "$REAL_PYTHON3" "$@"
"""

# Test double for mv: refuses a move whose source matches the glob $MV_REFUSE_FROM and whose target matches the glob
# $MV_REFUSE_TO; every other move reaches the real mv.
MV_STUB = """\
#!/usr/bin/env bash
if [[ "$1" == $MV_REFUSE_FROM && "$2" == $MV_REFUSE_TO ]]; then exit 1; fi
exec "$REAL_MV" "$@"
"""
# place_unit's three moves for the payload dir: the temp tree in, the old tree aside into its reserved dir, and back.
TEMP_TREE_GLOB = "*/.commit-guard.??????"
PAYLOAD_TARGET_GLOB = "*/llm-agent-workflow/commit-guard"
ASIDE_GLOB = "*/.commit-guard.old.??????/tree"


def row(*fields):
    return "\t".join(fields)


def tracker_text(*rows, allowed_repos=""):
    header = ["# setup-opencode.sh tracker v2", "# installed_at: 2026-09-25T00:00:00Z", "# repo_root: /srv/llm",
              "# scope: global", "# scope_root:", "# payload_root: llm-agent-workflow", "# recipe_policy:",
              "# mcp_aliases:", f"# allowed_repos: {allowed_repos}" if allowed_repos else "# allowed_repos:",
              "# skills_md:", "# columns: type plugin path realpath hash repo"]
    return "".join(f"{line}\n" for line in header + list(rows))


def double_load_warn(target_rel, other_scope):
    return (f"WARN: {target_rel} is also installed in the {other_scope} scope; opencode loads both copies; hooks run "
            "twice")


def sha256_file(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, text=USER_TEXT):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def empty_dir(path):
    path = Path(path)
    path.mkdir(parents=True)
    return path


class InstallerTestCase(unittest.TestCase):
    def new_sandbox(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        return sandbox

    def scope_of(self, sandbox, **links):
        return link_scope(sandbox.home / ".config" / "opencode", **links)

    def lines(self, result):
        return result.stdout.splitlines()

    def assert_exit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def assert_no_row(self, scope, target_rel):
        tracker = scope / TRACKER_NAME
        if tracker.exists():
            self.assertNotIn(f"\t{target_rel}\t", tracker.read_text())

    def with_guard_hook(self, sandbox, suffix, nth, action="", mode="action"):
        """The sandbox env with the python3 double armed: at the NTH guard call on SUFFIX, run ACTION (bash) or,
        with MODE "fail", make that guard call exit 1."""
        sandbox.stub("python3", PYTHON_STUB)
        action_path = write(sandbox.root / "action.sh", action)
        return sandbox.env(STUB_GUARD_SUFFIX=suffix, STUB_GUARD_NTH=nth, STUB_COUNT=sandbox.root / "guard-calls",
                           STUB_ACTION=action_path, STUB_MODE=mode, REAL_PYTHON3=sandbox.tools / "python3")

    def with_list_exit(self, sandbox, code):
        """The sandbox env with the python3 double armed so that `convert.py list` prints nothing and exits CODE."""
        env = self.with_guard_hook(sandbox, "/no/guard/call/ends/like/this", 1)
        env["STUB_LIST_EXIT"] = str(code)
        return env

    def dry_run_lines(self, result):
        return [line for line in self.lines(result) if line.startswith("[DRY-RUN]")]

    def install_commit_guard(self, sandbox, *args, **links):
        scope = self.scope_of(sandbox, **links)
        result = run_setup(*COMMIT_GUARD, *args, sandbox=sandbox)
        self.assert_exit(result, EXIT_OK)
        return scope

    def unlock_later(self, path):
        """chmod PATH back to 0755 before the sandbox is removed (cleanups run last-in, first-out)."""
        self.addCleanup(lambda: os.path.lexists(path) and os.chmod(path, 0o755))


class InstallGuardTests(InstallerTestCase):
    """Checks that stop the install before any write, and units the guard refuses."""

    def test_an_invalid_tracker_or_a_failed_plan_stops_the_install_before_any_write(self):
        cases = {
            "an invalid tracker": lambda sandbox, scope: (
                write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", "/etc/passwd", "/etc/passwd",
                                                             DUMMY_HASH, NO_VALUE))),
                sandbox.env(), "invalid; nothing was changed"),
            "a stage inside the scope (TMPDIR in the scope)": lambda sandbox, scope: (
                None, sandbox.env(TMPDIR=empty_dir(scope / "tmp")),
                "convert.py plan failed (exit 1); nothing was written"),
        }
        for name, build in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                scope = self.scope_of(sandbox)
                _fixture, env, message = build(sandbox, scope)
                before = snapshot(sandbox.home)

                result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=env)

                self.assert_exit(result, EXIT_FAILED)
                self.assertIn(message, result.stderr)
                self.assertEqual(snapshot(sandbox.home), before)

    def test_a_blocked_payload_fails_its_plugin_file_without_writing_it(self):
        sandbox = self.new_sandbox()
        skills_md = sandbox.make_skills_md(start_submodule=False)
        scope = self.scope_of(sandbox, **{"llm-agent-workflow": skills_md / "lib"})
        checkout_before = snapshot(skills_md)

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox)

        self.assert_exit(result, EXIT_FAILED)
        lines = self.lines(result)
        self.assertIn(f"[BLOCKED] {PAYLOAD_DIR}: G1 (repo: {skills_md} {SKILLS_MD_ORIGIN})", lines)
        self.assertIn(f"[FAIL] {TS_REL} (its payload unit was not installed)", lines)
        self.assertIn(f"[OK] {COMMAND_REL}", lines)
        self.assertFalse((scope / TS_REL).exists())
        self.assertEqual(snapshot(skills_md), checkout_before)

    def test_a_blocked_or_unapproved_target_is_blocked_even_when_it_already_matches(self):
        def in_skills_md(sandbox):
            skills_md = sandbox.make_skills_md(start_submodule=False)
            scope = self.scope_of(sandbox, commands=skills_md / "commands")
            target = write(skills_md / "commands" / "gh-issue-to-pr.md", SOURCES[GH_COMMAND_REL].read_text())
            return (("--plugin", "gh-issue-to-pr"), scope, target, GH_COMMAND_REL,
                    f"[BLOCKED] {GH_COMMAND_REL}: G1 (repo: {skills_md} {SKILLS_MD_ORIGIN})")

        def in_a_submodule(sandbox):
            _superproject, dotfiles = sandbox.make_dotfiles_submodule()
            scope = self.scope_of(sandbox, plugins=dotfiles / "plugins")
            target = write(dotfiles / "plugins" / "opencode-commit-guard.ts", SOURCES[TS_REL].read_text())
            return (("--plugin", "commit-guard"), scope, target, TS_REL,
                    f"[BLOCKED] {TS_REL}: G3 needs approval; re-run with --allow-repo {dotfiles} "
                    f"(repo: {dotfiles} https://example.invalid/dotfiles.git)")

        cases = {"G1": in_skills_md, "G3 without approval": in_a_submodule}
        for (name, build), force in ((case, force) for case in cases.items() for force in ((), ("--force",))):
            with self.subTest(name, force=force):
                sandbox = self.new_sandbox()
                args, scope, target, target_rel, line = build(sandbox)
                content = target.read_bytes()

                result = run_setup("--global", *args, *force, sandbox=sandbox)

                self.assert_exit(result, EXIT_FAILED)
                self.assertIn(line, self.lines(result))
                self.assertNotIn(f"[SAME] {target_rel}", result.stdout)
                self.assertEqual(target.read_bytes(), content)
                self.assert_no_row(scope, target_rel)

    def test_units_that_plan_rejects_fail_the_install_while_the_others_install(self):
        sandbox = self.new_sandbox()
        repo = sandbox.fixtures / "repo"
        for relpath in ("setup-opencode.sh", ".claude-plugin/marketplace.json", "scripts/opencode/convert.py",
                        "scripts/opencode/guard.py", "scripts/opencode/mapping.py", "scripts/opencode/tracker.py",
                        "scripts/opencode/units.py"):
            (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / relpath, repo / relpath)
        data = json.loads((REPO_ROOT / "scripts" / "opencode" / "mapping.json").read_text())
        data.update(payloads={"commit-guard": data["payloads"]["commit-guard"]}, extra_sources={})
        (repo / "scripts" / "opencode" / "mapping.json").write_text(json.dumps(data))
        shutil.copytree(REPO_ROOT / "plugin-commit-guard", repo / "plugin-commit-guard",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (repo / "plugin-commit-guard" / "hooks" / "link").symlink_to("hooks.json")
        scope = self.scope_of(sandbox)

        result = subprocess.run(["bash", str(repo / "setup-opencode.sh"), "--global"], cwd=sandbox.root,
                                env=sandbox.env(), input="", capture_output=True, text=True,
                                timeout=RUN_TIMEOUT_SECONDS)

        self.assert_exit(result, EXIT_FAILED)
        self.assertIn("ERROR plugin-commit-guard/hooks/link: payload-entry:", result.stderr)
        self.assertIn(f"[OK] {COMMAND_REL}", self.lines(result))
        self.assertFalse((scope / PAYLOAD_DIR).exists())
        self.assertFalse((scope / TS_REL).exists())

    def test_a_target_of_the_wrong_type_fails_without_a_write_even_with_force(self):
        for force in ((), ("--force",)):
            with self.subTest(force=force):
                sandbox = self.new_sandbox()
                scope = self.scope_of(sandbox)
                write(scope / COMMAND_REL / "keep.txt")
                write(scope / PAYLOAD_DIR)
                before = snapshot(sandbox.home)

                result = run_setup(*COMMIT_GUARD, *force, sandbox=sandbox)

                self.assert_exit(result, EXIT_FAILED)
                lines = self.lines(result)
                self.assertIn(f"[FAIL] {COMMAND_REL} (target is not a file)", lines)
                self.assertIn(f"[FAIL] {PAYLOAD_DIR} (target is not a dir)", lines)
                self.assertIn(f"[FAIL] {TS_REL} (its payload unit was not installed)", lines)
                self.assertEqual(snapshot(sandbox.home), before)

    def test_the_tracker_location_must_pass_the_guard_before_install_or_uninstall_changes_anything(self):
        # The scope root itself lies in a dotfiles submodule; the unit dirs link outside any repo.
        def world(sandbox):
            superproject, dotfiles = sandbox.make_dotfiles_submodule()
            outside = sandbox.root / "outside"
            sub_scope = link_scope(dotfiles / "opencode", plugins=outside / "plugins", commands=outside / "commands",
                                   **{"llm-agent-workflow": outside / "llm-agent-workflow"})
            (sandbox.home / ".config").mkdir()
            (sandbox.home / ".config" / "opencode").symlink_to(sub_scope)
            return superproject, dotfiles, outside, sub_scope

        with self.subTest("install"):
            sandbox = self.new_sandbox()
            superproject, dotfiles, outside, sub_scope = world(sandbox)
            before = (snapshot(superproject), snapshot(outside))

            result = run_setup(*COMMIT_GUARD, sandbox=sandbox)

            self.assert_exit(result, EXIT_FAILED)
            self.assertIn(f"{TRACKER_NAME}: G3 needs approval; re-run with --allow-repo {dotfiles}; nothing was "
                          "changed", result.stderr)
            self.assertEqual((snapshot(superproject), snapshot(outside)), before)

        with self.subTest("uninstall"):
            sandbox = self.new_sandbox()
            superproject, dotfiles, outside, sub_scope = world(sandbox)
            installed = write(outside / "plugins" / "opencode-commit-guard.ts", "installed\n")
            write(sub_scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", TS_REL, str(installed),
                                                             sha256_file(installed), NO_VALUE)))
            before = (snapshot(superproject), snapshot(outside))

            result = run_setup("--global", "--uninstall", sandbox=sandbox)

            self.assert_exit(result, EXIT_FAILED)
            self.assertIn(f"{TRACKER_NAME}: G3 needs approval", result.stderr)
            self.assertEqual((snapshot(superproject), snapshot(outside)), before)

        with self.subTest("install with --allow-repo"):
            sandbox = self.new_sandbox()
            superproject, dotfiles, outside, sub_scope = world(sandbox)

            result = run_setup(*COMMIT_GUARD, "--allow-repo", dotfiles, sandbox=sandbox)

            self.assert_exit(result, EXIT_OK)
            self.assertTrue((outside / "plugins" / "opencode-commit-guard.ts").is_file())
            self.assertIn(f"# allowed_repos: {dotfiles}", (sub_scope / TRACKER_NAME).read_text().splitlines())

    def test_two_approved_submodules_round_trip_through_the_tracker_header(self):
        sandbox = self.new_sandbox()
        superproject, dotfiles = sandbox.make_dotfiles_submodule()
        second = sandbox.add_submodule(superproject, sandbox.make_repo(sandbox.fixtures / "second-source"), "second",
                                       "https://example.invalid/second.git")
        scope = self.scope_of(sandbox, plugins=dotfiles / "plugins", commands=second / "commands")

        first = run_setup(*COMMIT_GUARD, "--allow-repo", dotfiles, "--allow-repo", second, sandbox=sandbox)

        self.assert_exit(first, EXIT_OK)
        self.assertIn(f"# allowed_repos: {dotfiles}:{second}", (scope / TRACKER_NAME).read_text().splitlines())
        (dotfiles / "plugins" / "opencode-commit-guard.ts").unlink()
        (second / "commands" / "commit-guard.md").unlink()

        rerun = run_setup(*COMMIT_GUARD, sandbox=sandbox)

        self.assert_exit(rerun, EXIT_OK)
        self.assertNotIn("[BLOCKED]", rerun.stdout)
        self.assertTrue((dotfiles / "plugins" / "opencode-commit-guard.ts").is_file())
        self.assertTrue((second / "commands" / "commit-guard.md").is_file())

    def test_allow_repo_must_name_a_recordable_directory(self):
        cases = {
            "a missing path": lambda sandbox: (sandbox.root / "missing", "not a directory"),
            "a path with a colon": lambda sandbox: (
                empty_dir(sandbox.root / "a:b"), "a path with ':' or a control character cannot be recorded"),
        }
        for name, build in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                self.scope_of(sandbox)
                path, message = build(sandbox)
                before = snapshot(sandbox.home)

                result = run_setup(*COMMIT_GUARD, "--allow-repo", path, sandbox=sandbox)

                self.assert_exit(result, EXIT_FAILED)
                self.assertIn(f"--allow-repo {path}: {message}", result.stderr)
                self.assertEqual(snapshot(sandbox.home), before)


class InstallStateTests(InstallerTestCase):
    """CONFLICT, UPDATE and --force: a user's file is never overwritten silently, and never without a backup."""

    def test_conflicts_are_skipped_without_force_and_a_matching_v2_row_is_an_update(self):
        cases = {
            "no row": ((), f"[SKIP] {COMMAND_REL} (conflict; pass --force to overwrite)", USER_TEXT),
            "a v2 row whose hash is not the target's": (
                (DUMMY_HASH,), f"[SKIP] {COMMAND_REL} (conflict; pass --force to overwrite)", USER_TEXT),
            "a v2 row whose hash is the target's": (
                ("target",), f"[UPDATE] {COMMAND_REL}", SOURCES[COMMAND_REL].read_text()),
        }
        for name, (tracked, line, content) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                scope = self.scope_of(sandbox)
                target = write(scope / COMMAND_REL)
                if tracked:
                    digest = sha256_file(target) if tracked == ("target",) else tracked[0]
                    write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", COMMAND_REL, str(target),
                                                                 digest, NO_VALUE)))

                result = run_setup(*COMMIT_GUARD, sandbox=sandbox)

                self.assert_exit(result, EXIT_OK)
                self.assertIn(line, self.lines(result))
                self.assertEqual(target.read_text(), content)
                self.assertEqual(list((scope / "llm-agent-workflow").iterdir()), [scope / PAYLOAD_DIR])

    def test_an_identical_file_without_a_row_is_same_and_gets_no_row(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        target = write(scope / COMMAND_REL, SOURCES[COMMAND_REL].read_text())

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertIn(f"[SAME] {COMMAND_REL}", self.lines(result))
        self.assertIn(f"\t{TS_REL}\t", (scope / TRACKER_NAME).read_text())
        self.assert_no_row(scope, COMMAND_REL)
        self.assertEqual(target.read_bytes(), SOURCES[COMMAND_REL].read_bytes())

    def test_force_backs_up_a_conflict_first_and_a_blocked_backup_blocks_the_overwrite(self):
        with self.subTest("backup inside the scope"):
            sandbox = self.new_sandbox()
            scope = self.scope_of(sandbox)
            target = write(scope / COMMAND_REL)

            result = run_setup(*COMMIT_GUARD, "--force", sandbox=sandbox)

            self.assert_exit(result, EXIT_OK)
            self.assertIn(f"[OVERWRITE] {COMMAND_REL}", self.lines(result))
            self.assertEqual(target.read_bytes(), SOURCES[COMMAND_REL].read_bytes())
            backups = list((scope / "llm-agent-workflow" / ".backup").glob(f"*/{COMMAND_REL}"))
            self.assertEqual([path.read_text() for path in backups], [USER_TEXT])

        with self.subTest("backup dir inside a skills-md checkout"):
            sandbox = self.new_sandbox()
            skills_md = sandbox.make_skills_md(start_submodule=False)
            scope = self.scope_of(sandbox, **{"llm-agent-workflow": skills_md / "lib"})
            target = write(scope / GH_COMMAND_REL)
            checkout_before = snapshot(skills_md)

            result = run_setup("--global", "--plugin", "gh-issue-to-pr", "--force", sandbox=sandbox)

            self.assert_exit(result, EXIT_FAILED)
            self.assertIn(f"[BLOCKED] {GH_COMMAND_REL}: backup: G1", self.lines(result))
            self.assertEqual(target.read_text(), USER_TEXT)
            self.assertEqual(snapshot(skills_md), checkout_before)

    @unittest.skipIf(IS_ROOT, "root ignores the read-only backup dir")
    def test_a_backup_that_cannot_be_written_fails_the_overwrite(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        target = write(scope / COMMAND_REL)
        backups = empty_dir(scope / "llm-agent-workflow" / ".backup")
        backups.chmod(0o555)
        self.unlock_later(backups)

        result = run_setup(*COMMIT_GUARD, "--force", sandbox=sandbox)

        self.assert_exit(result, EXIT_FAILED)
        self.assertIn(f"[FAIL] {COMMAND_REL} (backup failed)", self.lines(result))
        self.assertEqual(target.read_text(), USER_TEXT)
        self.assert_no_row(scope, COMMAND_REL)

    @unittest.skipIf(IS_ROOT, "root reads mode-000 dirs")
    def test_a_dir_target_that_cannot_be_hashed_fails_and_keeps_its_contents(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        kept = write(scope / PAYLOAD_DIR / "keep.txt")
        locked = empty_dir(scope / PAYLOAD_DIR / "locked")
        locked.chmod(0o000)
        self.unlock_later(locked)

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox)

        self.assert_exit(result, EXIT_FAILED)
        self.assertIn(f"[FAIL] {PAYLOAD_DIR} (cannot hash the target)", self.lines(result))
        self.assertEqual(kept.read_text(), USER_TEXT)
        self.assertTrue(os.path.isdir(locked))

    @unittest.skipIf(IS_ROOT, "root reads mode-000 files")
    def test_a_failed_copy_is_a_fail_with_no_row_and_keeps_the_old_target(self):
        with self.subTest("a file unit"):
            sandbox = self.new_sandbox()
            scope = self.scope_of(sandbox)
            env = self.with_guard_hook(sandbox, COMMAND_REL, 1,
                                       'chmod 000 "$TMPDIR"/opencode-setup.*/stage/commands/commit-guard.md\n')

            result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=env)

            self.assert_exit(result, EXIT_FAILED)
            self.assertIn(f"[FAIL] {COMMAND_REL} (copy failed)", self.lines(result))
            self.assert_no_row(scope, COMMAND_REL)

        with self.subTest("a dir unit with --force"):
            sandbox = self.new_sandbox()
            scope = self.install_commit_guard(sandbox)
            extra = write(scope / PAYLOAD_DIR / "hooks" / "extra.txt")
            env = self.with_guard_hook(sandbox, PAYLOAD_DIR, 1, 'chmod 000 "$TMPDIR"/opencode-setup.*/stage/'
                                                                'llm-agent-workflow/commit-guard/hooks/hooks.json\n')

            result = run_setup(*COMMIT_GUARD, "--force", sandbox=sandbox, env=env)

            self.assert_exit(result, EXIT_FAILED)
            self.assertIn(f"[FAIL] {PAYLOAD_DIR} (copy failed)", self.lines(result))
            self.assertEqual(extra.read_text(), USER_TEXT)

    def test_a_same_tracked_row_with_a_stale_hash_gets_the_current_hash(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        target = write(scope / COMMAND_REL, SOURCES[COMMAND_REL].read_text())
        write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", COMMAND_REL, str(target), DUMMY_HASH,
                                                     NO_VALUE)))

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertIn(f"[SAME] {COMMAND_REL}", self.lines(result))
        self.assertIn(row("file", "commit-guard", COMMAND_REL, str(target), sha256_file(SOURCES[COMMAND_REL]),
                          NO_VALUE), (scope / TRACKER_NAME).read_text().splitlines())

    def test_installed_at_is_utc_whatever_the_local_zone(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        started = datetime.datetime.now(datetime.timezone.utc)

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=sandbox.env(TZ="PHT-8"))  # 8 hours off UTC

        self.assert_exit(result, EXIT_OK)
        stamp = re.search(r"^# installed_at: (.*)$", (scope / TRACKER_NAME).read_text(), re.M).group(1)
        written = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        self.assertLess(abs(written - started), datetime.timedelta(minutes=10))

    def test_a_skipped_conflict_prints_one_conflict_line_and_no_other_unit_does(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        write(scope / COMMAND_REL)

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertEqual([line for line in self.lines(result) if line.startswith("[CONFLICT]")],
                         [f"[CONFLICT] {COMMAND_REL}"])

    def test_an_update_is_not_labelled_conflict(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        target = write(scope / COMMAND_REL)
        write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", COMMAND_REL, str(target),
                                                     sha256_file(target), NO_VALUE)))

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        lines = self.lines(result)
        self.assertIn(f"[UPDATE] {COMMAND_REL}", lines)
        self.assertEqual([line for line in lines if line.startswith("[CONFLICT]")], [])

    def update_world(self, sandbox):
        """An installed commit-guard whose payload dir holds a user file and whose row hash is the target's, so the
        next install is an UPDATE of that dir (no backup)."""
        scope = self.install_commit_guard(sandbox)
        extra = write(scope / PAYLOAD_DIR / "hooks" / "extra.txt")
        hashed = run_convert("hash", scope / PAYLOAD_DIR, sandbox=sandbox)
        self.assert_exit(hashed, EXIT_OK)
        tracker = scope / TRACKER_NAME
        rows = [line.split("\t") for line in tracker.read_text().splitlines()]
        tracker.write_text("".join("\t".join(fields[:4] + [hashed.stdout.split("\t")[0]] + fields[5:]) + "\n"
                                   if fields[:3] == ["dir", "commit-guard", PAYLOAD_DIR] else "\t".join(fields) + "\n"
                                   for fields in rows))
        return scope, extra

    def with_mv_double(self, sandbox, refuse_from, refuse_to):
        sandbox.stub("mv", MV_STUB)
        return sandbox.env(REAL_MV=sandbox.tools / "mv", MV_REFUSE_FROM=refuse_from, MV_REFUSE_TO=refuse_to)

    def test_a_dir_update_replaces_the_tree_and_leaves_no_sibling(self):
        sandbox = self.new_sandbox()
        scope, extra = self.update_world(sandbox)

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertIn(f"[UPDATE] {PAYLOAD_DIR}", self.lines(result))
        self.assertEqual(sorted(os.listdir(scope / "llm-agent-workflow")), ["commit-guard"])
        self.assertFalse(extra.exists())

    def test_a_dir_swap_that_fails_keeps_the_old_tree_of_an_update(self):
        cases = {
            "the move-in fails": (TEMP_TREE_GLOB, PAYLOAD_TARGET_GLOB),
            "the move-aside fails": (PAYLOAD_TARGET_GLOB, ASIDE_GLOB),
        }
        for name, (refuse_from, refuse_to) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                scope, extra = self.update_world(sandbox)
                before = snapshot(scope / "llm-agent-workflow")
                env = self.with_mv_double(sandbox, refuse_from, refuse_to)

                result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=env)

                self.assert_exit(result, EXIT_FAILED)
                self.assertIn(f"[FAIL] {PAYLOAD_DIR} (copy failed)", self.lines(result))
                self.assertNotIn(f"[UPDATE] {PAYLOAD_DIR}", result.stdout)
                self.assertEqual(extra.read_text(), USER_TEXT)
                self.assertEqual(snapshot(scope / "llm-agent-workflow"), before)

    def test_a_failed_swap_and_move_back_names_where_the_old_tree_is(self):
        sandbox = self.new_sandbox()
        scope, _extra = self.update_world(sandbox)
        env = self.with_mv_double(sandbox, "*", PAYLOAD_TARGET_GLOB)

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=env)

        self.assert_exit(result, EXIT_FAILED)
        asides = sorted((scope / "llm-agent-workflow").glob(".commit-guard.old.*"))
        self.assertEqual(len(asides), 1, os.listdir(scope / "llm-agent-workflow"))
        old_tree = asides[0] / "tree"
        self.assertIn(f"[FAIL] {PAYLOAD_DIR} (copy failed; the old tree is at {old_tree})", self.lines(result))
        self.assertEqual((old_tree / "hooks" / "extra.txt").read_text(), USER_TEXT)
        self.assertFalse((scope / PAYLOAD_DIR).exists())
        self.assertEqual(list((scope / "llm-agent-workflow").glob(".commit-guard.??????")), [])

    @unittest.skipIf(IS_ROOT, "root removes files below a read-only dir")
    def test_an_old_tree_that_cannot_be_removed_is_a_warning_after_a_good_overwrite(self):
        sandbox = self.new_sandbox()
        scope = self.install_commit_guard(sandbox)
        read_only = empty_dir(scope / PAYLOAD_DIR / "hooks" / "ro")
        write(read_only / "f.txt")
        read_only.chmod(0o555)
        self.addCleanup(subprocess.run, ["chmod", "-R", "u+rwx", str(sandbox.root)], check=False)

        result = run_setup(*COMMIT_GUARD, "--force", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertIn(f"[OVERWRITE] {PAYLOAD_DIR}", self.lines(result))
        asides = sorted((scope / "llm-agent-workflow").glob(".commit-guard.old.*"))
        self.assertEqual(len(asides), 1, os.listdir(scope / "llm-agent-workflow"))
        self.assertIn(f"WARN {asides[0]}: leftover: the old tree of {PAYLOAD_DIR} could not be removed; remove it "
                      "yourself", result.stderr.splitlines())
        self.assertFalse((scope / PAYLOAD_DIR / "hooks" / "ro").exists())
        staged_hash = run_convert("hash", scope / PAYLOAD_DIR, sandbox=sandbox).stdout.split("\t")[0]
        self.assertIn(row("dir", "commit-guard", PAYLOAD_DIR, str(scope / PAYLOAD_DIR), staged_hash, NO_VALUE),
                      (scope / TRACKER_NAME).read_text().splitlines())


class RecheckBeforeWriteTests(InstallerTestCase):
    """The guard runs again right before each write, so a change after `plan` cannot redirect the write."""

    def test_a_target_that_changes_after_plan_is_blocked_at_the_write(self):
        cases = {
            "its dir became a skills-md checkout": (
                'git init --quiet "$A" && git -C "$A" remote add origin "$ORIGIN"\n', "G1"),
            "its dir became a symlink elsewhere": (
                'mv "$A/plugins" "$A/plugins.orig" && mkdir -p "$B" && ln -s "$B" "$A/plugins"\n',
                "realpath changed since plan"),
        }
        for name, (action, reason) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                plain = sandbox.fixtures / "plain"
                elsewhere = sandbox.fixtures / "elsewhere"
                self.scope_of(sandbox, plugins=plain / "plugins")
                env = self.with_guard_hook(sandbox, TS_REL, 1, action)
                env.update(A=str(plain), B=str(elsewhere), ORIGIN=SKILLS_MD_ORIGIN)

                result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=env)

                self.assert_exit(result, EXIT_FAILED)
                self.assertIn(f"[BLOCKED] {TS_REL}: {reason}", self.lines(result))
                self.assertEqual([path.name for path in plain.rglob("opencode-commit-guard.ts")], [])
                self.assertFalse((elsewhere / "opencode-commit-guard.ts").exists())
                self.assert_no_row(sandbox.home / ".config" / "opencode", TS_REL)

    def test_a_target_replaced_by_a_symlink_after_plan_fails_even_when_its_content_matches(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        target = write(scope / COMMAND_REL, SOURCES[COMMAND_REL].read_text())
        elsewhere = write(sandbox.fixtures / "elsewhere" / "commit-guard.md", SOURCES[COMMAND_REL].read_text())
        # The payload unit's pre-write guard call comes before the command unit is looked at.
        env = self.with_guard_hook(sandbox, PAYLOAD_DIR, 1, 'rm "$T" && ln -s "$E" "$T"\n')
        env.update(T=str(target), E=str(elsewhere))

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=env)

        self.assert_exit(result, EXIT_FAILED)
        self.assertIn(f"[FAIL] {COMMAND_REL} (target is not a file)", self.lines(result))
        self.assertNotIn(f"[SAME] {COMMAND_REL}", result.stdout)
        self.assertEqual(elsewhere.read_bytes(), SOURCES[COMMAND_REL].read_bytes())
        self.assert_no_row(scope, COMMAND_REL)

    def test_a_tracker_location_that_changes_after_the_units_is_blocked_at_the_tracker_write(self):
        sandbox = self.new_sandbox()
        _superproject, dotfiles = sandbox.make_dotfiles_submodule()
        real_scope = sandbox.fixtures / "scope"
        real_scope.mkdir()
        (sandbox.home / ".config").mkdir()
        (sandbox.home / ".config" / "opencode").symlink_to(real_scope)
        moved = dotfiles / "opencode"
        # Before the second tracker check (the first is the pre-flight), the scope moves into the submodule.
        env = self.with_guard_hook(sandbox, TRACKER_NAME, 2, 'mv "$A" "$B" && ln -s "$B" "$A"\n')
        env.update(A=str(real_scope), B=str(moved))

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=env)

        self.assert_exit(result, EXIT_FAILED)
        self.assertIn(f"[BLOCKED] {TRACKER_NAME}: G3 needs approval; re-run with --allow-repo {dotfiles}",
                      self.lines(result))
        self.assertTrue((moved / TS_REL).is_file())
        self.assertFalse((moved / TRACKER_NAME).exists())

    def test_a_guard_that_fails_at_the_tracker_write_blocks_it(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        env = self.with_guard_hook(sandbox, TRACKER_NAME, 2, mode="fail")

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=env)

        self.assert_exit(result, EXIT_FAILED)
        self.assertIn(f"[BLOCKED] {TRACKER_NAME}: guard failed", self.lines(result))
        self.assertTrue((scope / TS_REL).is_file())
        self.assertFalse((scope / TRACKER_NAME).exists())


class UninstallTests(InstallerTestCase):
    """--uninstall deletes recorded realpaths only after the tracker and the guard allow it."""

    def test_uninstall_removes_every_tracked_unit_and_the_tracker(self):
        sandbox = self.new_sandbox()
        scope = self.install_commit_guard(sandbox)
        os.remove(scope / COMMAND_REL)

        result = run_setup("--global", "--uninstall", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        lines = self.lines(result)
        self.assertIn(f"[REMOVED] {PAYLOAD_DIR}", lines)
        self.assertIn(f"[REMOVED] {TS_REL}", lines)
        self.assertIn(f"[SKIP] {COMMAND_REL} (not present)", lines)
        for relpath in (PAYLOAD_DIR, TS_REL, TRACKER_NAME):
            self.assertFalse((scope / relpath).exists(), relpath)

    def test_uninstall_lists_its_removals_per_repo(self):
        sandbox = self.new_sandbox()
        superproject, dotfiles = sandbox.make_dotfiles_submodule()
        self.install_commit_guard(sandbox, "--allow-repo", dotfiles, plugins=dotfiles / "plugins")
        ts_realpath = dotfiles / "plugins" / "opencode-commit-guard.ts"

        result = run_setup("--global", "--uninstall", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        lines = self.lines(result)
        self.assertIn(f"[REMOVED] {TS_REL} (repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})", lines)
        summary = lines[lines.index(f"Repo {dotfiles}"):]
        self.assertEqual(summary[:4], [f"Repo {dotfiles}", f"  removed {ts_realpath}",
                                       f"  commit these yourself in {dotfiles}",
                                       f"  then update the submodule pointer in {superproject}"])
        self.assertFalse(ts_realpath.exists())

    def test_uninstall_dry_run_deletes_nothing_and_keeps_the_tracker(self):
        sandbox = self.new_sandbox()
        self.install_commit_guard(sandbox)
        before = snapshot(sandbox.home)

        result = run_setup("--global", "--uninstall", "--dry-run", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertIn(f"[DRY-RUN] REMOVE {TS_REL} ok", self.lines(result))
        self.assertEqual(snapshot(sandbox.home), before)

    def test_an_invalid_tracker_exits_1_before_any_delete(self):
        sandbox = self.new_sandbox()
        scope = self.install_commit_guard(sandbox)
        tracker = scope / TRACKER_NAME
        tracker.write_text(tracker.read_text() + row("file", "commit-guard", "/etc/passwd", "/etc/passwd",
                                                     DUMMY_HASH, NO_VALUE) + "\n")
        before = snapshot(sandbox.home)

        result = run_setup("--global", "--uninstall", sandbox=sandbox)

        self.assert_exit(result, EXIT_FAILED)
        self.assertIn("tracker: absolute path", result.stderr)
        self.assertEqual(snapshot(sandbox.home), before)

    def test_rows_the_guard_or_the_filesystem_refuse_are_kept_and_exit_1(self):
        def g1_row(sandbox, scope):
            skills_md = sandbox.make_skills_md(start_submodule=False)
            (scope / "commands").symlink_to(skills_md / "commands")
            target = write(skills_md / "commands" / "x.md")
            return (row("file", "commit-guard", "commands/x.md", str(target), sha256_file(target), str(skills_md)),
                    f"[BLOCKED] commands/x.md: G1 (repo: {skills_md} {SKILLS_MD_ORIGIN})", target)

        def dir_row_on_a_file(sandbox, scope):
            target = write(scope / PAYLOAD_DIR)
            return (row("dir", "commit-guard", PAYLOAD_DIR, str(target), DUMMY_HASH, NO_VALUE),
                    f"[FAIL] {PAYLOAD_DIR} (target is not a dir)", target)

        def file_row_on_a_dir(sandbox, scope):
            target = write(scope / COMMAND_REL / "keep.txt").parent
            return (row("file", "commit-guard", COMMAND_REL, str(target), DUMMY_HASH, NO_VALUE),
                    f"[FAIL] {COMMAND_REL} (target is not a file)", target / "keep.txt")

        def config_row(sandbox, scope):
            target = write(scope / "opencode.json", "{}\n")
            return (row("config", "markdown-lsp", "opencode.json#/lsp/rumdl", str(target), DUMMY_HASH, NO_VALUE),
                    "[SKIP] opencode.json#/lsp/rumdl (config leaves are not removed by this version)", target)

        cases = {
            "a row in a skills-md checkout": (g1_row, EXIT_FAILED),
            "a dir row whose target is a file": (dir_row_on_a_file, EXIT_FAILED),
            "a file row whose target is a dir": (file_row_on_a_dir, EXIT_FAILED),
            "a config row": (config_row, EXIT_OK),
        }
        for name, (build, code) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                scope = self.scope_of(sandbox)
                tracked, line, kept = build(sandbox, scope)
                tracker = write(scope / TRACKER_NAME, tracker_text(tracked))
                kept_before = kept.read_bytes()

                result = run_setup("--global", "--uninstall", sandbox=sandbox)

                self.assert_exit(result, code)
                self.assertIn(line, self.lines(result))
                self.assertEqual(kept.read_bytes(), kept_before)
                self.assertIn(tracked, tracker.read_text().splitlines())

    def moved_world(self, sandbox):
        """A tracker whose rows reach every uninstall decision once plugins/ and commands/ moved (F4 "Uninstall").

        plugins/ now links to fixtures/new-plugins, while three rows still record files in <scope>/plugins-old (inside
        the scope): one matching its hash, one edited, one with no hash. commands/ moved to fixtures/elsewhere and
        links there, so its row's recorded realpath now resolves elsewhere; its hash still matches. agents/ never
        moved, and its file changed since install. Returns (scope, tracker, {name: path}).
        """
        scope = self.scope_of(sandbox)
        files = {"matching": write(scope / "plugins-old" / "matching.ts", "matching\n"),
                 "changed": write(scope / "plugins-old" / "changed.ts", USER_TEXT),
                 "nohash": write(scope / "plugins-old" / "nohash.ts", "no hash\n"),
                 "new": write(sandbox.fixtures / "new-plugins" / "matching.ts", "a different file, same name\n"),
                 "aliased": write(scope / "commands" / "aliased.md", "aliased\n"),
                 "edited": write(scope / "agents" / "edited.md", USER_TEXT)}
        rows = [row("file", "commit-guard", "agents/edited.md", str(files["edited"]), DUMMY_HASH, NO_VALUE),
                row("file", "commit-guard", "commands/aliased.md", str(files["aliased"]), sha256_file(files["aliased"]),
                    NO_VALUE),
                row("file", "commit-guard", "plugins/changed.ts", str(files["changed"]), DUMMY_HASH, NO_VALUE),
                row("file", "commit-guard", "plugins/matching.ts", str(files["matching"]),
                    sha256_file(files["matching"]), NO_VALUE),
                row("file", "commit-guard", "plugins/nohash.ts", str(files["nohash"]), NO_VALUE, NO_VALUE)]
        tracker = write(scope / TRACKER_NAME, tracker_text(*rows))
        (scope / "plugins").symlink_to(files["new"].parent)
        (scope / "commands").rename(sandbox.fixtures / "elsewhere")
        (scope / "commands").symlink_to(sandbox.fixtures / "elsewhere")
        files["aliased"] = sandbox.fixtures / "elsewhere" / "aliased.md"
        return scope, tracker, files

    def test_a_moved_row_is_removed_only_when_its_recorded_realpath_still_matches_in_a_dry_run_too(self):
        sandbox = self.new_sandbox()
        scope, tracker, files = self.moved_world(sandbox)
        tracker_text_before = tracker.read_text()
        before = (snapshot(sandbox.home), snapshot(sandbox.fixtures))
        moved_realpath = "moved; the recorded realpath now resolves elsewhere"
        moved_hash = "moved; the recorded hash does not match"

        dry_run = run_setup("--global", "--uninstall", "--dry-run", sandbox=sandbox)

        self.assert_exit(dry_run, EXIT_OK)
        self.assertEqual(self.dry_run_lines(dry_run), [
            "[DRY-RUN] REMOVE agents/edited.md ok", f"[DRY-RUN] KEPT commands/aliased.md ok ({moved_realpath})",
            f"[DRY-RUN] KEPT plugins/changed.ts ok ({moved_hash})", "[DRY-RUN] REMOVE plugins/matching.ts ok",
            f"[DRY-RUN] KEPT plugins/nohash.ts ok ({moved_hash})"])
        self.assertEqual((snapshot(sandbox.home), snapshot(sandbox.fixtures)), before)

        result = run_setup("--global", "--uninstall", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        lines = self.lines(result)
        self.assertEqual([line for line in lines if line.startswith(("[REMOVED]", "[KEPT]"))], [
            "[REMOVED] agents/edited.md", f"[KEPT] commands/aliased.md ({moved_realpath})",
            f"[KEPT] plugins/changed.ts ({moved_hash})", "[REMOVED] plugins/matching.ts",
            f"[KEPT] plugins/nohash.ts ({moved_hash})"])
        self.assertEqual((lines.count("  removed: 2"), lines.count("  kept:    3")), (1, 1))
        self.assertEqual(sorted(name for name, path in files.items() if not path.exists()), ["edited", "matching"])
        self.assertEqual(files["new"].read_text(), "a different file, same name\n")
        self.assertEqual(files["aliased"].read_text(), "aliased\n")
        kept = [line for line in tracker_text_before.splitlines() if "\tcommands/aliased.md\t" in line
                or "\tplugins/changed.ts\t" in line or "\tplugins/nohash.ts\t" in line]
        self.assertEqual([line for line in tracker.read_text().splitlines() if not line.startswith("#")], kept)

    def test_a_guard_that_fails_while_locating_or_checking_a_row_blocks_it(self):
        # The command row's first guard call locates <scope>/<path>; its second checks the recorded realpath.
        for nth in (1, 2):
            with self.subTest(guard_call=nth):
                sandbox = self.new_sandbox()
                scope = self.install_commit_guard(sandbox)
                tracker = scope / TRACKER_NAME
                command_row = next(line for line in tracker.read_text().splitlines() if f"\t{COMMAND_REL}\t" in line)
                env = self.with_guard_hook(sandbox, COMMAND_REL, nth, mode="fail")

                result = run_setup("--global", "--uninstall", sandbox=sandbox, env=env)

                self.assert_exit(result, EXIT_FAILED)
                self.assertIn(f"[BLOCKED] {COMMAND_REL}: guard failed", self.lines(result))
                self.assertEqual((scope / COMMAND_REL).read_bytes(), SOURCES[COMMAND_REL].read_bytes())
                self.assertEqual([line for line in tracker.read_text().splitlines() if not line.startswith("#")],
                                 [command_row])
                self.assertFalse((scope / TS_REL).exists())

    def test_uninstall_with_plugin_removes_only_the_rows_of_those_plugins(self):
        with self.subTest("v2 rows"):
            sandbox = self.new_sandbox()
            scope = self.scope_of(sandbox)
            installed = run_setup("--global", "--plugin", "commit-guard", "--plugin", "token-saver", "--plugin",
                                  "markdown-lsp", sandbox=sandbox)
            self.assert_exit(installed, EXIT_OK)
            tracker = scope / TRACKER_NAME
            text = tracker.read_text()
            token_saver = [scope / "llm-agent-workflow" / "token-saver", scope / "plugins" / "opencode-token-saver.ts"]
            token_saver_before = [snapshot(path) for path in token_saver]

            removed = run_setup("--global", "--uninstall", "--plugin", "commit-guard", "--plugin", "markdown-lsp",
                                sandbox=sandbox)

            self.assert_exit(removed, EXIT_OK)
            self.assertEqual([line for line in self.lines(removed) if line.startswith("[REMOVED]")], [
                f"[REMOVED] {PAYLOAD_DIR}", "[REMOVED] llm-agent-workflow/markdown-lsp", f"[REMOVED] {COMMAND_REL}",
                f"[REMOVED] {TS_REL}"])
            self.assertEqual(tracker.read_text(), "".join(f"{line}\n" for line in text.splitlines()
                                                          if line.startswith("#") or "\ttoken-saver\t" in line))
            for relpath in (PAYLOAD_DIR, "llm-agent-workflow/markdown-lsp", COMMAND_REL, TS_REL):
                self.assertFalse(os.path.lexists(scope / relpath), relpath)
            self.assertEqual([snapshot(path) for path in token_saver], token_saver_before)
            after_first = tracker.read_bytes()

            again = run_setup("--global", "--uninstall", "--plugin", "commit-guard", sandbox=sandbox)

            self.assert_exit(again, EXIT_OK)
            self.assertIn("  removed: 0", self.lines(again))
            self.assertEqual(tracker.read_bytes(), after_first)

            last = run_setup("--global", "--uninstall", "--plugin", "token-saver", sandbox=sandbox)

            self.assert_exit(last, EXIT_OK)
            self.assertIn("  removed: 2", self.lines(last))
            self.assertFalse(os.path.lexists(tracker))
            self.assertEqual([os.path.lexists(path) for path in token_saver], [False, False])

        with self.subTest("v1 rows (plugin ?) belong to no --plugin"):
            sandbox = self.new_sandbox()
            scope = self.scope_of(sandbox)
            target = write(scope / COMMAND_REL)
            tracker = write(scope / TRACKER_NAME, "".join(f"{line}\n" for line in (*V1_TRACKER_HEADER, COMMAND_REL)))
            tracker_before = tracker.read_bytes()

            result = run_setup("--global", "--uninstall", "--plugin", "commit-guard", sandbox=sandbox)

            self.assert_exit(result, EXIT_OK)
            self.assertIn("  removed: 0", self.lines(result))
            self.assertEqual((target.read_text(), tracker.read_bytes()), (USER_TEXT, tracker_before))

    def one_moved_row(self, sandbox, scope, text="ours\n"):
        """plugins/ now links to fixtures/new-plugins, and <scope>/plugins-old/x.ts is where a row recorded x.ts."""
        old = write(scope / "plugins-old" / "x.ts", text)
        new = write(sandbox.fixtures / "new-plugins" / "x.ts", "a different file, same name\n")
        (scope / "plugins").symlink_to(new.parent)
        return old, new

    def test_a_moved_file_edited_right_before_its_delete_guard_is_kept(self):
        # The hash is taken after the delete guard, right before the rm, so an edit up to that guard call still counts.
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        old, new = self.one_moved_row(sandbox, scope)
        tracker = write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", "plugins/x.ts", str(old),
                                                               sha256_file(old), NO_VALUE)))
        env = self.with_guard_hook(sandbox, "plugins-old/x.ts", 1, action=f"printf 'edited\\n' > '{old}'\n")

        result = run_setup("--global", "--uninstall", sandbox=sandbox, env=env)

        self.assert_exit(result, EXIT_OK)
        self.assertIn("[KEPT] plugins/x.ts (moved; the recorded hash does not match)", self.lines(result))
        self.assertEqual((old.read_text(), new.read_text()), ("edited\n", "a different file, same name\n"))
        self.assertTrue(tracker.exists())

    @unittest.skipIf(IS_ROOT, "root reads mode-000 files")
    def test_a_moved_file_that_cannot_be_hashed_is_kept(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        old, _new = self.one_moved_row(sandbox, scope)
        write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", "plugins/x.ts", str(old),
                                                     sha256_file(old), NO_VALUE)))
        old.chmod(0o000)
        self.addCleanup(lambda: os.path.lexists(old) and os.chmod(old, 0o644))

        result = run_setup("--global", "--uninstall", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertIn("[KEPT] plugins/x.ts (moved; the recorded hash does not match)", self.lines(result))
        self.assertTrue(os.path.lexists(old))

    def test_a_kept_row_in_a_repo_is_not_listed_as_a_removal(self):
        sandbox = self.new_sandbox()
        project = sandbox.make_repo(sandbox.project, files={})
        scope = link_scope(project / ".opencode")
        old, _new = self.one_moved_row(sandbox, scope)
        write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", "plugins/x.ts", str(old), DUMMY_HASH,
                                                     str(project))))

        result = run_setup("--project", project, "--uninstall", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        lines = self.lines(result)
        self.assertIn(f"[KEPT] plugins/x.ts (moved; the recorded hash does not match) (repo: {project})", lines)
        self.assertNotIn(f"Repo {project}", lines)
        self.assertEqual(old.read_text(), "ours\n")

    def test_an_uninstall_dry_run_blocks_where_the_real_run_blocks(self):
        def guard_fails(nth):
            # The command row's first guard call locates <scope>/<path>; its second checks the recorded realpath.
            def build(sandbox):
                self.install_commit_guard(sandbox)
                return self.with_guard_hook(sandbox, COMMAND_REL, nth, mode="fail"), [
                    f"[DRY-RUN] REMOVE {PAYLOAD_DIR} ok", f"[DRY-RUN] BLOCKED {COMMAND_REL} blocked:guard failed",
                    f"[DRY-RUN] REMOVE {TS_REL} ok"]
            return build

        def in_skills_md(sandbox):
            skills_md = sandbox.make_skills_md(start_submodule=False)
            scope = self.scope_of(sandbox, commands=skills_md / "commands")
            target = write(skills_md / "commands" / "x.md")
            write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", "commands/x.md", str(target),
                                                         sha256_file(target), str(skills_md))))
            return sandbox.env(), [f"[DRY-RUN] BLOCKED commands/x.md blocked:G1 (repo: {skills_md} {SKILLS_MD_ORIGIN})"]

        def in_a_submodule(sandbox):
            # approve: is not a block: it keeps its state, since the real run asks for the approval (or blocks) then.
            _superproject, dotfiles = sandbox.make_dotfiles_submodule()
            scope = self.scope_of(sandbox, plugins=dotfiles / "plugins")
            target = write(dotfiles / "plugins" / "opencode-commit-guard.ts")
            write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", TS_REL, str(target),
                                                         sha256_file(target), str(dotfiles))))
            return sandbox.env(), [
                f"[DRY-RUN] REMOVE {TS_REL} approve:{dotfiles} (repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})"]

        cases = {"the guard fails while locating a row": guard_fails(1),
                 "the guard fails on the recorded realpath": guard_fails(2),
                 "the recorded realpath is in a skills-md checkout": in_skills_md,
                 "the recorded realpath is in an unapproved submodule": in_a_submodule}
        for name, build in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                env, expected = build(sandbox)
                before = (snapshot(sandbox.home), snapshot(sandbox.fixtures))  # fixtures/ holds any superproject

                result = run_setup("--global", "--uninstall", "--dry-run", sandbox=sandbox, env=env)

                self.assert_exit(result, EXIT_OK)
                self.assertEqual(self.dry_run_lines(result), expected)
                self.assertEqual((snapshot(sandbox.home), snapshot(sandbox.fixtures)), before)

    def test_an_uninstall_dry_run_with_plugin_shows_only_that_plugins_rows(self):
        sandbox = self.new_sandbox()
        self.scope_of(sandbox)
        self.assert_exit(run_setup("--global", "--plugin", "commit-guard", "--plugin", "token-saver", sandbox=sandbox),
                         EXIT_OK)
        before = snapshot(sandbox.home)

        result = run_setup("--global", "--uninstall", "--plugin", "commit-guard", "--dry-run", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertEqual(self.dry_run_lines(result), [f"[DRY-RUN] REMOVE {PAYLOAD_DIR} ok",
                                                      f"[DRY-RUN] REMOVE {COMMAND_REL} ok",
                                                      f"[DRY-RUN] REMOVE {TS_REL} ok"])
        self.assertEqual(snapshot(sandbox.home), before)

    def test_uninstall_with_a_dir_name_removes_that_plugins_rows(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        self.assert_exit(run_setup("--global", "--plugin", "attribution", sandbox=sandbox), EXIT_OK)

        result = run_setup("--global", "--uninstall", "--plugin", "attribution", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertEqual(result.stderr, f"{ALIAS_WARN}\n")
        self.assertEqual([line for line in self.lines(result) if line.startswith("[REMOVED]")],
                         [f"[REMOVED] {ATTRIBUTION_COMMAND}", f"[REMOVED] {ATTRIBUTION_TS}"])
        self.assertFalse(os.path.lexists(scope / TRACKER_NAME))

    def test_a_partial_uninstall_keeps_the_header_and_its_approved_repos(self):
        sandbox = self.new_sandbox()
        _superproject, dotfiles = sandbox.make_dotfiles_submodule()
        scope = self.scope_of(sandbox, plugins=dotfiles / "plugins")
        self.assert_exit(run_setup("--global", "--plugin", "commit-guard", "--plugin", "token-saver", "--allow-repo",
                                   dotfiles, sandbox=sandbox), EXIT_OK)
        tracker = scope / TRACKER_NAME
        text = tracker.read_text()
        self.assertIn(f"# allowed_repos: {dotfiles}\n", text)

        partial = run_setup("--global", "--uninstall", "--plugin", "token-saver", sandbox=sandbox)

        self.assert_exit(partial, EXIT_OK)
        self.assertEqual(tracker.read_text(), "".join(f"{line}\n" for line in text.splitlines()
                                                      if line.startswith("#") or "\ttoken-saver\t" not in line))

        rest = run_setup("--global", "--uninstall", sandbox=sandbox)  # non-TTY: the header's approval is enough

        self.assert_exit(rest, EXIT_OK)
        self.assertIn(f"[REMOVED] {TS_REL} (repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})", self.lines(rest))


class PluginSelectionTests(InstallerTestCase):
    """--plugin names resolve through `convert.py list` before anything else runs."""

    # AC-055 (alias half; the E2E proof is LegacyMigrationTests after Task 3.1)
    def test_a_dir_name_installs_under_its_plugin_id_with_one_warn(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)

        result = run_setup("--global", "--plugin", "attribution", "--plugin", "attribution", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertEqual(result.stderr, f"{ALIAS_WARN}\n")
        lines = self.lines(result)
        for target_rel in (ATTRIBUTION_TS, ATTRIBUTION_COMMAND):
            self.assertIn(f"[OK] {target_rel}", lines)
        rows = [line.split("\t") for line in (scope / TRACKER_NAME).read_text().splitlines() if line[:1] != "#"]
        self.assertEqual(sorted((fields[1], fields[2]) for fields in rows),
                         [(ATTRIBUTION_ID, ATTRIBUTION_COMMAND), (ATTRIBUTION_ID, ATTRIBUTION_TS)])

    def test_list_with_a_dir_name_prints_the_header_and_only_that_plugins_rows(self):
        sandbox = self.new_sandbox()

        result = run_setup("--list", "--plugin", "attribution", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertEqual(result.stderr, f"{ALIAS_WARN}\n")
        self.assertEqual([line.split(None, 2) for line in self.lines(result)], [
            ["PLUGIN", "KIND", "SOURCE"],
            [ATTRIBUTION_ID, "plugins", "plugin-attribution/plugins/opencode-claude-attribution.ts"],
            [ATTRIBUTION_ID, "skills", "plugin-attribution/skills/claude-attribution"],
            [ATTRIBUTION_ID, "commands", "plugin-attribution/commands/claude-attribution.md"],
        ])

    def test_an_unknown_plugin_name_dies_before_anything_is_written(self):
        runs = [(action, "nope") for action in (("--global",), ("--global", "--dry-run"), ("--global", "--uninstall"),
                                                 ("--list",))]
        runs.append((("--global",), "-x"))  # passed on as a value, never as a convert.py option
        for action, name in runs:
            with self.subTest(action=action, name=name):
                sandbox = self.new_sandbox()
                self.scope_of(sandbox)
                before = snapshot(sandbox.home)

                result = run_setup(*action, "--plugin", "commit-guard", "--plugin", name, sandbox=sandbox)

                self.assert_exit(result, EXIT_FAILED)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.splitlines(), [
                    f"ERROR --plugin '{name}': plugin-unknown: names no OpenCode-compatible plugin; plugin ids: "
                    f"{REAL_IDS}",
                    "error: Unknown --plugin value; nothing was changed",
                ])
                self.assertEqual(snapshot(sandbox.home), before)

    def test_a_failed_or_empty_plugin_listing_stops_before_anything_is_written(self):
        cases = {
            "list fails": (1, f"error: convert.py list failed for {REPO_ROOT}"),
            "list prints no rows for the selection": (
                0, "error: convert.py list printed no rows for the --plugin selection; nothing was changed"),
        }
        for name, (code, message) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                self.scope_of(sandbox)
                before = snapshot(sandbox.home)

                result = run_setup(*COMMIT_GUARD, sandbox=sandbox, env=self.with_list_exit(sandbox, code))

                self.assert_exit(result, EXIT_FAILED)
                self.assertEqual(result.stderr.splitlines(), [message])
                self.assertEqual(snapshot(sandbox.home), before)

    def test_a_crashed_plugin_listing_dies_for_every_action(self):
        cases = {
            "--list, list exits 3": (("--list",), 3),
            "--list, list exits 127": (("--list",), 127),
            "an install without --plugin, list exits 3": (("--global",), 3),
            "an install with --plugin, list exits 127": (COMMIT_GUARD, 127),
            "--uninstall, list exits 1": (("--global", "--uninstall"), 1),
        }
        for name, (args, code) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                self.scope_of(sandbox)
                before = snapshot(sandbox.home)

                result = run_setup(*args, sandbox=sandbox, env=self.with_list_exit(sandbox, code))

                self.assert_exit(result, EXIT_FAILED)
                self.assertEqual((result.stdout, result.stderr.splitlines()),
                                 ("", [f"error: convert.py list failed for {REPO_ROOT}"]))
                self.assertEqual(snapshot(sandbox.home), before)

    def test_list_rows_line_up_under_the_header(self):
        sandbox = self.new_sandbox()
        listed = run_convert("list", "--repo", REPO_ROOT, "--plugin", "commit-guard", sandbox=sandbox)
        columns = [("PLUGIN", "KIND", "SOURCE")] + [tuple(line.split("|")) for line in listed.stdout.splitlines()]

        result = run_setup("--list", "--plugin", "commit-guard", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        self.assertEqual(len(columns), 5)  # the header and commit-guard's payload, plugins, skills, commands rows
        self.assertEqual(result.stdout, "".join(f"{plugin:<22} {kind:<10} {source}\n"
                                                for plugin, kind, source in columns))

    def test_list_over_an_empty_listing_prints_only_the_header(self):
        sandbox = self.new_sandbox()

        result = run_setup("--list", sandbox=sandbox, env=self.with_list_exit(sandbox, 0))

        self.assert_exit(result, EXIT_OK)
        self.assertEqual((result.stdout, result.stderr), (f"{'PLUGIN':<22} {'KIND':<10} SOURCE\n", ""))


class DryRunTests(InstallerTestCase):
    """--dry-run prints the one state each unit would reach and writes nothing."""

    def test_a_dry_run_prints_the_state_an_install_would_reach_per_unit(self):
        def blocked_payload(sandbox):
            skills_md = sandbox.make_skills_md(start_submodule=False)
            self.scope_of(sandbox, **{"llm-agent-workflow": skills_md / "lib"})
            return [f"[DRY-RUN] BLOCKED {PAYLOAD_DIR} blocked:G1 (repo: {skills_md} {SKILLS_MD_ORIGIN})",
                    f"[DRY-RUN] FAIL {TS_REL} ok (its payload unit was not installed)",
                    f"[DRY-RUN] NEW {COMMAND_REL} ok"]

        def wrong_types(sandbox):
            scope = self.scope_of(sandbox)
            write(scope / COMMAND_REL / "keep.txt")
            write(scope / PAYLOAD_DIR)
            return [f"[DRY-RUN] FAIL {PAYLOAD_DIR} ok (target is not a dir)",
                    f"[DRY-RUN] FAIL {TS_REL} ok (its payload unit was not installed)",
                    f"[DRY-RUN] FAIL {COMMAND_REL} ok (target is not a file)"]

        def conflict_same_update(sandbox):
            scope = self.scope_of(sandbox)
            write(scope / PAYLOAD_DIR / "hooks" / "user.txt")
            write(scope / TS_REL, SOURCES[TS_REL].read_text())
            command = write(scope / COMMAND_REL)
            write(scope / TRACKER_NAME, tracker_text(row("file", "commit-guard", COMMAND_REL, str(command),
                                                         sha256_file(command), NO_VALUE)))
            return [f"[DRY-RUN] CONFLICT {PAYLOAD_DIR} ok", f"[DRY-RUN] SAME {TS_REL} ok",
                    f"[DRY-RUN] UPDATE {COMMAND_REL} ok"]

        def approve_payload(sandbox):
            _superproject, dotfiles = sandbox.make_dotfiles_submodule()
            self.scope_of(sandbox, **{"llm-agent-workflow": dotfiles / "lib"})
            return [f"[DRY-RUN] NEW {PAYLOAD_DIR} approve:{dotfiles} (repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})",
                    f"[DRY-RUN] NEW {TS_REL} ok", f"[DRY-RUN] NEW {COMMAND_REL} ok"]

        cases = {"a blocked payload": blocked_payload, "targets of the wrong type": wrong_types,
                 "conflict, same and update": conflict_same_update, "a payload needing approval": approve_payload}
        for name, build in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                expected = build(sandbox)
                before = (snapshot(sandbox.home), snapshot(sandbox.fixtures))

                result = run_setup(*COMMIT_GUARD, "--dry-run", sandbox=sandbox)

                self.assert_exit(result, EXIT_OK)
                self.assertEqual(self.dry_run_lines(result), expected)
                self.assertEqual((snapshot(sandbox.home), snapshot(sandbox.fixtures)), before)

    def test_a_dry_run_leaves_a_missing_project_dir_missing(self):
        sandbox = self.new_sandbox()
        project = sandbox.root / "not-yet"
        before = (snapshot(sandbox.home), snapshot(sandbox.project))

        result = run_setup("--project", project, "--plugin", "commit-guard", "--dry-run", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        lines = self.lines(result)
        self.assertIn(f"[dry-run] would mkdir -p {project}", lines)
        self.assertEqual(self.dry_run_lines(result), [f"[DRY-RUN] NEW {PAYLOAD_DIR} ok", f"[DRY-RUN] NEW {TS_REL} ok",
                                                      f"[DRY-RUN] NEW {COMMAND_REL} ok"])
        self.assertFalse(os.path.lexists(project))
        self.assertEqual((snapshot(sandbox.home), snapshot(sandbox.project)), before)

    def test_a_dry_run_conflict_prints_only_its_dry_run_line(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        write(scope / COMMAND_REL)

        result = run_setup(*COMMIT_GUARD, "--dry-run", sandbox=sandbox)

        self.assert_exit(result, EXIT_OK)
        lines = self.lines(result)
        self.assertIn(f"[DRY-RUN] CONFLICT {COMMAND_REL} ok", lines)
        self.assertEqual([line for line in lines if line.startswith("[CONFLICT]")], [])


class DoubleLoadNoticeTests(InstallerTestCase):
    """A plugin file recorded in the other scope's tracker gets one report-only WARN."""

    def other_tracker(self, scope_root, *target_rels):
        rows = (row("file", "commit-guard", target_rel, str(scope_root / target_rel), DUMMY_HASH, NO_VALUE)
                for target_rel in target_rels)
        return write(scope_root / TRACKER_NAME, tracker_text(*rows))

    # AC-054 (double-load half; the E2E proof is NoticeTests with Task 5.4)
    def test_a_plugin_file_in_the_other_scopes_tracker_gets_one_warn_and_nothing_else_changes(self):
        def global_run_from_a_project(sandbox):
            tracker = self.other_tracker(sandbox.project / ".opencode", TS_REL, COMMAND_REL,
                                         "plugins/opencode-token-saver.ts")
            return ("--global",), sandbox.project, {}, tracker, [double_load_warn(TS_REL, "project")]

        def project_run(sandbox):
            tracker = self.other_tracker(sandbox.home / ".config" / "opencode", TS_REL)
            return ("--project", sandbox.project), sandbox.root, {}, tracker, [double_load_warn(TS_REL, "global")]

        def project_run_with_xdg_config_home(sandbox):
            tracker = self.other_tracker(sandbox.root / "xdg" / "opencode", TS_REL)
            return (("--project", sandbox.project), sandbox.root, {"XDG_CONFIG_HOME": sandbox.root / "xdg"}, tracker,
                    [double_load_warn(TS_REL, "global")])

        def global_run_with_its_own_row_only(sandbox):
            self.other_tracker(sandbox.home / ".config" / "opencode", TS_REL)
            return ("--global",), sandbox.root, {}, sandbox.root / ".opencode" / TRACKER_NAME, []

        cases = {"a global run from a project dir": global_run_from_a_project, "a project run": project_run,
                 "a project run with XDG_CONFIG_HOME set": project_run_with_xdg_config_home,
                 "a global run whose own tracker records the file": global_run_with_its_own_row_only}
        for name, build in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                self.scope_of(sandbox)
                scope_args, cwd, overrides, other, warnings = build(sandbox)
                other_before = other.read_bytes() if other.exists() else None
                env = sandbox.env(**overrides)

                result = run_setup(*scope_args, "--plugin", "commit-guard", sandbox=sandbox, env=env, cwd=cwd)

                self.assert_exit(result, EXIT_OK)
                self.assertEqual([line for line in result.stderr.splitlines() if "also installed" in line], warnings)
                self.assertIn(f"[OK] {TS_REL}", self.lines(result))
                self.assertEqual(other.read_bytes() if other.exists() else None, other_before)

    def test_a_dry_run_prints_the_warn_before_the_unit_lines_and_writes_nothing(self):
        sandbox = self.new_sandbox()
        self.scope_of(sandbox)
        self.other_tracker(sandbox.project / ".opencode", TS_REL)
        before = (snapshot(sandbox.home), snapshot(sandbox.project))

        # stderr joins stdout, so the WARN's place among the [DRY-RUN] lines is observable
        result = subprocess.run(["bash", str(SETUP_SCRIPT), *COMMIT_GUARD, "--dry-run"], cwd=sandbox.project,
                                env=sandbox.env(), input="", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=RUN_TIMEOUT_SECONDS)

        self.assertEqual(result.returncode, EXIT_OK, result.stdout)
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith(("WARN", "[DRY-RUN]"))], [
            double_load_warn(TS_REL, "project"),
            f"[DRY-RUN] NEW {PAYLOAD_DIR} ok",
            f"[DRY-RUN] NEW {TS_REL} ok",
            f"[DRY-RUN] NEW {COMMAND_REL} ok",
        ])
        self.assertEqual((snapshot(sandbox.home), snapshot(sandbox.project)), before)

    def test_a_v1_row_in_the_other_scopes_tracker_gets_the_warn(self):
        # The live machine's global tracker still holds v1 rows, which read back as plugin "?" and hash "-".
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        tracker = write(sandbox.project / ".opencode" / TRACKER_NAME,
                        "".join(f"{line}\n" for line in (*V1_TRACKER_HEADER, TS_REL)))
        tracker_before = tracker.read_bytes()

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox, cwd=sandbox.project)

        self.assert_exit(result, EXIT_OK)
        self.assertEqual([line for line in result.stderr.splitlines() if "also installed" in line],
                         [double_load_warn(TS_REL, "project")])
        self.assertTrue((scope / TS_REL).is_file())
        self.assertEqual(tracker.read_bytes(), tracker_before)

    def test_an_invalid_other_scope_tracker_is_a_warning_and_the_install_goes_on(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        tracker = write(sandbox.project / ".opencode" / TRACKER_NAME,
                        tracker_text(row("file", "commit-guard", "/etc/passwd", "/etc/passwd", DUMMY_HASH, NO_VALUE)))
        tracker_before = tracker.read_bytes()

        result = run_setup(*COMMIT_GUARD, sandbox=sandbox, cwd=sandbox.project)

        self.assert_exit(result, EXIT_OK)
        self.assertIn(f"WARN {tracker}: double-load: the project scope tracker is invalid; its plugin files were not "
                      "checked", result.stderr.splitlines())
        self.assertNotIn("also installed", result.stderr)
        self.assertTrue((scope / TS_REL).is_file())
        self.assertEqual(tracker.read_bytes(), tracker_before)


@unittest.skipUnless(hasattr(os, "openpty"), "needs a pty")
class TtyPromptTests(InstallerTestCase):
    """The approval and CONFLICT prompts, answered on a real terminal (a pty as stdin)."""

    def run_tty(self, sandbox, answers, *args):
        """Run the installer with a pty as stdin; ANSWERS, then EOF keys, are typed before it starts reading."""
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        try:
            process = subprocess.Popen(["bash", str(SETUP_SCRIPT), *map(str, args)], stdin=slave,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=sandbox.env(),
                                       cwd=sandbox.root, start_new_session=True)
        finally:
            os.close(slave)
        os.write(master, answers + EOF_KEYS)
        try:
            stdout, stderr = process.communicate(timeout=TTY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
            self.fail(f"the installer was still waiting for input after {TTY_TIMEOUT_SECONDS}s")
        return subprocess.CompletedProcess(process.args, process.returncode, stdout.decode(), stderr.decode())

    def dotfiles_world(self, sandbox):
        superproject, dotfiles = sandbox.make_dotfiles_submodule()
        scope = self.scope_of(sandbox, plugins=dotfiles / "plugins", commands=dotfiles / "commands")
        prompt = f"Write into {dotfiles} (submodule of {superproject})? You commit these. [y/N] "
        return scope, dotfiles, prompt

    def test_one_approval_per_repo_on_a_tty(self):
        with self.subTest("y"):
            sandbox = self.new_sandbox()
            scope, dotfiles, prompt = self.dotfiles_world(sandbox)

            result = self.run_tty(sandbox, b"y\n", *COMMIT_GUARD)

            self.assert_exit(result, EXIT_OK)
            self.assertEqual(result.stderr.count(prompt), 1, result.stderr)
            self.assertEqual(result.stderr.count("[y/N]"), 1, result.stderr)
            lines = self.lines(result)
            for target_rel in (TS_REL, COMMAND_REL):
                self.assertIn(f"[OK] {target_rel} (repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})", lines)
            self.assertIn(f"# allowed_repos: {dotfiles}", (scope / TRACKER_NAME).read_text().splitlines())

        with self.subTest("n"):
            sandbox = self.new_sandbox()
            scope, dotfiles, prompt = self.dotfiles_world(sandbox)
            before = snapshot(dotfiles)

            result = self.run_tty(sandbox, b"n\n", *COMMIT_GUARD)

            self.assert_exit(result, EXIT_FAILED)
            self.assertEqual(result.stderr.count(prompt), 1, result.stderr)
            self.assertEqual(result.stderr.count("[y/N]"), 1, result.stderr)
            lines = self.lines(result)
            for target_rel in (TS_REL, COMMAND_REL):
                self.assertIn(f"[BLOCKED] {target_rel}: G3 needs approval; re-run with --allow-repo {dotfiles} "
                              f"(repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})", lines)
            self.assertEqual(snapshot(dotfiles), before)

    def test_conflict_overwrite_or_skip_on_a_tty(self):
        cases = {
            "o": (b"o\n", f"[OVERWRITE] {COMMAND_REL}", SOURCES[COMMAND_REL].read_text(), [USER_TEXT]),
            "s": (b"s\n", f"[SKIP] {COMMAND_REL}", USER_TEXT, []),
        }
        for name, (answers, line, content, backups) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                scope = self.scope_of(sandbox)
                target = write(scope / COMMAND_REL)

                result = self.run_tty(sandbox, answers, *COMMIT_GUARD)

                self.assert_exit(result, EXIT_OK)
                self.assertEqual(result.stderr.count(f"  {COMMAND_REL} differs"), 1, result.stderr)
                lines = self.lines(result)
                self.assertLess(lines.index(f"[CONFLICT] {COMMAND_REL}"), lines.index(line))
                self.assertEqual(target.read_text(), content)
                backup_files = scope.glob(f"llm-agent-workflow/.backup/*/{COMMAND_REL}")
                self.assertEqual([path.read_text() for path in backup_files], backups)

    def test_conflict_abort_or_eof_on_a_tty_keeps_the_rows_written_before_it(self):
        for name, answers in {"a": b"a\n", "EOF": b""}.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                scope = self.scope_of(sandbox)
                target = write(scope / COMMAND_REL)

                result = self.run_tty(sandbox, answers, *COMMIT_GUARD)

                self.assert_exit(result, EXIT_FAILED)
                self.assertEqual(result.stderr.count(f"  {COMMAND_REL} differs"), 1, result.stderr)
                self.assertIn("Aborted by user", result.stderr)
                self.assertEqual(target.read_text(), USER_TEXT)
                rows = [line.split("\t")[2] for line in (scope / TRACKER_NAME).read_text().splitlines()
                        if not line.startswith("#")]
                self.assertEqual(sorted(rows), [PAYLOAD_DIR, TS_REL])


if __name__ == "__main__":
    unittest.main()
