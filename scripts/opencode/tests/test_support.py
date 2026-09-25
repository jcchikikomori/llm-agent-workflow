#!/usr/bin/env python3
"""Self-tests for the shared harness in _support.py.

Every integration and E2E suite in this directory builds on these fixtures,
so each fixture is proven here against real git, real symlinks and the real
setup-opencode.sh, inside a temp sandbox.

  python3 -m unittest discover -s scripts/opencode/tests
"""

import os
import shutil
import subprocess
import unittest
from unittest import mock

import _support
from _support import REPO_ROOT, Sandbox, link_scope, run_convert, run_setup, snapshot, write_stub_converter

HELLO_SHA256 = "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"  # sha256 of b"hello\n"
SKILL_BODY = "---\nname: start\ndescription: 'Fixture start skill.'\n---\n\n# Start\n"


class SandboxTestCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def git_out(self, cwd, *args):
        return self.sb.git(cwd, *args).stdout.strip()


class SandboxLayoutTests(SandboxTestCase):
    def test_sandbox_dirs_live_under_one_realpath_root_outside_the_repo(self):
        sb = self.sb
        self.assertEqual(str(sb.root), os.path.realpath(sb.root))
        self.assertNotIn(REPO_ROOT, sb.root.parents)
        self.assertEqual(
            [sb.home, sb.project, sb.bin, sb.tools, sb.fixtures, sb.stub_log],
            [sb.root / "home", sb.root / "project", sb.root / "bin", sb.root / "tools", sb.root / "fixtures",
             sb.root / "calls.log"],
        )
        self.assertTrue(all(path.is_dir() for path in (sb.home, sb.project, sb.bin, sb.tools, sb.fixtures)))

    def test_leaving_the_context_removes_the_root(self):
        with Sandbox() as sb:
            root = sb.root

        self.assertFalse(root.exists())

    def test_repo_root_points_at_this_checkout(self):
        self.assertTrue((REPO_ROOT / ".claude-plugin" / "marketplace.json").is_file())
        self.assertTrue((REPO_ROOT / "setup-opencode.sh").is_file())


class SandboxEnvTests(SandboxTestCase):
    def test_env_values_hold_no_real_home_path(self):
        real_homes = {os.path.expanduser("~"), os.path.realpath(os.path.expanduser("~"))}

        env = self.sb.env()

        offenders = sorted(key for key, value in env.items() if any(home in value for home in real_homes))
        self.assertEqual(offenders, [])

    def test_env_sets_the_isolated_keys(self):
        sb = self.sb

        env = sb.env()

        self.assertEqual(env["HOME"], str(sb.root / "home"))
        self.assertEqual(env["PATH"], f"{sb.root / 'bin'}:{sb.root / 'tools'}")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(sb.root / "gitconfig"))
        self.assertEqual(env["STUB_LOG"], str(sb.root / "calls.log"))
        self.assertEqual(env["LC_ALL"], "C")

    def test_env_drops_the_callers_xdg_dirs(self):
        caller_xdg = {"XDG_CONFIG_HOME": "/x/config", "XDG_CACHE_HOME": "/x/cache", "XDG_DATA_HOME": "/x/data"}

        with mock.patch.dict(os.environ, caller_xdg):
            env = self.sb.env()

        self.assertEqual(sorted(key for key in env if key.startswith("XDG_")), [])

    def test_env_is_built_from_scratch_and_inherits_nothing_from_the_caller(self):
        sentinels = {"GIT_DIR": "/x/.git", "GIT_INDEX_FILE": "/x/index", "GIT_CONFIG_PARAMETERS": "'a.b=c'",
                     "LEAK_SENTINEL": "leak"}

        with mock.patch.dict(os.environ, sentinels):
            env = self.sb.env()

        self.assertEqual(sorted(env), [
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT", "HOME", "LC_ALL",
            "PATH", "PYTHONDONTWRITEBYTECODE", "STUB_LOG", "TMPDIR",
        ])
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_env_overrides_add_values_and_none_removes_a_key(self):
        cache = str(self.sb.root / "cache")

        env = self.sb.env(XDG_CACHE_HOME=cache, STUB_LOG=None)

        self.assertEqual(env["XDG_CACHE_HOME"], cache)
        self.assertNotIn("STUB_LOG", env)

    def test_global_git_config_is_the_temp_file_only(self):
        sb = self.sb

        global_list = self.git_out(sb.root, "config", "--global", "--list")
        origins = self.git_out(sb.root, "config", "--list", "--show-origin").splitlines()

        self.assertEqual(global_list.splitlines(), [
            "user.name=Sandbox Test",
            "user.email=sandbox@example.invalid",
            "init.defaultbranch=main",
            "commit.gpgsign=false",
            "tag.gpgsign=false",
            "protocol.file.allow=always",
        ])
        self.assertEqual({line.split("\t")[0] for line in origins}, {f"file:{sb.root / 'gitconfig'}"})


class ToolsTests(SandboxTestCase):
    def test_tools_link_the_host_binaries_the_installer_needs(self):
        linked = {path.name for path in self.sb.tools.iterdir()}

        self.assertLessEqual({"bash", "sh", "python3", "git", "sed", "awk", "sha256sum", "rmdir", "diff"}, linked)
        self.assertTrue(all((self.sb.tools / name).is_symlink() for name in linked))

    def test_sandbox_python3_runs_with_the_sandbox_home(self):
        result = subprocess.run(["python3", "-c", "import os, sys; print(sys.version_info[0], os.environ['HOME'])"],
                                env=self.sb.env(), capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"3 {self.sb.home}\n")

    def test_tools_without_rejects_a_name_outside_the_tool_set(self):
        with self.assertRaisesRegex(ValueError, "gti"):
            self.sb.tools_without("git", "gti")

    def test_tools_without_gives_a_path_missing_the_named_tool(self):
        path = self.sb.tools_without("git")

        self.assertIsNone(shutil.which("git", path=path))
        self.assertIsNotNone(shutil.which("python3", path=path))
        self.assertIsNotNone(shutil.which("git", path=self.sb.env()["PATH"]))

    def test_run_setup_help_succeeds_on_the_isolated_path(self):
        result = run_setup("--help", sandbox=self.sb)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("--global", result.stdout)
        self.assertEqual(result.args, [str(self.sb.tools / "bash"), str(REPO_ROOT / "setup-opencode.sh"), "--help"])

    def test_run_setup_help_reports_a_missing_tool_on_stderr(self):
        env = self.sb.env(PATH=self.sb.tools_without("sed"))

        result = run_setup("--help", sandbox=self.sb, env=env)

        self.assertIn("sed", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_run_convert_calls_the_repo_convert_py_with_the_sandbox_python(self):
        result = run_convert("--help", sandbox=self.sb)

        self.assertEqual(
            result.args,
            [str(self.sb.tools / "python3"), str(REPO_ROOT / "scripts" / "opencode" / "convert.py"), "--help"],
        )

    def test_stub_records_its_argv_in_the_stub_log(self):
        self.sb.stub("docker")

        subprocess.run(["docker", "run", "--rm", "img"], env=self.sb.env(), check=True)

        self.assertEqual(self.sb.calls(), ["docker run --rm img"])

    def test_calls_is_empty_before_any_stub_runs(self):
        self.assertEqual(self.sb.calls(), [])


class GitFixtureTests(SandboxTestCase):
    def test_make_repo_makes_one_unsigned_commit_with_the_origin_and_files(self):
        repo = self.sb.make_repo(self.sb.fixtures / "plain", origin="https://example.invalid/plain.git",
                                 files={"a/b.txt": "x\n"})

        self.assertEqual(repo, self.sb.fixtures / "plain")
        self.assertEqual(self.git_out(repo, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(self.git_out(repo, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git_out(repo, "remote", "get-url", "origin"), "https://example.invalid/plain.git")
        self.assertEqual(self.git_out(repo, "log", "-1", "--format=%an <%ae>"),
                         "Sandbox Test <sandbox@example.invalid>")
        self.assertNotIn("gpgsig", self.git_out(repo, "cat-file", "commit", "HEAD"))
        self.assertEqual((repo / "a" / "b.txt").read_text(), "x\n")
        self.assertEqual(self.git_out(repo, "status", "--porcelain"), "")

    def test_make_repo_without_an_origin_has_no_remote(self):
        repo = self.sb.make_repo(self.sb.project)

        self.assertEqual(self.git_out(repo, "remote"), "")

    def test_git_failure_raises_with_the_git_stderr(self):
        with self.assertRaisesRegex(_support.GitError, "not a git repository"):
            self.sb.git(self.sb.root, "rev-parse", "--show-toplevel")

    def test_make_dotfiles_submodule_reports_its_superproject_and_credentialed_origin(self):
        superproject, submodule = self.sb.make_dotfiles_submodule()

        self.assertEqual(self.git_out(submodule, "rev-parse", "--show-superproject-working-tree"), str(superproject))
        self.assertEqual(self.git_out(submodule, "rev-parse", "--show-toplevel"), str(submodule))
        self.assertEqual(self.git_out(submodule, "remote", "get-url", "origin"),
                         "https://user:token@example.invalid/dotfiles.git")

    def test_make_skills_md_nested_has_skills_dir_and_a_start_submodule(self):
        repo = self.sb.make_skills_md(layout="nested")

        self.assertTrue((repo / "skills" / "alpha" / "SKILL.md").is_file())
        self.assertEqual(self.git_out(repo, "submodule", "status").split()[1], "start")
        self.assertEqual(self.git_out(repo, "remote", "get-url", "origin"),
                         "https://example.invalid/jcchikikomori/skills-md.git")
        self.assertEqual(self.git_out(repo / "start", "remote", "get-url", "origin"),
                         "https://example.invalid/jcchikikomori/skills-md-dev-orchestrator.git")
        self.assertEqual(self.git_out(repo / "start", "rev-parse", "--show-superproject-working-tree"), str(repo))
        self.assertEqual(self.git_out(repo, "status", "--porcelain"), "")

    def test_make_skills_md_root_layout_without_start_puts_skills_at_the_top(self):
        repo = self.sb.make_skills_md(layout="root", start_submodule=False)

        self.assertTrue((repo / "alpha" / "SKILL.md").is_file())
        self.assertFalse((repo / "skills").exists())
        self.assertFalse((repo / ".gitmodules").exists())

    def test_make_skills_md_commits_the_given_skills(self):
        repo = self.sb.make_skills_md(skills={"start": SKILL_BODY}, start_submodule=False)

        self.assertEqual((repo / "skills" / "start" / "SKILL.md").read_text(), SKILL_BODY)
        self.assertEqual(self.git_out(repo, "ls-files"), "README.md\nskills/start/SKILL.md")

    def test_make_skills_md_rejects_an_unknown_layout(self):
        with self.assertRaisesRegex(ValueError, "layout"):
            self.sb.make_skills_md(layout="flat")

    def test_make_file_remote_is_a_bare_repo_that_clones_and_pulls(self):
        remote = self.sb.fixtures / "remote.git"

        url = self.sb.make_file_remote(remote)
        self.sb.git(self.sb.root, "clone", url, "clone")
        pulled = self.sb.git(self.sb.root / "clone", "pull", "--ff-only")

        self.assertEqual(url, remote.as_uri())
        self.assertEqual(self.git_out(remote, "rev-parse", "--is-bare-repository"), "true")
        self.assertTrue((self.sb.root / "clone" / "README.md").is_file())
        self.assertEqual(pulled.returncode, 0)

    def test_make_file_remote_from_a_source_repo_serves_its_tree(self):
        source = self.sb.make_skills_md(start_submodule=False)

        url = self.sb.make_file_remote(self.sb.fixtures / "skills-md.git", source=source)
        self.sb.git(self.sb.root, "clone", url, "clone")

        self.assertTrue((self.sb.root / "clone" / "skills" / "alpha" / "SKILL.md").is_file())

    def test_cloning_a_missing_file_remote_fails_fast(self):
        missing = (self.sb.root / "missing.git").as_uri()

        result = self.sb.git(self.sb.root, "clone", missing, "clone", check=False)

        self.assertEqual(result.returncode, 128)
        self.assertFalse((self.sb.root / "clone").exists())


class LinkScopeTests(SandboxTestCase):
    def test_link_scope_creates_the_scope_and_symlinks_into_created_targets(self):
        _, dotfiles = self.sb.make_dotfiles_submodule()
        scope = self.sb.home / ".config" / "opencode"
        plugins = dotfiles / ".config" / "opencode" / "plugins"
        agents = dotfiles / ".config" / "opencode" / "agents"

        result = link_scope(scope, plugins=plugins, agents=agents)

        self.assertEqual(result, scope)
        self.assertTrue((scope / "plugins").is_symlink())
        self.assertEqual(os.path.realpath(scope / "plugins"), str(plugins))
        self.assertEqual(os.path.realpath(scope / "agents"), str(agents))
        self.assertTrue(plugins.is_dir() and agents.is_dir())

    def test_link_scope_refuses_to_replace_an_existing_entry(self):
        scope = self.sb.home / ".config" / "opencode"
        (scope / "plugins").mkdir(parents=True)

        with self.assertRaises(FileExistsError):
            link_scope(scope, plugins=self.sb.fixtures / "plugins")


class SnapshotTests(SandboxTestCase):
    def make_linked_scope(self):
        target = self.sb.fixtures / "dotfiles-plugins"
        scope = link_scope(self.sb.home / ".config" / "opencode", plugins=target)
        (target / "a.ts").write_text("hello\n")
        return scope, target

    def test_snapshot_records_the_link_and_the_files_behind_it(self):
        scope, target = self.make_linked_scope()

        snap = snapshot(scope)

        self.assertEqual(snap, {
            str(scope): "dir",
            str(scope / "plugins"): f"symlink:{target}",
            str(target): "dir",
            str(target / "a.ts"): HELLO_SHA256,
        })

    def test_snapshot_is_equal_after_read_only_actions(self):
        scope, _ = self.make_linked_scope()
        before = snapshot(scope)

        help_run = run_setup("--help", sandbox=self.sb, cwd=scope)
        content = (scope / "plugins" / "a.ts").read_text()

        self.assertEqual(help_run.returncode, 0, help_run.stderr)
        self.assertEqual(content, "hello\n")
        self.assertEqual(snapshot(scope), before)

    def test_snapshot_changes_when_a_file_behind_a_symlinked_dir_changes(self):
        scope, target = self.make_linked_scope()
        before = snapshot(scope)

        (target / "a.ts").write_text("changed\n")
        after = snapshot(scope)

        self.assertEqual({key for key in before if before[key] != after[key]}, {str(target / "a.ts")})

    def test_snapshot_of_a_repo_is_unchanged_by_read_only_git_queries(self):
        superproject, submodule = self.sb.make_dotfiles_submodule()
        # A stat-only change makes `git status` want to refresh .git/index;
        # the sandbox env must keep that query read-only.
        os.utime(superproject / "README.md", (1, 1))
        before = snapshot(superproject)

        self.sb.git(superproject, "status", "--porcelain")
        self.sb.git(superproject, "submodule", "status")
        self.sb.git(submodule, "status", "--porcelain")

        self.assertEqual(snapshot(superproject), before)

    def test_snapshot_stops_at_a_symlink_cycle(self):
        loop = self.sb.fixtures / "loop"
        loop.mkdir()
        (loop / "self").symlink_to(loop)

        snap = snapshot(loop)

        self.assertEqual(snap, {str(loop): "dir", str(loop / "self"): f"symlink:{loop}"})

    def test_snapshot_of_a_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            snapshot(self.sb.root / "absent")


class StubConverterTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.skills_md = self.sb.fixtures / "skills-md"
        self.src = self.skills_md / "skills" / "alpha"
        self.src.mkdir(parents=True)
        (self.src / "SKILL.md").write_text("hello\n")
        self.dest = self.sb.root / "validate" / "alpha"

    def run_stub(self, stub, **env):
        return subprocess.run([str(stub), str(self.src), str(self.dest)], env=self.sb.env(**env),
                              capture_output=True, text=True)

    def test_write_stub_converter_writes_an_executable_at_the_skills_md_path(self):
        stub = write_stub_converter(self.skills_md)

        self.assertEqual(stub, self.skills_md / "scripts" / "opencode-convert-skill.sh")
        self.assertTrue(os.access(stub, os.X_OK))

    def test_stub_appends_src_dest_and_copies_the_skill(self):
        stub = write_stub_converter(self.skills_md)

        result = self.run_stub(stub)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sb.stub_log.read_text(), f"{self.src} {self.dest}\n")
        self.assertEqual((self.dest / "SKILL.md").read_text(), "hello\n")

    def test_stub_exits_with_stub_convert_exit(self):
        stub = write_stub_converter(self.skills_md)

        result = self.run_stub(stub, STUB_CONVERT_EXIT="1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.sb.calls(), [f"{self.src} {self.dest}"])

    def test_stub_fails_when_stub_log_is_unset(self):
        stub = write_stub_converter(self.skills_md)

        result = self.run_stub(stub, STUB_LOG=None)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("STUB_LOG", result.stderr)


if __name__ == "__main__":
    unittest.main()
