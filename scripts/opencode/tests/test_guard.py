#!/usr/bin/env python3
# Repository guard Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.4)
# Generated: 2026-09-24 | Budget Used: 3/3 integration, 0/2 E2E (F8 E2E lives in test_setup_opencode.py)
"""Integration tests for `convert.py guard` on real git repos.

Each test runs the real CLI in a subprocess:

  python3 scripts/opencode/convert.py guard --scope-root DIR --op write|delete [--allow-repo DIR]... PATH...

stdout contract: "path<TAB>realpath<TAB>repo<TAB>origin<TAB>verdict" per PATH; exit 0 when all are ok, 4 when any
is blocked or needs approval, 1 on a fatal error. This is the level AC-048 and AC-049 are pushed down to (IP-19).

  python3 -m unittest discover -s scripts/opencode/tests
"""

import os
import shutil
import sys
import unittest
from unittest import mock

from _support import (DOTFILES_ORIGIN, REPO_ROOT, SKILLS_MD_ORIGIN, START_ORIGIN, TOOL_NAMES, Sandbox, link_scope,
                      run_convert, snapshot)

# Harness: real git only (Mock Boundary "git repo queries (guard): No"). Fresh fixtures per test (or per subTest)
# from _support.Sandbox, one temp root each:
# - skills-md: `git init`, origin https://example.invalid/jcchikikomori/skills-md.git, with a nested submodule
#   "start" (origin .../skills-md-dev-orchestrator.git) added via `git -c protocol.file.allow=always submodule add`
# - dotfiles: a repo added as a submodule of a temp superproject; the scope's plugins/ symlinks into it
# - plain: a repo that is not a submodule; outside: a dir in no repo
# HOME points at the tempdir, so no real ~/.gitconfig is read (GIT_CONFIG_NOSYSTEM=1 too).
# G0 runs first, so every target that must reach G1 enters the fixture repo through a scope dir symlink.

EXIT_OK = 0
EXIT_BLOCKED = 4
NO_VALUE = "-"
DOTFILES_CLEAN_ORIGIN = "https://example.invalid/dotfiles.git"
PLAIN_ORIGIN = "https://reader@example.invalid/jcchikikomori/plain.git"
PLAIN_CLEAN_ORIGIN = "https://example.invalid/jcchikikomori/plain.git"
CREDENTIAL_FRAGMENTS = ("user:token@", "reader@")
MISSING_GITDIR = "/nonexistent/modules/x"


def import_guard():
    """Import scripts/opencode/guard.py without writing __pycache__/ into the repo."""
    scripts_dir = str(REPO_ROOT / "scripts" / "opencode")
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, scripts_dir)
    try:
        import guard
    finally:
        sys.path.remove(scripts_dir)
        sys.dont_write_bytecode = previous
    return guard


def scope_root_of(sandbox):
    return sandbox.home / ".config" / "opencode"


def rows_of(result):
    return [line.split("\t") for line in result.stdout.splitlines()]


def fixture_snapshots(sandbox):
    """Snapshot every tree a guard run could touch (the tools/ links into the host are left out)."""
    roots = [sandbox.home, sandbox.fixtures, sandbox.root / "elsewhere"]
    return {str(root): snapshot(root) for root in roots if root.exists()}


class GuardTests(unittest.TestCase):
    def new_sandbox(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        return sandbox

    def guard(self, sandbox, op, *paths, allow=(), env=None):
        allow_args = [arg for repo in allow for arg in ("--allow-repo", repo)]
        return run_convert("guard", "--scope-root", scope_root_of(sandbox), "--op", op, *allow_args, *paths,
                           sandbox=sandbox, env=env)

    def build_skills_md_world(self):
        """skills-md with start; <scope>/commands and <scope>/agents route into it; skills/ is a plain dir."""
        sandbox = self.new_sandbox()
        skills_md = sandbox.make_skills_md()
        scope = link_scope(scope_root_of(sandbox), commands=skills_md / "commands",
                           agents=skills_md / "start" / "agents")
        (scope / "skills" / "alpha").mkdir(parents=True)
        (sandbox.root / "elsewhere").mkdir()
        return sandbox, skills_md, scope

    # Supports AC-048 (D12)
    # AC-048 (part): "If a target realpath lies in a checkout whose origin repo name is skills-md, or in a submodule
    #   of one, then [BLOCKED], nothing written, exit 1. ... Nothing shall ever be written under
    #   realpath(<scope>/skills)."
    # F8: G0 containment, G1 skills-md origin (nearest repo or any superproject), G2 native skills dir.
    # Given (one subTest each, fresh fixtures):
    #   - a new file in the skills-md checkout (the parent dir is resolved)
    #   - a file in its nested start submodule
    #   - a file under <scope>/skills, where skills/ is a plain dir in no repo
    #   - a path outside the scope realpaths (for example <tmp>/elsewhere/x.md)
    #   - the skills-md checkout target again, with GIT_DIR (with and without GIT_WORK_TREE) in the env pointing at a
    #     plain repo, so a guard that let git read them would ask the wrong repo
    # When: guard --op write, then --op delete, for each PATH; also once with --allow-repo <skills-md toplevel>.
    # Then: each target is refused by the expected rule.
    # Verification items:
    #   - verdicts are blocked:G1, blocked:G1 (via the superproject origin), blocked:G2 and blocked:G0
    #   - --op delete gives the same verdicts
    #   - --allow-repo does not turn a G1 verdict into ok
    #   - GIT_DIR / GIT_WORK_TREE are ignored: still blocked:G1 with the skills-md repo and origin columns
    #   - exit 4 whenever any verdict is blocked; the guard itself writes nothing (fixture snapshots are equal)
    # Pass criteria: every verdict column matches, exit 4 each time, and the fixtures are unchanged.
    # ROI: 90 (BV:10 x Freq:8 + Legal:0 + Defect:10) | biggest-risk area: wrong-repo writes
    # @category: core-functionality
    # @dependency: convert.py guard, guard.py, git
    # @real-dependency: git (origin, superproject), filesystem (symlinks)
    # @complexity: high
    def test_skills_md_checkouts_native_skills_dir_and_outside_targets_are_blocked(self):
        # Each case: (target under the scope or outside it, expected realpath, repo, origin, verdict).
        cases = {
            "new file in the skills-md checkout": lambda sb, md, scope: (
                scope / "commands" / "new.md", md / "commands" / "new.md", str(md), SKILLS_MD_ORIGIN, "blocked:G1"),
            "file in the nested start submodule": lambda sb, md, scope: (
                scope / "agents" / "orchestrator.md", md / "start" / "agents" / "orchestrator.md",
                str(md / "start"), START_ORIGIN, "blocked:G1"),
            "file under the native skills dir": lambda sb, md, scope: (
                scope / "skills" / "alpha" / "SKILL.md", scope / "skills" / "alpha" / "SKILL.md",
                NO_VALUE, NO_VALUE, "blocked:G2"),
            "path outside the scope realpaths": lambda sb, md, scope: (
                sb.root / "elsewhere" / "x.md", sb.root / "elsewhere" / "x.md", NO_VALUE, NO_VALUE, "blocked:G0"),
        }
        for name, build in cases.items():
            with self.subTest(name):
                sandbox, skills_md, scope = self.build_skills_md_world()
                target, realpath, repo, origin, verdict = build(sandbox, skills_md, scope)
                before = fixture_snapshots(sandbox)

                runs = [
                    ("write", ()),
                    ("delete", ()),
                    ("write", (skills_md, skills_md / "start")),
                ]
                for op, allow in runs:
                    result = self.guard(sandbox, op, target, allow=allow)

                    self.assertEqual(result.returncode, EXIT_BLOCKED, f"{op} {allow}: {result.stderr}")
                    self.assertEqual(rows_of(result), [[str(target), str(realpath), repo, origin, verdict]],
                                     f"{op} allow={[str(path) for path in allow]}")
                self.assertEqual(fixture_snapshots(sandbox), before)

        # The caller's GIT_DIR (and GIT_WORK_TREE) point at a plain repo; the guard must still ask the target's repo.
        env_variants = {
            "GIT_DIR and GIT_WORK_TREE point at a plain repo": ("GIT_DIR", "GIT_WORK_TREE"),
            "GIT_DIR alone points at a plain repo": ("GIT_DIR",),
        }
        for name, keys in env_variants.items():
            with self.subTest(name):
                sandbox, skills_md, scope = self.build_skills_md_world()
                plain = sandbox.make_repo(sandbox.fixtures / "plain", origin=PLAIN_ORIGIN)
                location = {"GIT_DIR": plain / ".git", "GIT_WORK_TREE": plain}
                env = sandbox.env(**{key: location[key] for key in keys})
                target = scope / "commands" / "new.md"
                before = fixture_snapshots(sandbox)

                result = self.guard(sandbox, "write", target, env=env)

                self.assertEqual(result.returncode, EXIT_BLOCKED, result.stderr)
                self.assertEqual(rows_of(result), [[str(target), str(skills_md / "commands" / "new.md"),
                                                     str(skills_md), SKILLS_MD_ORIGIN, "blocked:G1"]])
                self.assertEqual(fixture_snapshots(sandbox), before)

    # Supports AC-048 (D12, submodule bullet) and AC-049 (D12)
    # AC-048 (part): "If it lies in another submodule, non-TTY, without approval, then [BLOCKED] with the
    #   --allow-repo hint. With --allow-repo it is written"
    # AC-049 (part): "Each unit whose realpath is in a git repo prints (repo: <toplevel> <origin>)."
    # Given: the scope's plugins/ symlinked into the dotfiles submodule, whose origin is
    #   https://user:token@example.invalid/dotfiles.git; a target in the plain repo; a target in no repo.
    # When: guard --op write <scope>/plugins/new.ts (a new file), with and without --allow-repo <dotfiles toplevel>;
    #   guard for the plain-repo and no-repo targets.
    # Then: submodules need approval, plain repos and non-repo dirs pass, and every row names its repo.
    # Verification items:
    #   - without --allow-repo: verdict approve:<dotfiles toplevel>, exit 4
    #   - with --allow-repo: verdict ok, exit 0
    #   - the realpath column is the path inside the dotfiles repo (the symlink is resolved), and the repo column is
    #     its toplevel
    #   - the origin column is https://example.invalid/dotfiles.git: the user:token@ part is stripped
    #   - plain repo: ok with the repo and origin filled in; no repo: ok with repo "-"
    # Pass criteria: all verdict, realpath, repo and origin columns match, with the expected exit codes.
    # ROI: 90 (BV:10 x Freq:8 + Legal:0 + Defect:10)
    # @category: core-functionality
    # @dependency: convert.py guard, guard.py, git
    # @real-dependency: git (submodule, superproject, remote), filesystem (symlinks)
    # @complexity: high
    def test_submodule_targets_need_approval_and_plain_repos_pass(self):
        sandbox = self.new_sandbox()
        _superproject, dotfiles = sandbox.make_dotfiles_submodule(origin=DOTFILES_ORIGIN)
        plain = sandbox.make_repo(sandbox.fixtures / "plain", origin=PLAIN_ORIGIN)
        dotfiles_plugins = dotfiles / ".config" / "opencode" / "plugins"
        scope = link_scope(scope_root_of(sandbox), plugins=dotfiles_plugins, commands=plain / "commands")
        dotfiles_target = scope / "plugins" / "new.ts"
        plain_target = scope / "commands" / "new.md"
        no_repo_target = scope / "llm-agent-workflow" / "commit-guard" / "hooks" / "hook.py"
        dotfiles_row = [str(dotfiles_target), str(dotfiles_plugins / "new.ts"), str(dotfiles), DOTFILES_CLEAN_ORIGIN]

        without_approval = self.guard(sandbox, "write", dotfiles_target)
        with_approval = self.guard(sandbox, "write", dotfiles_target, allow=(dotfiles,))
        plain_and_no_repo = self.guard(sandbox, "write", plain_target, no_repo_target)

        self.assertEqual(without_approval.returncode, EXIT_BLOCKED, without_approval.stderr)
        self.assertEqual(rows_of(without_approval), [dotfiles_row + [f"approve:{dotfiles}"]])
        self.assertEqual(with_approval.returncode, EXIT_OK, with_approval.stderr)
        self.assertEqual(rows_of(with_approval), [dotfiles_row + ["ok"]])
        self.assertEqual(plain_and_no_repo.returncode, EXIT_OK, plain_and_no_repo.stderr)
        self.assertEqual(rows_of(plain_and_no_repo), [
            [str(plain_target), str(plain / "commands" / "new.md"), str(plain), PLAIN_CLEAN_ORIGIN, "ok"],
            [str(no_repo_target), str(no_repo_target), NO_VALUE, NO_VALUE, "ok"],
        ])
        for result in (without_approval, with_approval, plain_and_no_repo):
            for fragment in CREDENTIAL_FRAGMENTS:
                self.assertNotIn(fragment, result.stdout + result.stderr)

    # Supports AC-048 (D12); Design Doc F8: "Without git on PATH, G1 and G3 can't be evaluated, so every target
    #   inside a .git-bearing ancestor is BLOCKED (fail closed)."
    # Given: PATH holding python3 only (an isolated tools dir, as in plugin-markdown-lsp/tests/test_run_rumdl.py);
    #   one target inside the plain repo and one in a dir with no .git ancestor.
    #   Second subTest: git on the normal sandbox PATH, but the target's dir holds a .git FILE pointing at a missing
    #   gitdir, so git is present and still cannot answer.
    # When: guard --op write for both targets.
    # Then: the guard fails closed when it cannot ask git.
    # Verification items:
    #   - the target inside the repo is blocked, and the row says why (git unavailable)
    #   - one stderr line "WARN <realpath>: git-unavailable: <reason>" names the blocked target
    #   - the target outside any repo passes G0 and is ok
    #   - exit 4
    # Pass criteria: the verdicts match, and exit 4.
    # ROI: 27 (BV:9 x Freq:2 + Legal:0 + Defect:9) | fail-closed security rule
    # @category: edge-case
    # @dependency: convert.py guard, guard.py
    # @real-dependency: filesystem
    # @complexity: low
    def test_without_git_every_target_inside_a_git_ancestor_is_blocked(self):
        cases = {
            "git is not on PATH": self.plain_repo_with_python_only_path,
            "git is on PATH but the .git file points at a missing gitdir": self.broken_gitfile_with_sandbox_path,
        }
        for name, build in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                repo_dir, env = build(sandbox)
                scope = link_scope(scope_root_of(sandbox), commands=repo_dir / "commands")
                in_repo_target = scope / "commands" / "new.md"
                in_repo_realpath = repo_dir / "commands" / "new.md"
                no_repo_target = scope / "llm-agent-workflow" / "commit-guard" / "hooks" / "hook.py"

                result = self.guard(sandbox, "write", in_repo_target, no_repo_target, env=env)

                self.assertEqual(result.returncode, EXIT_BLOCKED, result.stderr)
                self.assertEqual(rows_of(result), [
                    [str(in_repo_target), str(in_repo_realpath), NO_VALUE, NO_VALUE, "blocked:git-unavailable"],
                    [str(no_repo_target), str(no_repo_target), NO_VALUE, NO_VALUE, "ok"],
                ])
                warnings = [line for line in result.stderr.splitlines() if line.startswith("WARN ")]
                self.assertEqual(len(warnings), 1, result.stderr)
                self.assertTrue(warnings[0].startswith(f"WARN {in_repo_realpath}: git-unavailable: "), warnings[0])

    def plain_repo_with_python_only_path(self, sandbox):
        plain = sandbox.make_repo(sandbox.fixtures / "plain", origin=PLAIN_ORIGIN)
        python_only_path = sandbox.tools_without(*(name for name in TOOL_NAMES if name != "python3"))
        self.assertIsNone(shutil.which("git", path=python_only_path))
        self.assertIsNotNone(shutil.which("python3", path=python_only_path))
        return plain, sandbox.env(PATH=python_only_path)

    def broken_gitfile_with_sandbox_path(self, sandbox):
        broken = sandbox.fixtures / "broken"
        broken.mkdir()
        (broken / ".git").write_text(f"gitdir: {MISSING_GITDIR}\n")
        env = sandbox.env()
        self.assertIsNotNone(shutil.which("git", path=env["PATH"]))
        self.assertFalse(os.path.lexists(MISSING_GITDIR))
        return broken, env


class GuardHelperTests(unittest.TestCase):
    """Unit cases for the public guard.py helpers that 1.2 and 1.3a import."""

    def setUp(self):
        self.guard = import_guard()

    def test_strip_credentials_removes_every_userinfo_form(self):
        cases = {
            "https://user:token@example.invalid/dotfiles.git": "https://example.invalid/dotfiles.git",
            "https://reader@example.invalid/plain.git": "https://example.invalid/plain.git",
            "https://user:p@ss@example.invalid/x.git": "https://example.invalid/x.git",
            "ssh://git@example.invalid:22/o/r.git": "ssh://example.invalid:22/o/r.git",
            "git@github.com:jcchikikomori/skills-md.git": "github.com:jcchikikomori/skills-md.git",
            "https://example.invalid/o/r.git": "https://example.invalid/o/r.git",
            "/srv/git/r.git": "/srv/git/r.git",
        }
        for url, expected in cases.items():
            with self.subTest(url):
                self.assertEqual(self.guard.strip_credentials(url), expected)

    def test_origin_repo_name_is_the_last_segment_without_dot_git(self):
        cases = {
            "https://example.invalid/jcchikikomori/skills-md.git": "skills-md",
            "https://example.invalid/jcchikikomori/skills-md/": "skills-md",
            "github.com:jcchikikomori/skills-md.git": "skills-md",
            "file:///srv/git/skills-md": "skills-md",
            "https://example.invalid/jcchikikomori/skills-md-dev-orchestrator.git": "skills-md-dev-orchestrator",
        }
        for url, expected in cases.items():
            with self.subTest(url):
                self.assertEqual(self.guard.origin_repo_name(url), expected)

    def test_g1_compares_origin_repo_names_case_insensitively(self):
        # (origin of the checkout, blocked_origin_repo_names): each side's casefold is needed for one case.
        cases = {
            "mixed-case origin, lowercase blocked name": (
                "https://example.invalid/jcchikikomori/Skills-MD.git", ("skills-md",)),
            "lowercase origin, mixed-case blocked name": (
                "https://example.invalid/jcchikikomori/skills-md.git", ("Skills-MD",)),
        }
        for name, (origin, blocked_names) in cases.items():
            with self.subTest(name):
                sandbox = Sandbox()
                self.addCleanup(sandbox.cleanup)
                checkout = sandbox.make_repo(sandbox.fixtures / "checkout", origin=origin)
                scope = link_scope(scope_root_of(sandbox), commands=checkout / "commands")
                env = sandbox.env()
                git = self.guard.GitLookup(program=shutil.which("git", path=env["PATH"]), environ=env)
                policy = self.guard.GuardPolicy(
                    scope_root=str(scope), blocked_origin_repo_names=blocked_names,
                    blocked_scope_dirs=("skills", "skill"),
                    writable_scope_dirs=("plugins", "agents", "commands", "llm-agent-workflow"))

                verdict = self.guard.Guard(policy, git=git).check(scope / "commands" / "new.md")

                self.assertEqual((verdict.repo, verdict.origin, verdict.verdict),
                                 (str(checkout), origin, "blocked:G1"))

    def test_a_git_query_failing_after_the_toplevel_answer_blocks_the_target(self):
        # Design Doc IP-9: repo queries fail closed. Real git and a real timeout, no stubbed answers: the toplevel
        # (and, for the submodule, origin) answers are cached first, then GIT_TIMEOUT_SECONDS drops below what any git
        # call can meet, so the next query fails the way a hung or unforkable git would.
        cases = {
            "origin query of a skills-md checkout": (self.skills_md_checkout, False,
                                                     "git remote get-url origin failed: "),
            "superproject query of a submodule": (self.dotfiles_submodule, True,
                                                  "git rev-parse --show-superproject-working-tree failed: "),
        }
        for name, (build, origin_answered_first, reason_prefix) in cases.items():
            with self.subTest(name):
                sandbox = Sandbox()
                self.addCleanup(sandbox.cleanup)
                scope, target, toplevel, origin_column = build(sandbox)
                env = sandbox.env()
                git = self.guard.GitLookup(program=shutil.which("git", path=env["PATH"]), environ=env)
                checker = self.guard.Guard(self.guard.GuardPolicy(
                    scope_root=str(scope), blocked_origin_repo_names=("skills-md",),
                    blocked_scope_dirs=("skills", "skill"),
                    writable_scope_dirs=("plugins", "agents", "commands", "llm-agent-workflow")), git=git)
                self.assertEqual(git.toplevel(self.guard.lookup_dir(self.guard.resolve_target(target))), str(toplevel))
                if origin_answered_first:
                    self.assertEqual(git.origin(str(toplevel)), origin_column)

                with mock.patch.object(self.guard, "GIT_TIMEOUT_SECONDS", 1e-6):
                    verdict = checker.check(target)

                self.assertEqual((verdict.repo, verdict.origin, verdict.verdict),
                                 (str(toplevel), origin_column, "blocked:git-unavailable"))
                self.assertTrue(verdict.reason.startswith(reason_prefix), verdict.reason)

    def skills_md_checkout(self, sandbox):
        checkout = sandbox.make_repo(sandbox.fixtures / "checkout", origin=SKILLS_MD_ORIGIN)
        scope = link_scope(scope_root_of(sandbox), commands=checkout / "commands")
        return scope, scope / "commands" / "new.md", checkout, NO_VALUE

    def dotfiles_submodule(self, sandbox):
        _superproject, dotfiles = sandbox.make_dotfiles_submodule()
        scope = link_scope(scope_root_of(sandbox), plugins=dotfiles / ".config" / "opencode" / "plugins")
        return scope, scope / "plugins" / "new.ts", dotfiles, DOTFILES_CLEAN_ORIGIN

    def test_is_within_matches_whole_path_components_only(self):
        self.assertTrue(self.guard.is_within("/scope/skills", "/scope/skills"))
        self.assertTrue(self.guard.is_within("/scope/skills/a/SKILL.md", "/scope/skills"))
        self.assertFalse(self.guard.is_within("/scope/skills-extra/a", "/scope/skills"))
        self.assertFalse(self.guard.is_within("/scope", "/scope/skills"))
        self.assertTrue(self.guard.is_within("/anything", "/"))

    def test_resolve_target_follows_a_symlink_before_applying_dot_dot(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        outside = sandbox.root / "outside" / "inner"
        scope = link_scope(sandbox.home / "scope", plugins=outside)

        resolved = self.guard.resolve_target(scope / "plugins" / ".." / "x.md")

        self.assertEqual(resolved, str(sandbox.root / "outside" / "x.md"))
        self.assertFalse(self.guard.is_contained(resolved, self.guard.containment_roots(scope, ["agents"])))

    def test_resolve_target_resolves_a_new_file_through_its_existing_parent(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        real_dir = sandbox.fixtures / "real"
        scope = link_scope(sandbox.home / "scope", commands=real_dir)

        resolved = self.guard.resolve_target(scope / "commands" / "new-dir" / "new.md")

        self.assertEqual(resolved, str(real_dir / "new-dir" / "new.md"))
        self.assertFalse(os.path.lexists(resolved))

    def test_has_git_ancestor_sees_a_git_dir_or_gitfile_above(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        (sandbox.fixtures / "repo" / ".git").mkdir(parents=True)
        (sandbox.fixtures / "repo" / "a" / "b").mkdir(parents=True)
        (sandbox.fixtures / "sub").mkdir()
        (sandbox.fixtures / "sub" / ".git").write_text("gitdir: ../repo/.git\n")

        self.assertTrue(self.guard.has_git_ancestor(str(sandbox.fixtures / "repo" / "a" / "b")))
        self.assertTrue(self.guard.has_git_ancestor(str(sandbox.fixtures / "sub")))
        self.assertFalse(self.guard.has_git_ancestor(str(sandbox.home)))


if __name__ == "__main__":
    unittest.main()
