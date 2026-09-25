#!/usr/bin/env python3
# Install unit manifest Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.5)
# Generated: 2026-09-25 (Task 1.3a) | Suites row "test_units.py"; E2E for the installer lives in test_setup_opencode.py
"""Integration tests for `convert.py plan` and `convert.py hash`, plus unit cases for units.py.

Each integration test runs the real CLI in a subprocess:

  python3 scripts/opencode/convert.py plan --repo DIR --scope-root DIR --scope global|project --stage DIR
                                           [--project-dir DIR] [--plugin ID]... [--allow-repo DIR]...
  python3 scripts/opencode/convert.py hash PATH...

`plan` stdout: plugin<TAB>unit<TAB>stage_rel<TAB>target_rel<TAB>sha256:<hex><TAB>realpath<TAB>repo<TAB>verdict per
unit; exit 0, 3 when some units were rejected (the others are staged), 1 fatal, 2 usage. `hash` prints
sha256:<hex><TAB>PATH per PATH and exits 1 on a missing path.

  python3 -m unittest discover -s scripts/opencode/tests
"""

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import unittest
from pathlib import Path

from _support import REPO_ROOT, Sandbox, link_scope, run_convert, snapshot

# Harness: one _support.Sandbox per test (or per subTest). The scope root is <sandbox home>/.config/opencode and the
# stage is <sandbox>/stage, both outside the repo. Tests that need __pycache__/, evals/, symlinks or fifos inside a
# plugin build a fixture repo under the sandbox (copied plugin dirs plus a trimmed mapping.json), never inside this
# repo. Real git for the verdict test (Mock Boundary "git repo queries (guard): No").

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2
EXIT_REJECTED = 3
NO_VALUE = "-"
PLAN_COLUMN_COUNT = 8
PAYLOAD_NAMESPACE = "llm-agent-workflow"
PAYLOAD_PLUGINS = ("commit-guard", "markdown-format", "markdown-lsp", "memory-guard", "mempalace-docker", "qa",
                   "ruby-lsp", "token-saver")
FILE_KIND_ORDER = ("plugins", "agents", "commands")
EXCLUDED_FRAGMENTS = ("__pycache__", ".pyc", "evals", "hooks/vendor")
EXCLUDED_DIR_NAMES = ("__pycache__", "evals")
HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT_GUARD_UNITS = [
    ("commit-guard", "dir", "llm-agent-workflow/commit-guard"),
    ("commit-guard", "file", "plugins/opencode-commit-guard.ts"),
    ("commit-guard", "file", "commands/commit-guard.md"),
]


def import_units():
    """Import scripts/opencode/units.py without writing __pycache__/ into the repo."""
    scripts_dir = str(REPO_ROOT / "scripts" / "opencode")
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, scripts_dir)
    try:
        import units
    finally:
        sys.path.remove(scripts_dir)
        sys.dont_write_bytecode = previous
    return units


def plugin_json_names(repo=REPO_ROOT):
    return {json.loads(path.read_text())["name"] for path in repo.glob("plugin-*/.claude-plugin/plugin.json")}


def plugin_dirs_by_name(repo=REPO_ROOT):
    return {json.loads(path.read_text())["name"]: path.parent.parent
            for path in repo.glob("plugin-*/.claude-plugin/plugin.json")}


def rows_of(result):
    return [line.split("\t") for line in result.stdout.splitlines()]


def units_of(result):
    return [(row[0], row[1], row[3]) for row in rows_of(result)]


def files_below(root):
    """Every file path below ROOT, relative and in POSIX form."""
    return sorted(path.relative_to(root).as_posix() for path in Path(root).rglob("*") if not path.is_dir())


def excluded_dirs_below(root):
    """Every dir below ROOT, empty ones included, that is named like an always-excluded dir."""
    return sorted(os.path.relpath(os.path.join(parent, name), root) for parent, dirs, _files in os.walk(root)
                  for name in dirs if name in EXCLUDED_DIR_NAMES)


def is_executable(path):
    return bool(os.stat(path).st_mode & stat.S_IXUSR)


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def make_fixture_repo(sandbox, plugin_dirs, payloads):
    """A repo under the sandbox: copies of PLUGIN_DIRS (without their on-disk caches) and a mapping.json whose
    payloads are the real entries for PAYLOADS, with no extra_sources."""
    repo = sandbox.fixtures / "repo"
    for name in plugin_dirs:
        shutil.copytree(REPO_ROOT / name, repo / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "evals"))
    data = json.loads((REPO_ROOT / "scripts" / "opencode" / "mapping.json").read_text())
    data["payloads"] = {plugin_id: data["payloads"][plugin_id] for plugin_id in payloads}
    data["extra_sources"] = {}
    mapping_path = repo / "scripts" / "opencode" / "mapping.json"
    mapping_path.parent.mkdir(parents=True)
    mapping_path.write_text(json.dumps(data))
    return repo


def write(path, text=""):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def empty_dir(path):
    path = Path(path)
    path.mkdir(parents=True)
    return path


def link_to(link, target):
    Path(link).symlink_to(target)
    return Path(link)


class PlanTestCase(unittest.TestCase):
    def new_sandbox(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        return sandbox

    def scope_of(self, sandbox):
        scope = sandbox.home / ".config" / "opencode"
        scope.mkdir(parents=True, exist_ok=True)
        return scope

    def new_stage(self, sandbox):
        stage = sandbox.root / "stage"
        stage.mkdir()
        return stage

    def plan(self, sandbox, *args, repo=REPO_ROOT, scope=None, stage=None, scope_args=("--scope", "global")):
        scope = self.scope_of(sandbox) if scope is None else scope
        stage = self.new_stage(sandbox) if stage is None else stage
        return run_convert("plan", "--repo", repo, "--scope-root", scope, *scope_args, "--stage", stage, *args,
                           sandbox=sandbox)

    def assert_fatal(self, result, stderr_line):
        self.assertEqual((result.returncode, result.stdout), (EXIT_FATAL, ""), result.stderr)
        self.assertEqual(result.stderr, f"{stderr_line}\n")


class PlanRepoTests(PlanTestCase):
    """`plan` over this repo: the manifest rows, their order, the staged content and the --plugin filter."""

    # AC-020 (unit level), AC-055 (plugin.json ids in the manifest)
    def test_plan_over_the_repo_lists_every_unit_once_in_manifest_order(self):
        sandbox = self.new_sandbox()
        scope = self.scope_of(sandbox)
        stage = self.new_stage(sandbox)

        result = self.plan(sandbox, scope=scope, stage=stage)

        self.assertEqual((result.returncode, result.stderr), (EXIT_OK, ""))
        rows = rows_of(result)
        self.assertTrue(rows)
        self.assertEqual({len(row) for row in rows}, {PLAN_COLUMN_COUNT})
        self.assertLessEqual({row[0] for row in rows}, plugin_json_names())
        self.assertEqual([(row[0], row[3]) for row in rows if row[1] == "dir"],
                         [(plugin_id, f"{PAYLOAD_NAMESPACE}/{plugin_id}") for plugin_id in PAYLOAD_PLUGINS])
        unit_column = [row[1] for row in rows]
        self.assertEqual(unit_column, ["dir"] * len(PAYLOAD_PLUGINS) + ["file"] * (len(rows) - len(PAYLOAD_PLUGINS)))
        file_keys = [(FILE_KIND_ORDER.index(row[3].split("/")[0]), row[0], row[3]) for row in rows if row[1] == "file"]
        self.assertEqual(file_keys, sorted(file_keys))
        self.assertEqual([row[3] for row in rows if row[3].startswith("agents/")],
                         ["agents/opencode-gh-issue-to-pr.md"])
        self.assertIn("plugins/opencode-wandavision.ts", [row[3] for row in rows])
        for plugin, unit, stage_rel, target_rel, digest, realpath, repo, verdict in rows:
            with self.subTest(target_rel):
                self.assertEqual(stage_rel, target_rel)
                self.assertTrue((stage / stage_rel).exists())
                self.assertRegex(digest, HASH_PATTERN)
                self.assertEqual((realpath, repo, verdict), (str(scope / target_rel), NO_VALUE, "ok"))
        for staged in files_below(stage):
            for fragment in EXCLUDED_FRAGMENTS:
                self.assertNotIn(fragment, staged)
        self.assertEqual(excluded_dirs_below(stage), [])
        self.assertEqual(os.listdir(scope), [])

    def test_staged_units_keep_exec_bits_hash_to_the_manifest_and_copy_verbatim(self):
        sandbox = self.new_sandbox()
        stage = self.new_stage(sandbox)
        plugin_dirs = plugin_dirs_by_name()
        listed = run_convert("list", "--repo", REPO_ROOT, sandbox=sandbox)
        file_sources = {f"{kind}/{Path(relpath).name}": REPO_ROOT / relpath
                        for _plugin, kind, relpath in (line.split("|") for line in listed.stdout.splitlines())}

        result = self.plan(sandbox, stage=stage)

        self.assertEqual((result.returncode, result.stderr), (EXIT_OK, ""))
        rows = rows_of(result)
        executables = []
        for plugin, unit, stage_rel, *_rest in (row for row in rows if row[1] == "dir"):
            for relpath in files_below(stage / stage_rel):
                with self.subTest(plugin=plugin, relpath=relpath):
                    source = plugin_dirs[plugin] / relpath
                    staged = stage / stage_rel / relpath
                    self.assertEqual(staged.read_bytes(), source.read_bytes())
                    self.assertEqual(is_executable(staged), is_executable(source))
                    if is_executable(staged):
                        executables.append(f"{plugin}/{relpath}")
        self.assertIn("commit-guard/hooks/await_commit.sh", executables)
        self.assertIn("mempalace-docker/scripts/run-mempalace.sh", executables)
        commit_guard_hook = stage / PAYLOAD_NAMESPACE / "commit-guard" / "hooks" / "commit_guard_hook.py"
        self.assertEqual(is_executable(commit_guard_hook),
                         is_executable(REPO_ROOT / "plugin-commit-guard" / "hooks" / "commit_guard_hook.py"))
        for row in (row for row in rows if row[1] == "file"):
            with self.subTest(row[3]):
                self.assertEqual((stage / row[2]).read_bytes(), file_sources[row[3]].read_bytes())

        hashed = run_convert("hash", *(stage / row[2] for row in rows), sandbox=sandbox)

        self.assertEqual((hashed.returncode, hashed.stderr), (EXIT_OK, ""))
        self.assertEqual(hashed.stdout.splitlines(), [f"{row[4]}\t{stage / row[2]}" for row in rows])

    def test_plugin_filter_stages_exactly_that_plugins_units(self):
        sandbox = self.new_sandbox()
        stage = self.new_stage(sandbox)

        result = self.plan(sandbox, "--plugin", "commit-guard", stage=stage)

        self.assertEqual((result.returncode, result.stderr), (EXIT_OK, ""))
        self.assertEqual(units_of(result), COMMIT_GUARD_UNITS)
        self.assertEqual(sorted(os.listdir(stage)), ["commands", PAYLOAD_NAMESPACE, "plugins"])
        self.assertEqual(os.listdir(stage / PAYLOAD_NAMESPACE), ["commit-guard"])

    def test_usage_errors_exit_2_and_stage_nothing(self):
        cases = {
            "an unknown plugin id": (("--plugin", "nope"), ("--scope", "global"), "unknown plugin id 'nope'"),
            "a dir-derived plugin name": (("--plugin", "attribution"), ("--scope", "global"),
                                          "unknown plugin id 'attribution'"),
            "an excluded plugin": (("--plugin", "opencode-migrate"), ("--scope", "global"),
                                   "unknown plugin id 'opencode-migrate'"),
            "a known id next to an unknown one": (("--plugin", "commit-guard", "--plugin", "nope"),
                                                  ("--scope", "global"), "unknown plugin id 'nope'"),
            "--scope project without --project-dir": ((), ("--scope", "project"),
                                                      "--scope project needs --project-dir"),
            "--project-dir with --scope global": ((), ("--scope", "global", "--project-dir", "/srv/p"),
                                                  "--project-dir is only for --scope project"),
        }
        for name, (args, scope_args, message) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                stage = self.new_stage(sandbox)

                result = self.plan(sandbox, *args, stage=stage, scope_args=scope_args)

                self.assertEqual((result.returncode, result.stdout), (EXIT_USAGE, ""))
                self.assertIn("usage: convert.py plan", result.stderr)
                self.assertIn(f"error: {message}", result.stderr)
                self.assertEqual(os.listdir(stage), [])


class PayloadStagingTests(PlanTestCase):
    """Payload excludes and rejections, on a fixture repo under the sandbox."""

    def fixture(self, sandbox):
        return make_fixture_repo(sandbox, ("plugin-commit-guard", "plugin-mempalace-docker"),
                                 ("commit-guard", "mempalace-docker"))

    # AC-020 (unit level)
    def test_payload_units_leave_out_pycache_pyc_evals_and_the_entry_excludes(self):
        sandbox = self.new_sandbox()
        repo = self.fixture(sandbox)
        hooks = repo / "plugin-commit-guard" / "hooks"
        write(hooks / "__pycache__" / "commit_guard_hook.cpython-310.pyc", "cache")
        (hooks / "nested" / "__pycache__").mkdir(parents=True)
        (hooks / "nested" / "empty" / "evals").mkdir(parents=True)
        write(hooks / "stale.pyc", "cache")
        write(hooks / "evals" / "case.json", "{}")
        write(hooks / "nested" / "evals" / "deep.json", "{}")
        write(hooks / "nested" / "keep.txt", "kept\n")
        write(hooks / "evals.md", "a file named like the dir is kept\n")
        write(repo / "plugin-mempalace-docker" / "hooks" / "vendor-notes.txt", "kept: not below hooks/vendor/\n")
        stage = self.new_stage(sandbox)

        result = self.plan(sandbox, repo=repo, stage=stage)

        self.assertEqual((result.returncode, result.stderr), (EXIT_OK, ""))
        self.assertEqual(excluded_dirs_below(stage), [])
        self.assertTrue((stage / PAYLOAD_NAMESPACE / "commit-guard" / "hooks" / "nested" / "empty").is_dir())
        self.assertEqual(files_below(stage / PAYLOAD_NAMESPACE / "commit-guard"),
                         ["hooks/await_commit.sh", "hooks/commit_guard_hook.py", "hooks/evals.md", "hooks/hooks.json",
                          "hooks/nested/keep.txt"])
        mempalace = files_below(stage / PAYLOAD_NAMESPACE / "mempalace-docker")
        self.assertIn("hooks/vendor-notes.txt", mempalace)
        self.assertIn("scripts/mark_mined.py", mempalace)
        self.assertEqual([path for path in mempalace if path.startswith("hooks/vendor/")], [])
        self.assertTrue((repo / "plugin-mempalace-docker" / "hooks" / "vendor").is_dir())

    def test_a_payload_with_a_symlink_or_special_file_is_rejected_and_the_other_units_still_stage(self):
        cases = {
            "a symlink": lambda hooks: (hooks / "link").symlink_to("hooks.json"),
            "a symlinked dir": lambda hooks: (hooks / "linked-dir").symlink_to(hooks.parent / "commands"),
            "a fifo": lambda hooks: os.mkfifo(hooks / "pipe"),
        }
        for name, make_entry in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                repo = self.fixture(sandbox)
                hooks = repo / "plugin-commit-guard" / "hooks"
                make_entry(hooks)
                entry = sorted(set(os.listdir(hooks)) - {"await_commit.sh", "commit_guard_hook.py", "hooks.json"})[0]
                stage = self.new_stage(sandbox)

                result = self.plan(sandbox, repo=repo, stage=stage)

                self.assertEqual(result.returncode, EXIT_REJECTED, result.stderr)
                self.assertEqual(result.stderr.splitlines(), [
                    f"ERROR plugin-commit-guard/hooks/{entry}: payload-entry: a payload holds regular files and dirs "
                    "only, not a symlink or special file",
                    "ERROR plugin-commit-guard/plugins/opencode-commit-guard.ts: payload-rejected: the payload unit "
                    "of commit-guard was rejected",
                ])
                self.assertEqual(units_of(result), [
                    ("mempalace-docker", "dir", "llm-agent-workflow/mempalace-docker"),
                    ("commit-guard", "file", "commands/commit-guard.md"),
                ])
                self.assertEqual(os.listdir(stage / PAYLOAD_NAMESPACE), ["mempalace-docker"])
                self.assertFalse((stage / "plugins").exists())

    def test_a_rejected_payload_of_a_plugin_without_a_ts_still_exits_3(self):
        sandbox = self.new_sandbox()
        repo = self.fixture(sandbox)
        (repo / "plugin-mempalace-docker" / "scripts" / "link").symlink_to("mark_mined.py")
        stage = self.new_stage(sandbox)

        result = self.plan(sandbox, repo=repo, stage=stage)

        self.assertEqual(result.returncode, EXIT_REJECTED, result.stderr)
        self.assertEqual(result.stderr.splitlines(), [
            "ERROR plugin-mempalace-docker/scripts/link: payload-entry: a payload holds regular files and dirs only, "
            "not a symlink or special file",
        ])
        self.assertEqual(units_of(result), COMMIT_GUARD_UNITS)
        self.assertEqual(os.listdir(stage / PAYLOAD_NAMESPACE), ["commit-guard"])

    def test_two_units_with_one_target_are_fatal_before_anything_is_staged(self):
        sandbox = self.new_sandbox()
        repo = self.fixture(sandbox)
        twin = repo / "plugin-commit-guard-twin"
        shutil.copytree(repo / "plugin-commit-guard", twin)
        manifest = twin / ".claude-plugin" / "plugin.json"
        manifest.write_text(json.dumps(dict(json.loads(manifest.read_text()), name="commit-guard-twin")))
        stage = self.new_stage(sandbox)

        result = self.plan(sandbox, repo=repo, stage=stage)

        self.assert_fatal(result, "ERROR plugin-commit-guard-twin/plugins/opencode-commit-guard.ts: duplicate-target: "
                                  "plugins/opencode-commit-guard.ts is also the target of "
                                  "plugin-commit-guard/plugins/opencode-commit-guard.ts")
        self.assertEqual(os.listdir(stage), [])


class PlanFatalTests(PlanTestCase):
    """Where `plan` may stage, and what it refuses to print."""

    def test_a_stage_that_is_missing_not_empty_or_inside_the_repo_or_the_scope_is_fatal(self):
        # Each case: (sandbox, fixture repo, scope) -> (stage, the --repo, the message).
        def inside_symlinked_scope_dir(sandbox, repo, scope):
            (scope / "plugins").symlink_to(empty_dir(sandbox.root / "elsewhere"))
            return empty_dir(sandbox.root / "elsewhere" / "stage"), REPO_ROOT, "inside the scope"

        def through_a_link_to_the_scope(sandbox, repo, scope):
            empty_dir(scope / "stage")
            return link_to(sandbox.root / "scope-link", scope) / "stage", REPO_ROOT, "inside the scope"

        cases = {
            "a missing stage": lambda sb, repo, scope: (sb.root / "missing", REPO_ROOT, "not a directory"),
            "a stage that is a file": lambda sb, repo, scope: (write(sb.root / "file"), REPO_ROOT, "not a directory"),
            "a stage that is not empty": lambda sb, repo, scope: (write(sb.root / "full" / "x").parent, REPO_ROOT,
                                                                  "not empty"),
            "a stage inside the repo": lambda sb, repo, scope: (empty_dir(repo / "stage"), repo, "inside the repo"),
            "a stage inside the scope root": lambda sb, repo, scope: (empty_dir(scope / "stage"), REPO_ROOT,
                                                                      "inside the scope"),
            "a stage inside a symlinked scope dir": inside_symlinked_scope_dir,
            "a stage reached through a symlink to the scope": through_a_link_to_the_scope,
        }
        for name, build in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                repo = make_fixture_repo(sandbox, ("plugin-commit-guard",), ("commit-guard",))
                scope = self.scope_of(sandbox)
                stage, plan_repo, message = build(sandbox, repo, scope)
                before = snapshot(sandbox.root)

                result = self.plan(sandbox, "--plugin", "commit-guard", repo=plan_repo, scope=scope, stage=stage)

                self.assert_fatal(result, f"ERROR {stage}: plan-stage: {message}")
                self.assertEqual(snapshot(sandbox.root), before)

    def test_a_failed_copy_into_the_stage_is_fatal(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores the read-only stage mode")
        sandbox = self.new_sandbox()
        stage = self.new_stage(sandbox)
        stage.chmod(0o555)
        self.addCleanup(stage.chmod, 0o755)

        result = self.plan(sandbox, "--plugin", "commit-guard", stage=stage)

        self.assert_fatal(result, f"ERROR {stage}: stage-failed: Permission denied")

    def test_a_control_character_in_an_output_column_is_fatal(self):
        sandbox = self.new_sandbox()
        scope = link_scope(sandbox.home / ".config" / "opencode", plugins=sandbox.root / "bad\ndir")

        result = self.plan(sandbox, "--plugin", "commit-guard", scope=scope)

        self.assertEqual(result.returncode, EXIT_FATAL, result.stderr)
        self.assertEqual(result.stderr,
                         'ERROR plan: plan-column: the "realpath" column of a unit has a control character\n')
        self.assertEqual(units_of(result), COMMIT_GUARD_UNITS[:1])

    def test_a_control_character_in_the_plugin_column_is_fatal(self):
        sandbox = self.new_sandbox()
        repo = make_fixture_repo(sandbox, ("plugin-gh-issue-to-pr",), ())
        manifest = repo / "plugin-gh-issue-to-pr" / ".claude-plugin" / "plugin.json"
        manifest.write_text(json.dumps(dict(json.loads(manifest.read_text()), name="gh\u2028x")))

        result = self.plan(sandbox, repo=repo)

        self.assertEqual((result.returncode, result.stdout), (EXIT_FATAL, ""), result.stderr)
        self.assertEqual(result.stderr,
                         'ERROR plan: plan-column: the "plugin" column of a unit has a control character\n')


class PlanVerdictTests(PlanTestCase):
    """`plan` carries the guard verdict for each target; --allow-repo answers G3."""

    # AC-048 (submodule bullet, pushed down from the E2E)
    def test_ts_rows_need_approval_in_a_dotfiles_submodule_until_the_repo_is_allowed(self):
        sandbox = self.new_sandbox()
        superproject, dotfiles = sandbox.make_dotfiles_submodule()
        plugins_dir = dotfiles / ".config" / "opencode" / "plugins"
        scope = link_scope(sandbox.home / ".config" / "opencode", plugins=plugins_dir)
        before = snapshot(superproject)
        ts_realpath = str(plugins_dir / "opencode-commit-guard.ts")

        runs = {
            "without approval": ((), f"approve:{dotfiles}"),
            "with --allow-repo": (("--allow-repo", dotfiles), "ok"),
        }
        for name, (allow, ts_verdict) in runs.items():
            with self.subTest(name):
                stage = empty_dir(sandbox.root / f"stage-{len(allow)}")

                result = self.plan(sandbox, "--plugin", "commit-guard", *allow, scope=scope, stage=stage)

                self.assertEqual((result.returncode, result.stderr), (EXIT_OK, ""))
                verdicts = {row[3]: (row[5], row[6], row[7]) for row in rows_of(result)}
                self.assertEqual(verdicts, {
                    "llm-agent-workflow/commit-guard": (str(scope / "llm-agent-workflow" / "commit-guard"), NO_VALUE,
                                                        "ok"),
                    "plugins/opencode-commit-guard.ts": (ts_realpath, str(dotfiles), ts_verdict),
                    "commands/commit-guard.md": (str(scope / "commands" / "commit-guard.md"), NO_VALUE, "ok"),
                })
                self.assertEqual(snapshot(superproject), before)


class PluginNameTests(PlanTestCase):
    """`list --plugin NAME`: a plugin.json id as is, a dir-derived name with one WARN, anything else exit 2."""

    def list_rows(self, sandbox, *args, repo=REPO_ROOT):
        return run_convert("list", "--repo", repo, *args, sandbox=sandbox)

    def rows_of_ids(self, sandbox, ids, repo=REPO_ROOT):
        """The unfiltered `list` rows of IDS, in list order."""
        return [line for line in self.list_rows(sandbox, repo=repo).stdout.splitlines() if line.split("|")[0] in ids]

    # AC-055 (alias half, pushed down from the E2E)
    def test_an_id_passes_silently_and_a_dir_name_resolves_to_its_id_with_one_warn(self):
        # Task 3.1 renames claude-attribution to ai-attribution; the dir stays plugin-attribution.
        cases = {
            "an id": (("commit-guard",), {"commit-guard"}, ""),
            "two ids": (("token-saver", "commit-guard"), {"commit-guard", "token-saver"}, ""),
            "a dir-derived name": (("attribution",), {"claude-attribution"}, "WARN: use claude-attribution\n"),
            "that name twice and its id": (("attribution", "attribution", "claude-attribution"),
                                           {"claude-attribution"}, "WARN: use claude-attribution\n"),
        }
        for name, (names, ids, stderr) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()

                result = self.list_rows(sandbox, *(f"--plugin={plugin}" for plugin in names))

                self.assertEqual((result.returncode, result.stderr), (EXIT_OK, stderr))
                self.assertEqual(result.stdout.splitlines(), self.rows_of_ids(sandbox, ids))

    def test_the_alias_table_comes_from_the_plugin_dirs_and_an_id_wins_over_a_dir_name(self):
        sandbox = self.new_sandbox()
        repo = make_fixture_repo(sandbox, ("plugin-commit-guard", "plugin-gh-issue-to-pr"), ())
        for plugin_dir, plugin_id in (("plugin-commit-guard", "gh-issue-to-pr"), ("plugin-gh-issue-to-pr", "gh")):
            manifest = repo / plugin_dir / ".claude-plugin" / "plugin.json"
            manifest.write_text(json.dumps(dict(json.loads(manifest.read_text()), name=plugin_id)))
        cases = {
            "an id that is also another plugin's dir name": ("gh-issue-to-pr", "gh-issue-to-pr", ""),
            "a dir name whose id differs": ("commit-guard", "gh-issue-to-pr", "WARN: use gh-issue-to-pr\n"),
            "the other id": ("gh", "gh", ""),
        }
        for name, (plugin, plugin_id, stderr) in cases.items():
            with self.subTest(name):
                result = self.list_rows(sandbox, "--plugin", plugin, repo=repo)

                self.assertEqual((result.returncode, result.stderr), (EXIT_OK, stderr))
                rows = result.stdout.splitlines()
                self.assertEqual(rows, self.rows_of_ids(sandbox, {plugin_id}, repo=repo))
                self.assertTrue(rows)

    def test_a_name_that_names_no_listed_plugin_exits_2_naming_the_plugin_ids(self):
        # Task 3.1 renames claude-attribution to ai-attribution.
        real_ids = ("claude-attribution, commit-guard, dev, env-guard, gh-issue-to-pr, markdown-format, markdown-lsp, "
                    "memory-guard, mempalace-docker, qa, ruby-lsp, token-saver, wandavision")

        def migrate_dir(sandbox):
            repo = make_fixture_repo(sandbox, ("plugin-commit-guard",), ())
            shutil.copytree(REPO_ROOT / "plugin-opencode-migrate", repo / "plugin-migrate")
            return repo

        cases = {
            "an unknown name": (("nope",), None, "'nope'", real_ids),
            "an excluded plugin's id": (("opencode-migrate",), None, "'opencode-migrate'", real_ids),
            "a full plugin dir name": (("plugin-commit-guard",), None, "'plugin-commit-guard'", real_ids),
            "a dir name next to an unknown name": (("attribution", "nope"), None, "'nope'", real_ids),
            "the dir name of an excluded plugin": (("migrate",), migrate_dir, "'migrate'", "commit-guard"),
        }
        for name, (names, build, shown, ids) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                repo = REPO_ROOT if build is None else build(sandbox)

                result = self.list_rows(sandbox, *(f"--plugin={plugin}" for plugin in names), repo=repo)

                self.assertEqual((result.returncode, result.stdout), (EXIT_USAGE, ""))
                self.assertEqual(result.stderr, f"ERROR --plugin {shown}: plugin-unknown: names no OpenCode-compatible "
                                                f"plugin; plugin ids: {ids}\n")


class HashTests(PlanTestCase):
    """`hash`: the file hash or the tree hash per PATH, with the tree hash defined entry by entry."""

    def make_tree(self, root):
        write(root / "a.sh", "echo a\n").chmod(0o755)
        write(root / "sub" / "b.txt", "b\n").chmod(0o644)
        return root

    def expected_tree_hash(self):
        entries = [
            b"a.sh\x001\x00" + sha256_of(b"echo a\n").encode() + b"\n",
            b"sub/b.txt\x000\x00" + sha256_of(b"b\n").encode() + b"\n",
        ]
        return sha256_of(b"".join(entries))

    def test_hash_prints_the_file_hash_or_the_tree_hash_per_path(self):
        sandbox = self.new_sandbox()
        tree = self.make_tree(sandbox.fixtures / "tree")
        file_path = write(sandbox.fixtures / "file.md", "# file\n")

        result = run_convert("hash", file_path, tree, sandbox=sandbox)

        self.assertEqual((result.returncode, result.stderr), (EXIT_OK, ""))
        self.assertEqual(result.stdout, f"sha256:{sha256_of(b'# file' + bytes([10]))}\t{file_path}\n"
                                        f"sha256:{self.expected_tree_hash()}\t{tree}\n")

    def test_the_tree_hash_covers_names_exec_bits_content_and_symlinks_but_not_empty_dirs(self):
        units = import_units()
        sandbox = self.new_sandbox()
        baseline = units.tree_sha256(self.make_tree(sandbox.fixtures / "baseline"))
        write(sandbox.fixtures / "other" / "b.txt", "b\n")
        changes = {
            "an exec bit": lambda tree: (tree / "sub" / "b.txt").chmod(0o755),
            "a name": lambda tree: (tree / "sub" / "b.txt").rename(tree / "sub" / "c.txt"),
            "content": lambda tree: (tree / "sub" / "b.txt").write_text("B\n"),
            "a symlink to identical content": lambda tree: (
                (tree / "sub" / "b.txt").unlink(), (tree / "sub" / "b.txt").symlink_to(sandbox.fixtures / "other" /
                                                                                     "b.txt")),
            "a symlinked dir with identical content": lambda tree: (
                shutil.rmtree(tree / "sub"), (tree / "sub").symlink_to(sandbox.fixtures / "other")),
            "a fifo": lambda tree: os.mkfifo(tree / "pipe"),
        }

        self.assertEqual(baseline, self.expected_tree_hash())
        for name, change in changes.items():
            with self.subTest(name):
                tree = self.make_tree(sandbox.fixtures / name.replace(" ", "-"))
                change(tree)
                self.assertNotEqual(units.tree_sha256(tree), baseline)
        with self.subTest("a symlinked dir is an entry of its own"):
            without = self.make_tree(sandbox.fixtures / "without-sub")
            shutil.rmtree(without / "sub")
            linked = self.make_tree(sandbox.fixtures / "linked-sub")
            shutil.rmtree(linked / "sub")
            (linked / "sub").symlink_to(sandbox.fixtures / "other")
            self.assertNotEqual(units.tree_sha256(linked), units.tree_sha256(without))
        with self.subTest("an empty dir"):
            tree = self.make_tree(sandbox.fixtures / "with-empty-dir")
            (tree / "empty").mkdir()
            self.assertEqual(units.tree_sha256(tree), baseline)

    def test_hash_of_a_missing_path_or_a_special_file_exits_1_and_prints_nothing(self):
        sandbox = self.new_sandbox()
        good = write(sandbox.fixtures / "good.md", "good\n")
        fifo = sandbox.fixtures / "pipe"
        os.mkfifo(fifo)
        cases = {
            "a missing path": ((sandbox.fixtures / "missing",), "no such file or directory"),
            "a fifo (never opened)": ((fifo,), "not a file or directory"),
            "a good path, then a missing one": ((good, sandbox.fixtures / "missing"), "no such file or directory"),
        }
        for name, (paths, problem) in cases.items():
            with self.subTest(name):
                result = run_convert("hash", *paths, sandbox=sandbox)

                self.assert_fatal(result, f"ERROR {paths[-1]}: hash: {problem}")


if __name__ == "__main__":
    unittest.main()
