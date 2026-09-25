#!/usr/bin/env python3
# opencode port installer E2E Test - Design Doc: docs/design/opencode-port-design.md (revision 1.4)
# Generated: 2026-09-24 | Budget Used: 0/3 integration, 12 E2E over 9 features (see FEATURE BUDGET below)
# Test Type: End-to-End Test
# Implementation Timing: each class is the L2 gate of its slice (Design Doc "Verification Strategy"); all green in
# the final QA phase
"""End-to-end tests for setup-opencode.sh (skeleton).

Each test runs the real installer in a subprocess:

  bash setup-opencode.sh <flags>

with HOME pointing at a tempdir, an isolated PATH, a stub skills-md converter,
and real git. A body stays a skipTest until its slice lands, so the suite
stays green; RepositoryGuardTests are real since Task 1.3a,
InstallerCoreJourneyTests since Task 1.3b and TrackerJourneyTests since Task
1.3c.

  python3 -m unittest discover -s scripts/opencode/tests
"""

import datetime
import hashlib
import json
import os
import unittest
from pathlib import Path

from _support import (SKILLS_MD_ORIGIN, START_ORIGIN, Sandbox, link_scope, run_convert, run_setup, snapshot,
                      write_stub_converter)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXIT_OK = 0
EXIT_FAILED = 1
TRACKER_NAME = ".opencode-setup-tracker"
DOTFILES_CLEAN_ORIGIN = "https://example.invalid/dotfiles.git"
CREDENTIALS = "user:token@"
PAYLOAD_NAMESPACE = "llm-agent-workflow"
# AC-001's ids. Task 3.1 renames claude-attribution to ai-attribution.
LISTED_IDS = {"claude-attribution", "commit-guard", "env-guard", "markdown-format", "memory-guard", "token-saver",
              "wandavision", "gh-issue-to-pr", "ruby-lsp", "markdown-lsp", "mempalace-docker", "dev", "qa"}
LIST_KINDS = {"plugins", "agents", "commands", "skills", "payload", "config"}
PAYLOAD_PLUGINS = {"commit-guard", "memory-guard", "markdown-format", "token-saver", "ruby-lsp", "markdown-lsp",
                   "mempalace-docker", "qa"}
WANDAVISION_TS = "wandavision/opencode-plugin/opencode-wandavision.ts"
EXCLUDED_NAMES = ("evals", "__pycache__")
WRITE_LABELS = ("[OK]", "[UPDATE]", "[OVERWRITE]")
NO_VALUE = "-"
USER_TEXT = "the user's own file\n"
OTHER_TEXT = "a different file with the same name\n"
COMMIT_GUARD_TS = "plugins/opencode-commit-guard.ts"
COMMIT_GUARD_COMMAND = "commands/commit-guard.md"
TOKEN_SAVER_TS = "plugins/opencode-token-saver.ts"
SOURCES = {
    COMMIT_GUARD_TS: REPO_ROOT / "plugin-commit-guard" / "plugins" / "opencode-commit-guard.ts",
    COMMIT_GUARD_COMMAND: REPO_ROOT / "plugin-commit-guard" / "commands" / "commit-guard.md",
    TOKEN_SAVER_TS: REPO_ROOT / "plugin-token-saver" / "plugins" / "opencode-token-saver.ts",
}
# The (type, plugin, path) of each unit TrackerJourneyTests installs: --plugin commit-guard --plugin token-saver (the
# token-saver plugin file is SAME-foreign, so it has no row), then --plugin markdown-lsp.
FIRST_RUN_KEYS = (("dir", "commit-guard", f"{PAYLOAD_NAMESPACE}/commit-guard"),
                  ("dir", "token-saver", f"{PAYLOAD_NAMESPACE}/token-saver"),
                  ("file", "commit-guard", COMMIT_GUARD_COMMAND),
                  ("file", "commit-guard", COMMIT_GUARD_TS))
MARKDOWN_LSP_KEY = ("dir", "markdown-lsp", f"{PAYLOAD_NAMESPACE}/markdown-lsp")
TRACKER_V2_MAGIC = "# setup-opencode.sh tracker v2"
TRACKER_HEADER_KEYS = ("installed_at", "repo_root", "scope", "scope_root", "payload_root", "recipe_policy",
                       "mcp_aliases", "allowed_repos", "skills_md", "columns")
TRACKER_COLUMNS = "type plugin path realpath hash repo"
V1_TRACKER_HEADER = ("# setup-opencode.sh tracker v1", "# installed_at: 2026-01-02T03:04:05Z",
                     "# repo_root: /srv/llm-agent-workflow", "# scope: global")
INSTALLED_AT = r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z"
BACKUP_STAMP = r"\A\d{8}T\d{6}Z\Z"
KEPT_WARNING = "tracker: realpath outside the scope (kept)"

# Harness (to implement; Design Doc "Test Strategy > Conventions" and "Mock Boundary Decisions"):
# - One sandbox per test: tempfile.TemporaryDirectory() holding home/, project/, bin/, fixtures/.
# - env: HOME=<tmp>/home; XDG_CONFIG_HOME, XDG_CACHE_HOME and XDG_DATA_HOME unset or under <tmp>;
#   PATH = <tmp>/bin plus the dirs holding bash, python3, git and coreutils (isolated, as in
#   plugin-markdown-lsp/tests/test_run_rumdl.py); STUB_LOG=<tmp>/calls.log; SKILLS_MD_DIR=<fixture> unless the
#   test says otherwise.
# - Stub skills-md converter (mocked: external repo, the contract is argv + exit code):
#   <fixture>/scripts/opencode-convert-skill.sh appends "SRC DEST" to STUB_LOG, copies SRC to DEST, and exits with
#   ${STUB_CONVERT_EXIT:-0}. The real converter runs only with SKILLS_MD_REAL=1.
# - Real git for every repo query, because the guard's correctness depends on it (Mock Boundary: No). Fixtures:
#   - "dotfiles": a repo added as a submodule of a temp superproject
#     (git -c protocol.file.allow=always submodule add); home/.config/opencode/{plugins,agents} symlink into it.
#   - "skills-md": a repo with origin .../skills-md.git and a nested submodule "start".
#   - clone and pull use local file:// remotes; no network.
# - stdin is never a TTY (subprocess.run(..., input="")), so every prompt takes its non-TTY branch.
# - snapshot(root) -> {realpath: sha256} over the scope realpaths, symlink targets included.
#
# FEATURE BUDGET (unit = one Design Doc component; E2E max 2 with a user-facing multi-step journey: 1 reserved slot
# + 1 slot needing ROI > 50; max 1 without a journey, which needs ROI > 50)
#   F2 installer core ...... 1/2  InstallerCoreJourneyTests (reserved)
#   F2 notices ............. 1/1  NoticeTests (ROI 56)
#   D9 legacy removal ...... 1/1  LegacyMigrationTests (ROI 64)
#   F4 tracker v2 .......... 2/2  TrackerJourneyTests (reserved + ROI 63); Task 1.3c adds the [KEPT] case as a
#                                 third method, as its task file allows
#   F5 snippets and merge .. 2/2  ConfigMergeTests (reserved + ROI 79)
#   F6 skills-md ........... 1/1  SkillsMdTests (ROI 80)
#   F7 recipe policy ....... 1/1  RecipePolicyTests (ROI 63)
#   F8 repository guard .... 2/2  RepositoryGuardTests (reserved + ROI 69)
#   F3 skill uniqueness .... 1/2  SkillUniquenessJourneyTests (reserved)
# State-rule details are pushed down to the convert.py-level integration suites: test_tracker.py, test_merge.py,
# test_guard.py and test_convert_skills.py.


def plugin_json_names():
    return {json.loads(path.read_text())["name"] for path in REPO_ROOT.glob("plugin-*/.claude-plugin/plugin.json")}


def excluded_below(root):
    """Every evals/ or __pycache__/ dir and every *.pyc file below ROOT."""
    return sorted(os.path.join(parent, name) for parent, dirs, files in os.walk(root) for name in dirs + files
                  if name in EXCLUDED_NAMES or name.endswith(".pyc"))


def row(*fields):
    return "\t".join(fields)


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def sha256_of(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hash_of(sandbox, path):
    """The `convert.py hash` of PATH (a file hash, or the tree hash of a dir)."""
    return run_convert("hash", path, sandbox=sandbox).stdout.split("\t")[0]


def row_lines(tracker_lines):
    return [line for line in tracker_lines if not line.startswith("#")]


def tracker_parts(scope):
    """(lines, header {key: value} in file order, rows as column lists) of SCOPE's tracker."""
    lines = (scope / TRACKER_NAME).read_text().splitlines()
    header = {}
    for line in lines[1:]:
        if line.startswith("#"):
            key, _colon, value = line[2:].partition(":")
            header[key] = value[1:]
    return lines, header, [line.split("\t") for line in row_lines(lines)]


def label_lines(result, label):
    return [line for line in result.stdout.splitlines() if line.startswith(f"{label} ")]


def written_paths(result):
    return sorted(line.split()[1] for line in result.stdout.splitlines() if line.startswith(WRITE_LABELS))


def files_below(root):
    return sorted(path.relative_to(root).as_posix() for path in Path(root).rglob("*") if path.is_file())


def without(tree, *roots):
    """TREE (a snapshot) without the entries at or below any of ROOTS."""
    prefixes = [str(root) for root in roots]
    return {key: value for key, value in tree.items()
            if not any(key == prefix or key.startswith(f"{prefix}/") for prefix in prefixes)}


def untracked_files(scope, tracked_paths):
    """Every file below SCOPE that is neither the tracker nor inside a tracked unit path."""
    files = (Path(parent, name).relative_to(scope).as_posix() for parent, _dirs, names in os.walk(scope)
             for name in names)
    return sorted(path for path in files if path != TRACKER_NAME
                  and not any(path == unit or path.startswith(f"{unit}/") for unit in tracked_paths))


class InstallerCoreJourneyTests(unittest.TestCase):
    """F2 installer core: discovery, dry-run, plugin ids, payload shape."""

    # AC-002 (V2, [e2e]), with AC-001, AC-020, AC-024 and AC-055 (payload dirs, tracker ids) along the same journey
    # AC-001: "When --list runs, the system shall exit 0 and list rows keyed by plugin.json ids for ai-attribution,
    #   commit-guard, env-guard, markdown-format, memory-guard, token-saver, wandavision (including
    #   wandavision/opencode-plugin/opencode-wandavision.ts), gh-issue-to-pr, ruby-lsp, markdown-lsp,
    #   mempalace-docker, dev and qa, and none for opencode-migrate."
    # AC-002: "When --dry-run --project <tmp> runs (and in a global run against a temp HOME whose plugins/ and agents/
    #   are symlinks into a temp "dotfiles" repo), every scope realpath, including the symlink targets, shall stay
    #   byte-identical. One [DRY-RUN] <STATE> line shall print per unit."
    # AC-020: "No evals/, __pycache__/ or *.pyc shall exist under <scope>/llm-agent-workflow/."
    # AC-024: "plugin-dev/skills/testing-principles shall be gone, and exactly one
    #   llm-agent-workflow/skills/testing-principles/ shall exist."
    # AC-055 (part): "tracker plugin values are plugin.json names; payload dirs are
    #   llm-agent-workflow/<plugin.json name>."
    # User Journey: --list -> --project --dry-run -> --global --dry-run (symlinked layout) -> --project install
    # Given: a temp HOME whose ~/.config/opencode/{plugins,agents} are symlinks into the "dotfiles" fixture (a
    #   submodule of a temp superproject), an empty temp project, SKILLS_MD_DIR at the stub converter fixture, and a
    #   non-TTY stdin.
    # When: (1) --list; (2) --project <p> --dry-run; (3) --global --dry-run; (4) --project <p>.
    # Then: the dry-runs change nothing and name every unit, and the real install writes exactly the listed units.
    # Verification items:
    #   - (1) exit 0; the PLUGIN column holds exactly the 13 ids above; one row has SOURCE
    #     wandavision/opencode-plugin/opencode-wandavision.ts; no row mentions opencode-migrate; every KIND is one of
    #     plugins, agents, commands, skills, payload, config.
    #   - (2) and (3) exit 0; snapshot() of every scope realpath, including the files behind the symlinks in the
    #     dotfiles repo, is equal before and after; no tracker, no opencode.json, no skills-md cache under
    #     XDG_CACHE_HOME; exactly one "[DRY-RUN] <STATE> <path>" line per unit.
    #   - (3) the dotfiles-backed units show the guard verdict (approve:<dotfiles toplevel>).
    #   - (4) exit 0; for file, dir and skill units, the set of written target paths equals the set of paths in the
    #     step (2) [DRY-RUN] lines and the set of tracker row paths (Design Doc "dry-run paths = written paths =
    #     tracker rows"); config units are printed, not written, without --merge.
    #   - (4) every payload dir is .opencode/llm-agent-workflow/<plugin.json name> (commit-guard, memory-guard,
    #     markdown-format, token-saver, ruby-lsp, markdown-lsp, mempalace-docker, qa); the tracker plugin column
    #     holds only plugin.json names.
    #   - (4) no evals/, __pycache__/ or *.pyc under .opencode/llm-agent-workflow/.
    #   - (4) exactly one .opencode/llm-agent-workflow/skills/testing-principles/, and REPO_ROOT has no
    #     plugin-dev/skills/testing-principles.
    # Pass criteria: every step exits 0, both dry-run snapshots are equal, and the three path sets are equal.
    # ROI: 100 (BV:10 x Freq:9 + Legal:0 + Defect:10) | reserved slot: user-facing multi-step journey (CLI)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, convert.py list/plan/guard, tracker, git, stub skills-md converter)
    # @real-dependency: git, filesystem (symlinks)
    # @complexity: high
    def test_list_dry_run_then_install_writes_exactly_the_listed_units(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        superproject, dotfiles = sandbox.make_dotfiles_submodule()
        dotfiles_scope = dotfiles / ".config" / "opencode"
        global_scope = link_scope(sandbox.home / ".config" / "opencode", plugins=dotfiles_scope / "plugins",
                                  agents=dotfiles_scope / "agents")
        project = sandbox.project
        cache = sandbox.home / ".cache"
        env = sandbox.env(SKILLS_MD_DIR=write_stub_converter(sandbox.fixtures / "skills-md-stub").parents[1],
                          XDG_CACHE_HOME=cache)
        repo_note = f"(repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})"

        listed = run_setup("--list", sandbox=sandbox, env=env)

        self.assertEqual((listed.returncode, listed.stderr), (EXIT_OK, ""))
        header, *list_lines = listed.stdout.splitlines()
        self.assertEqual(header.split(), ["PLUGIN", "KIND", "SOURCE"])
        rows = [tuple(line.split(None, 2)) for line in list_lines]
        self.assertEqual(rows, [tuple(line.split("|")) for line in
                                run_convert("list", "--repo", REPO_ROOT, sandbox=sandbox).stdout.splitlines()])
        self.assertEqual({plugin for plugin, _kind, _source in rows}, LISTED_IDS)
        self.assertIn(("wandavision", "plugins", WANDAVISION_TS), rows)
        self.assertNotIn("opencode-migrate", listed.stdout)
        self.assertLessEqual({kind for _plugin, kind, _source in rows}, LIST_KINDS)

        before = self.world(sandbox, superproject)
        project_units = self.planned_units(sandbox, project / ".opencode", "--scope", "project", "--project-dir",
                                           project)

        project_dry_run = run_setup("--project", project, "--dry-run", sandbox=sandbox, env=env)

        self.assertEqual((project_dry_run.returncode, project_dry_run.stderr), (EXIT_OK, ""))
        self.assertEqual(self.dry_run_lines(project_dry_run),
                         [f"[DRY-RUN] NEW {target_rel} ok" for _plugin, _unit, target_rel in project_units])
        self.assert_wrote_nothing(sandbox, superproject, before)
        self.assertFalse(os.path.lexists(project / ".opencode"))  # no tracker, no opencode.json

        global_units = self.planned_units(sandbox, global_scope, "--scope", "global")

        global_dry_run = run_setup("--global", "--dry-run", sandbox=sandbox, env=env)

        self.assertEqual((global_dry_run.returncode, global_dry_run.stderr), (EXIT_OK, ""))
        self.assertEqual(global_units, project_units)
        self.assertEqual(self.dry_run_lines(global_dry_run), [
            f"[DRY-RUN] NEW {target_rel} approve:{dotfiles} {repo_note}"
            if target_rel.startswith(("plugins/", "agents/")) else f"[DRY-RUN] NEW {target_rel} ok"
            for _plugin, _unit, target_rel in global_units])
        self.assert_wrote_nothing(sandbox, superproject, before)
        self.assertFalse(os.path.lexists(global_scope / TRACKER_NAME))

        installed = run_setup("--project", project, sandbox=sandbox, env=env)

        self.assertEqual((installed.returncode, installed.stderr), (EXIT_OK, ""), installed.stdout)
        scope = project / ".opencode"
        written = sorted(line.split()[1] for line in installed.stdout.splitlines() if line.startswith(WRITE_LABELS))
        tracker_rows = [line.split("\t") for line in (scope / TRACKER_NAME).read_text().splitlines()
                        if not line.startswith("#")]
        # file and dir units only: skill units come with Task 4.4 and config units with Task 5.1 (printed, not
        # written, without --merge); this path-set equality covers them unchanged.
        self.assertEqual(written, sorted(target_rel for _plugin, _unit, target_rel in project_units))
        self.assertEqual(sorted((row[0], row[1], row[2]) for row in tracker_rows),
                         sorted((unit, plugin, target_rel) for plugin, unit, target_rel in project_units))
        self.assertEqual([row[3] for row in tracker_rows], [str(scope / row[2]) for row in tracker_rows])
        hashed = run_convert("hash", *(row[3] for row in tracker_rows), sandbox=sandbox)
        self.assertEqual(hashed.stdout.splitlines(), [f"{row[4]}\t{row[3]}" for row in tracker_rows])
        self.assertLessEqual({row[1] for row in tracker_rows}, plugin_json_names())
        self.assertEqual({row[2] for row in tracker_rows if row[0] == "dir"},
                         {f"{PAYLOAD_NAMESPACE}/{plugin_id}" for plugin_id in PAYLOAD_PLUGINS})
        for plugin_id in PAYLOAD_PLUGINS:
            self.assertTrue((scope / PAYLOAD_NAMESPACE / plugin_id).is_dir(), plugin_id)
        self.assertEqual(excluded_below(scope / PAYLOAD_NAMESPACE), [])
        self.assertEqual(untracked_files(scope, {row[2] for row in tracker_rows}), [])
        # AC-024 (exactly one llm-agent-workflow/skills/testing-principles/) is asserted from Task 6.1 on.

    def world(self, sandbox, superproject):
        """Snapshots of every scope realpath (the dotfiles files behind the symlinks included) and the temp dir."""
        return snapshot(sandbox.home), snapshot(superproject), snapshot(sandbox.project), os.listdir(sandbox.tmpdir)

    def assert_wrote_nothing(self, sandbox, superproject, before):
        self.assertEqual(self.world(sandbox, superproject), before)
        self.assertFalse(os.path.lexists(sandbox.home / ".cache" / PAYLOAD_NAMESPACE))  # no skills-md cache

    def planned_units(self, sandbox, scope_root, *scope_args):
        """(plugin, unit, target_rel) per `convert.py plan` row for SCOPE_ROOT, staged into a fresh dir."""
        stage = sandbox.root / f"stage-{len(list(sandbox.root.glob('stage-*')))}"
        stage.mkdir()
        result = run_convert("plan", "--repo", REPO_ROOT, "--scope-root", scope_root, *scope_args, "--stage", stage,
                             sandbox=sandbox)
        self.assertEqual((result.returncode, result.stderr), (EXIT_OK, ""))
        rows = (line.split("\t") for line in result.stdout.splitlines())
        return [(fields[0], fields[1], fields[3]) for fields in rows]

    def dry_run_lines(self, result):
        return [line for line in result.stdout.splitlines() if line.startswith("[DRY-RUN]")]


class NoticeTests(unittest.TestCase):
    """F2 notices: report-only lines about files the installer does not own."""

    # AC-054 (D, [e2e]) + AC-040 (D, [e2e])
    # AC-054: "A commands/wandavision.md with no tracker row gives [SHADOWED] commands/wandavision.md and stays
    #   byte-identical. A plugin recorded in the other scope's tracker gives the double-load WARN."
    # AC-040: "A git-untracked plugins/*.ts referencing mempal_ gets one overlap notice and stays byte-identical."
    # Given: a global scope whose plugins/ is symlinked into the dotfiles fixture, holding a git-untracked
    #   plugins/opencode-mempalace-hooks.ts that contains "mempal_"; an untracked commands/wandavision.md whose body
    #   differs from the wandavision skill; no global tracker rows for either; a project tracker
    #   (<project>/.opencode/.opencode-setup-tracker) that records plugins/opencode-commit-guard.ts.
    # When: --global --plugin mempalace-docker --plugin wandavision --plugin commit-guard --allow-repo <dotfiles>,
    #   run from the project dir.
    # Then: each notice prints once, and neither user file changes.
    # Verification items:
    #   - exactly one mempalace overlap notice, naming plugins/opencode-mempalace-hooks.ts
    #   - a "[SHADOWED] commands/wandavision.md" line
    #   - the double-load WARN for commit-guard ("opencode loads both copies; hooks run twice")
    #   - sha256 of both user files is equal before and after; the tracker has no row for either path
    # Pass criteria: all three notices are present exactly once, both files are byte-identical, and exit 0.
    # ROI: 56 (BV:7 x Freq:7 + Legal:0 + Defect:7) | additional slot (> 50)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, both scope trackers, git)
    # @real-dependency: git (untracked detection), filesystem
    # @complexity: medium
    def test_notices_report_user_files_and_leave_them_byte_identical(self):
        self.skipTest("skeleton: AC-054, AC-040")


class LegacyMigrationTests(unittest.TestCase):
    """D9 legacy removal (ai-attribution and gh-issue-to-pr slices)."""

    # AC-010 (D9, [e2e]) + AC-043 (D007, [e2e]) + AC-055 (D003, [e2e], alias half)
    # AC-010: "Tracker-recorded legacy attribution files present on disk shall be removed with [REMOVED-LEGACY] and
    #   their repo named. Copies with no tracker row shall stay ([LEGACY-UNTRACKED])."
    # AC-043: "agents/gh-issue-to-pr.md is present and agents/opencode-gh-issue-to-pr.md is gone (tracked legacy).
    #   The body has no AskUserQuestion, and no gh snippet is printed."
    # AC-055 (part): "--plugin ai-attribution works; --plugin attribution works with WARN: use ai-attribution"
    # User scenario: an existing opencode user re-runs the installer with the old dir-derived plugin name (Rollout
    #   step 6).
    # Given: a global scope with plugins/ and agents/ symlinked into the dotfiles fixture; a v2 tracker recording
    #   plugins/opencode-claude-attribution.ts and agents/opencode-gh-issue-to-pr.md, both present on disk; a
    #   git-untracked commands/claude-attribution.md with no tracker row.
    # When: (1) --global --plugin attribution --plugin gh-issue-to-pr --allow-repo <dotfiles>;
    #   (2) --global --plugin ai-attribution --dry-run.
    # Then: tracked legacy files go, the untracked copy stays, and the old name maps to the new id.
    # Verification items:
    #   - (1) output has "WARN: use ai-attribution"; the ai-attribution units install and their tracker rows carry
    #     plugin "ai-attribution"
    #   - (1) "[REMOVED-LEGACY] plugins/opencode-claude-attribution.ts" followed by "(repo: <dotfiles toplevel>
    #     <origin>)", and the file is gone; the same for agents/opencode-gh-issue-to-pr.md
    #   - (1) "[LEGACY-UNTRACKED] commands/claude-attribution.md"; that file is byte-identical
    #   - (1) agents/gh-issue-to-pr.md exists and contains no "AskUserQuestion"; the output has no gh MCP snippet
    #   - (2) exit 0 and no alias WARN
    # Pass criteria: legacy rows are removed from the tracker, both invocations exit 0, and the untracked copy is
    #   unchanged.
    # ROI: 64 (BV:8 x Freq:7 + Legal:0 + Defect:8) | additional slot (> 50)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, mapping.json legacy list, tracker v2, git)
    # @real-dependency: git, filesystem (symlinks)
    # @complexity: medium
    def test_rerun_with_old_plugin_name_removes_tracked_legacy_files_only(self):
        self.skipTest("skeleton: AC-010, AC-043, AC-055")


class TrackerJourneyTests(unittest.TestCase):
    """F4 tracker v2 and uninstall."""

    # AC-007 (D8, [e2e], bullets 1-5) + AC-008 (D8, [e2e])
    # AC-007: "After an install: the tracker shall start with # setup-opencode.sh tracker v2; each written unit shall
    #   have one row with 6 columns; realpath shall equal realpath(<scope>/<path>); repo shall be the git toplevel
    #   or -; a second --plugin markdown-lsp run shall keep the first run's rows"
    # AC-008: "--uninstall shall delete the tracker-recorded realpaths (behind the guard) and the matching config
    #   leaves, keep leaves whose hash differs ([KEPT]) and files that have no row (SAME-foreign included), and
    #   delete the tracker file only when no rows remain; --uninstall --plugin X keeps the other rows."
    # User Journey: install -> second --plugin run -> --uninstall --plugin -> --uninstall
    # Given: an empty temp project, plus one pre-seeded foreign file that is byte-identical to the verbatim port
    #   (.opencode/plugins/opencode-token-saver.ts copied from plugin-token-saver/plugins/), with no tracker row.
    #   The project is an empty git repo, so the repo column names a toplevel. Uninstall deletes recorded realpaths
    #   only, never a parent dir the install created (that would be a delete outside a recorded realpath), so
    #   .opencode/commands/ and .opencode/llm-agent-workflow/ exist before step (1) too.
    # When: (1) --project <p> --plugin commit-guard --plugin token-saver; (2) --project <p> --plugin markdown-lsp;
    #   (3) --project <p> --uninstall --plugin markdown-lsp; (4) --project <p> --uninstall.
    # Then: the tracker records every written unit cumulatively, and uninstall restores the pre-install tree.
    # Verification items:
    #   - (1) line 1 is "# setup-opencode.sh tracker v2"; the header carries installed_at, repo_root, scope,
    #     scope_root, payload_root and columns; each written unit has exactly one row of 6 columns (type plugin path
    #     realpath hash repo); realpath == os.path.realpath(<scope>/<path>); repo equals `git rev-parse
    #     --show-toplevel` for the realpath, or "-"; rows are sorted
    #   - (1) the pre-seeded token-saver file prints [SAME] and gets no row
    #   - (2) every row from (1) is still present and unchanged; the markdown-lsp rows are added
    #   - (3) the markdown-lsp rows and their recorded realpaths are gone; the commit-guard and token-saver rows and
    #     files are unchanged
    #   - (4) every remaining recorded realpath is deleted ([REMOVED]); the pre-seeded file is still there and
    #     byte-identical; the tracker file is gone
    #   - (4) snapshot(project) equals the snapshot taken before step (1) (Design Doc correctness definition 5)
    # Pass criteria: every step exits 0, the row checks hold after each step, and the final snapshot equals the
    #   initial one.
    # ROI: 99 (BV:10 x Freq:9 + Legal:0 + Defect:9) | reserved slot: user-facing multi-step journey (CLI)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, convert.py plan/guard/tracker, git)
    # @real-dependency: git, filesystem
    # @complexity: high
    def test_install_rerun_and_uninstall_keep_tracker_and_tree_consistent(self):
        sandbox = self.new_sandbox()
        project = sandbox.make_repo(sandbox.project, files={})
        scope = project / ".opencode"
        foreign = write(scope / TOKEN_SAVER_TS, SOURCES[TOKEN_SAVER_TS].read_text())
        for name in ("commands", PAYLOAD_NAMESPACE):
            (scope / name).mkdir()
        before = snapshot(project)
        repo_note = f"(repo: {project})"
        markdown_lsp_dir = scope / MARKDOWN_LSP_KEY[2]

        first = run_setup("--project", project, "--plugin", "commit-guard", "--plugin", "token-saver", sandbox=sandbox)

        self.assert_clean_exit(first)
        self.assertIn(f"[SAME] {TOKEN_SAVER_TS} {repo_note}", first.stdout.splitlines())
        self.assertEqual(written_paths(first), sorted(path for _type, _plugin, path in FIRST_RUN_KEYS))
        lines, header, rows = tracker_parts(scope)
        self.assertEqual(lines[0], TRACKER_V2_MAGIC)
        self.assertEqual(list(header), list(TRACKER_HEADER_KEYS))
        self.assertEqual({key: header[key] for key in ("repo_root", "scope", "scope_root", "payload_root", "columns")},
                         {"repo_root": str(REPO_ROOT), "scope": "project", "scope_root": str(scope),
                          "payload_root": PAYLOAD_NAMESPACE, "columns": TRACKER_COLUMNS})
        self.assertRegex(header["installed_at"], INSTALLED_AT)
        self.assert_rows_describe_their_units(sandbox, scope, rows, FIRST_RUN_KEYS)
        first_rows = row_lines(lines)

        second = run_setup("--project", project, "--plugin", "markdown-lsp", sandbox=sandbox)

        self.assert_clean_exit(second)
        self.assertEqual(written_paths(second), [MARKDOWN_LSP_KEY[2]])
        lines, _header, rows = tracker_parts(scope)
        self.assert_rows_describe_their_units(sandbox, scope, rows, FIRST_RUN_KEYS + (MARKDOWN_LSP_KEY,))
        markdown_lsp_row = row(*MARKDOWN_LSP_KEY, str(markdown_lsp_dir), hash_of(sandbox, markdown_lsp_dir),
                               str(project))
        self.assertEqual(row_lines(lines), sorted(first_rows + [markdown_lsp_row]))
        tracker_after_second = (scope / TRACKER_NAME).read_text()
        tree_after_second = snapshot(project)

        partial = run_setup("--project", project, "--uninstall", "--plugin", "markdown-lsp", sandbox=sandbox)

        self.assert_clean_exit(partial)
        self.assertEqual(label_lines(partial, "[REMOVED]"), [f"[REMOVED] {MARKDOWN_LSP_KEY[2]} {repo_note}"])
        self.assertFalse(os.path.lexists(markdown_lsp_dir))
        self.assertEqual((scope / TRACKER_NAME).read_text(),
                         tracker_after_second.replace(f"{markdown_lsp_row}\n", "", 1))
        self.assertEqual(without(snapshot(project), scope / TRACKER_NAME),
                         without(tree_after_second, scope / TRACKER_NAME, markdown_lsp_dir))

        final = run_setup("--project", project, "--uninstall", sandbox=sandbox)

        self.assert_clean_exit(final)
        self.assertEqual(label_lines(final, "[REMOVED]"),
                         [f"[REMOVED] {path} {repo_note}" for _type, _plugin, path in sorted(FIRST_RUN_KEYS)])
        self.assertIn("  removed: 4", final.stdout.splitlines())
        self.assertEqual(foreign.read_bytes(), SOURCES[TOKEN_SAVER_TS].read_bytes())
        self.assertFalse(os.path.lexists(scope / TRACKER_NAME))
        self.assertEqual(snapshot(project), before)

    # AC-009 (D8, [e2e]) + AC-011 (D8, [e2e]) + AC-007 (D8, [e2e], bullet 6)
    # AC-009: "A v1 tracker shall be read as rows with plugin ? and hash -. Before any re-install, --uninstall shall
    #   delete those files by their current realpath. On re-install, identical files (SAME-v1) get v2 rows with
    #   plugin, realpath, hash and repo filled in; differing files are CONFLICT."
    # AC-011: "For an existing target with no tracker row: differing content, non-TTY, without --force gives [SKIP];
    #   identical content gives [SAME] and no row (SAME-foreign); --force on differing content backs up to
    #   llm-agent-workflow/.backup/<ts>/ first and then writes a row."
    # AC-007 (bullet 6): "an identical target with no row (SAME-foreign) shall print [SAME] and get no row; an
    #   identical target with a v1 row (SAME-v1) shall have its row upgraded to v2"
    # Branch coverage, one fresh sandbox per subTest (the live machine's first run: 12 v1 rows, Rollout step 2):
    #   a. Given a v1 tracker ("# setup-opencode.sh tracker v1" + logical paths) whose plugins/ entries resolve into
    #      the dotfiles fixture. When --global --uninstall. Then each file is deleted at its current realpath inside
    #      the dotfiles repo, and the tracker is gone. The dotfiles fixture is a submodule and a v1 header has no
    #      allowed_repos, so the delete needs approval (F8 G3): without --allow-repo every row is BLOCKED and nothing
    #      changes; with --allow-repo <dotfiles> the files go.
    #   b. Given the same v1 tracker, one file identical to the new output and one differing. When --global
    #      --allow-repo <dotfiles> (with --plugin for the two plugins, to keep the run short). Then the identical file
    #      prints [SAME] and its row becomes v2 (plugin = plugin.json id, realpath, sha256 hash, repo = dotfiles
    #      toplevel); the differing file prints [CONFLICT] and then [SKIP] (non-TTY), with its content unchanged and
    #      its v1 row kept as read (plugin ?, hash -).
    #   c. Given no tracker and two existing project targets, one identical and one differing. When --project <p>.
    #      Then the differing one prints [SKIP] with no row and no change; the identical one prints [SAME] with no row.
    #   d. Given c's differing target. When --project <p> --force. Then the old content is at
    #      .opencode/llm-agent-workflow/.backup/<UTC ts>/<target_rel> (byte-identical to the old file, outside
    #      llm-agent-workflow/skills/), the new content is written, and a v2 row is added. TZ is 8 hours off UTC, so a
    #      local-time stamp would show.
    # Pass criteria: every sub-scenario's labels, file hashes and tracker rows match, and every sub-scenario exits 0
    #   (a's unapproved first run exits 1, a BLOCKED delete).
    # Interpretation (Low), settled in Task 1.3c: b and c exit 0 because a skipped CONFLICT is neither FAILED nor
    #   BLOCKED (Design Doc F2 exit-code rule).
    # ROI: 63 (BV:9 x Freq:6 + Legal:0 + Defect:9) | additional slot (> 50)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, tracker v1 reader, backup path, git)
    # @real-dependency: git, filesystem (symlinks)
    # @complexity: high
    def test_targets_without_a_v2_row_follow_the_same_skip_and_conflict_rules(self):
        with self.subTest("a: --uninstall deletes v1 files at their current realpath"):
            sandbox = self.new_sandbox()
            files = {COMMIT_GUARD_TS: USER_TEXT, TOKEN_SAVER_TS: SOURCES[TOKEN_SAVER_TS].read_text()}
            superproject, dotfiles, tracker = self.v1_world(sandbox, files)
            repo_note = f"(repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})"
            tracker_before, dotfiles_before = tracker.read_bytes(), snapshot(superproject)

            unapproved = run_setup("--global", "--uninstall", sandbox=sandbox)

            self.assertEqual(unapproved.returncode, EXIT_FAILED, unapproved.stdout + unapproved.stderr)
            self.assertEqual(label_lines(unapproved, "[BLOCKED]"), [
                f"[BLOCKED] {target_rel}: G3 needs approval; re-run with --allow-repo {dotfiles} {repo_note}"
                for target_rel in files])
            self.assertEqual((tracker.read_bytes(), snapshot(superproject)), (tracker_before, dotfiles_before))

            removed = run_setup("--global", "--uninstall", "--allow-repo", dotfiles, sandbox=sandbox)

            self.assert_clean_exit(removed)
            lines = removed.stdout.splitlines()
            self.assertEqual(label_lines(removed, "[REMOVED]"), [f"[REMOVED] {path} {repo_note}" for path in files])
            self.assertEqual(lines[lines.index(f"Repo {dotfiles}"):][:4], [
                f"Repo {dotfiles}", *(f"  removed {dotfiles / path}" for path in files),
                f"  commit these yourself in {dotfiles}"])
            for path in files:
                self.assertFalse(os.path.lexists(dotfiles / path), path)
            self.assertFalse(os.path.lexists(tracker))

        with self.subTest("b: a re-install upgrades a SAME-v1 row and skips a v1 CONFLICT"):
            sandbox = self.new_sandbox()
            files = {COMMIT_GUARD_TS: SOURCES[COMMIT_GUARD_TS].read_text(), TOKEN_SAVER_TS: USER_TEXT}
            _superproject, dotfiles, tracker = self.v1_world(sandbox, files)
            repo_note = f"(repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})"

            result = run_setup("--global", "--plugin", "commit-guard", "--plugin", "token-saver", "--allow-repo",
                               dotfiles, sandbox=sandbox)

            self.assert_clean_exit(result)
            lines = result.stdout.splitlines()
            self.assertIn(f"[SAME] {COMMIT_GUARD_TS} {repo_note}", lines)
            conflict = lines.index(f"[CONFLICT] {TOKEN_SAVER_TS} {repo_note}")
            self.assertEqual(lines[conflict + 1],
                             f"[SKIP] {TOKEN_SAVER_TS} (conflict; pass --force to overwrite) {repo_note}")
            self.assertEqual((dotfiles / TOKEN_SAVER_TS).read_text(), USER_TEXT)
            tracker_lines = tracker.read_text().splitlines()
            self.assertEqual(tracker_lines[0], TRACKER_V2_MAGIC)
            ts_rows = [line for line in row_lines(tracker_lines) if line.split("\t")[2] in files]
            self.assertEqual(ts_rows, [
                row("file", "?", TOKEN_SAVER_TS, str(dotfiles / TOKEN_SAVER_TS), NO_VALUE, str(dotfiles)),
                row("file", "commit-guard", COMMIT_GUARD_TS, str(dotfiles / COMMIT_GUARD_TS),
                    sha256_of(SOURCES[COMMIT_GUARD_TS]), str(dotfiles))])

        with self.subTest("c: targets with no row are SAME-foreign or a skipped CONFLICT, and get no row"):
            sandbox = self.new_sandbox()
            scope, same, differing = self.no_row_world(sandbox)

            result = run_setup("--project", sandbox.project, "--plugin", "commit-guard", sandbox=sandbox)

            self.assert_clean_exit(result)
            lines = result.stdout.splitlines()
            self.assertIn(f"[SAME] {COMMIT_GUARD_TS}", lines)
            conflict = lines.index(f"[CONFLICT] {COMMIT_GUARD_COMMAND}")
            self.assertEqual(lines[conflict + 1],
                             f"[SKIP] {COMMIT_GUARD_COMMAND} (conflict; pass --force to overwrite)")
            self.assertEqual((same.read_bytes(), differing.read_text()),
                             (SOURCES[COMMIT_GUARD_TS].read_bytes(), USER_TEXT))
            self.assertEqual([fields[2] for fields in tracker_parts(scope)[2]], [f"{PAYLOAD_NAMESPACE}/commit-guard"])
            self.assertFalse(os.path.lexists(scope / PAYLOAD_NAMESPACE / ".backup"))

        with self.subTest("d: --force backs a CONFLICT up, then writes it and adds a v2 row"):
            sandbox = self.new_sandbox()
            scope, same, differing = self.no_row_world(sandbox)
            started = datetime.datetime.now(datetime.timezone.utc)

            result = run_setup("--project", sandbox.project, "--plugin", "commit-guard", "--force", sandbox=sandbox,
                               env=sandbox.env(TZ="PHT-8"))

            self.assert_clean_exit(result)
            stamps = sorted((scope / PAYLOAD_NAMESPACE / ".backup").iterdir())
            self.assertEqual(len(stamps), 1)
            self.assertRegex(stamps[0].name, BACKUP_STAMP)
            stamp = datetime.datetime.strptime(stamps[0].name, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
            self.assertLess(abs(stamp - started), datetime.timedelta(minutes=10))
            backup = stamps[0] / COMMIT_GUARD_COMMAND
            self.assertEqual(files_below(stamps[0]), [COMMIT_GUARD_COMMAND])
            self.assertEqual(backup.read_text(), USER_TEXT)
            self.assertNotIn(scope / PAYLOAD_NAMESPACE / "skills", backup.parents)
            lines = result.stdout.splitlines()
            conflict = lines.index(f"[CONFLICT] {COMMIT_GUARD_COMMAND}")
            self.assertEqual(lines[conflict + 1:conflict + 3],
                             [f"  backup: {backup}", f"[OVERWRITE] {COMMIT_GUARD_COMMAND}"])
            self.assertEqual(differing.read_bytes(), SOURCES[COMMIT_GUARD_COMMAND].read_bytes())
            self.assertIn(row("file", "commit-guard", COMMIT_GUARD_COMMAND, str(differing),
                              sha256_of(SOURCES[COMMIT_GUARD_COMMAND]), NO_VALUE),
                          row_lines(tracker_parts(scope)[0]))
            self.assertEqual(same.read_bytes(), SOURCES[COMMIT_GUARD_TS].read_bytes())

    # AC-008 (D8, [e2e], the file half of [KEPT]; config leaves come with ConfigMergeTests in Task 5.1)
    # Design Doc F4 (revision 1.5) "Uninstall": "If the path now resolves elsewhere, for example because the symlink
    #   changed, and the recorded realpath is still inside the current roots, it is deleted only when its hash
    #   matches. Otherwise it is [KEPT]. A recorded realpath outside the current roots is always [KEPT] (see
    #   Validation on read)."
    # Added by Task 1.3c as a third method (the task's choice), beside the two budgeted journeys.
    # Branch coverage, one fresh sandbox per subTest:
    #   Given: a global scope whose plugins/ symlink pointed at an old dir while commit-guard was installed, then was
    #     repointed at a new dir holding a different file with the same name.
    #   When: --global --uninstall.
    #   Then, by where the old dir lies and whether the recorded file changed:
    #   - outside the scope: `tracker read` keeps the row verbatim and warns, so the installer never sees it; both
    #     files stay, and the tracker stays with that one row
    #   - inside the scope, edited: [KEPT] plugins/opencode-commit-guard.ts; both files and the row stay
    #   - inside the scope, unchanged: [REMOVED] deletes the recorded (old) file only; the new file stays, and the
    #     tracker is gone
    # Pass criteria: every case exits 0, the new file is byte-identical, and the old file and the row match the case.
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, convert.py guard/hash/tracker)
    # @real-dependency: filesystem (symlinks)
    # @complexity: medium
    def test_uninstall_after_a_repointed_scope_symlink_keeps_what_it_cannot_prove_is_ours(self):
        kept_line = f"[KEPT] {COMMIT_GUARD_TS} (moved; the recorded hash does not match)"
        # name: (the old dir lies outside the scope, the recorded file was edited, the expected [KEPT] lines, the
        #   removed and kept counts, whether the recorded file and its row stay)
        cases = {
            "the old dir is outside the scope": (True, False, [], 2, 0, True),
            "the old dir is inside the scope, and the recorded file was edited": (False, True, [kept_line], 2, 1, True),
            "the old dir is inside the scope, and the recorded file is unchanged": (False, False, [], 3, 0, False),
        }
        for name, (outside, edited, kept_lines, removed, kept, stays) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                scope = sandbox.home / ".config" / "opencode"
                old = (sandbox.fixtures / "dotfiles-old" if outside else scope / "plugins-old") / "plugins"
                link_scope(scope, plugins=old)
                self.assert_clean_exit(run_setup("--global", "--plugin", "commit-guard", sandbox=sandbox))
                old_file = old / "opencode-commit-guard.ts"
                if edited:
                    old_file.write_text(USER_TEXT)
                new_file = write(sandbox.fixtures / "dotfiles-new" / "plugins" / "opencode-commit-guard.ts",
                                 OTHER_TEXT)
                (scope / "plugins").unlink()
                (scope / "plugins").symlink_to(new_file.parent)
                tracker = scope / TRACKER_NAME
                tracker_lines = tracker.read_text().splitlines()
                ts_row = next(line for line in row_lines(tracker_lines) if line.split("\t")[2] == COMMIT_GUARD_TS)
                old_content = old_file.read_bytes()
                warning = f"WARN {tracker}:{tracker_lines.index(ts_row) + 1}: {KEPT_WARNING}\n"

                result = run_setup("--global", "--uninstall", sandbox=sandbox)

                self.assertEqual(result.returncode, EXIT_OK, result.stdout + result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(result.stderr, warning if outside else "")
                self.assertEqual(COMMIT_GUARD_TS in result.stdout, not outside)
                self.assertEqual(label_lines(result, "[KEPT]"), kept_lines)
                self.assertEqual(label_lines(result, "[REMOVED]"),
                                 [f"[REMOVED] {PAYLOAD_NAMESPACE}/commit-guard", f"[REMOVED] {COMMIT_GUARD_COMMAND}"]
                                 + ([] if stays else [f"[REMOVED] {COMMIT_GUARD_TS}"]))
                self.assertEqual((lines.count(f"  removed: {removed}"), lines.count(f"  kept:    {kept}")), (1, 1))
                self.assertEqual(new_file.read_text(), OTHER_TEXT)
                self.assertEqual(old_file.read_bytes() if os.path.lexists(old_file) else None,
                                 old_content if stays else None)
                self.assertEqual(row_lines(tracker.read_text().splitlines()) if os.path.lexists(tracker) else None,
                                 [ts_row] if stays else None)

    def new_sandbox(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        return sandbox

    def assert_clean_exit(self, result):
        self.assertEqual((result.returncode, result.stderr), (EXIT_OK, ""), result.stdout)

    def assert_rows_describe_their_units(self, sandbox, scope, rows, keys):
        """ROWS are exactly KEYS' units, in sorted order, each describing the unit on disk right now."""
        self.assertEqual([tuple(fields[:3]) for fields in rows], sorted(keys))
        for fields in rows:
            self.assertEqual(len(fields), 6, fields)
            self.assertEqual(fields[3], os.path.realpath(scope / fields[2]))
            toplevel = sandbox.git(os.path.dirname(fields[3]), "rev-parse", "--show-toplevel", check=False)
            self.assertEqual(fields[5], toplevel.stdout.strip() if toplevel.returncode == 0 else NO_VALUE)
            self.assertEqual(fields[4], hash_of(sandbox, fields[3]))

    def v1_world(self, sandbox, files):
        """A global scope whose plugins/ links into the dotfiles submodule, FILES ({target_rel: text}) at their
        realpaths there, and a v1 tracker listing them; returns (superproject, dotfiles, tracker)."""
        superproject, dotfiles = sandbox.make_dotfiles_submodule()
        scope = link_scope(sandbox.home / ".config" / "opencode", plugins=dotfiles / "plugins")
        for target_rel, text in files.items():
            write(scope / target_rel, text)
        tracker = write(scope / TRACKER_NAME, "".join(f"{line}\n" for line in (*V1_TRACKER_HEADER, *files)))
        return superproject, dotfiles, tracker

    def no_row_world(self, sandbox):
        """A project scope with no tracker: the commit-guard plugin file identical to ours, its command differing."""
        scope = sandbox.project / ".opencode"
        same = write(scope / COMMIT_GUARD_TS, SOURCES[COMMIT_GUARD_TS].read_text())
        return scope, same, write(scope / COMMIT_GUARD_COMMAND, USER_TEXT)


class ConfigMergeTests(unittest.TestCase):
    """F5 config snippets and --merge."""

    # AC-012 (D10, [e2e]) + AC-050 (D13, [e2e], project half) + AC-008 (D8, [e2e], config-leaf half)
    # AC-012: "--merge into a plain opencode.json adds lsp.rumdl pointing at an executable payload script, preserves
    #   the other keys and records a config row. A differing lsp.rumdl gives CONFLICT, untouched."
    # AC-050 (part): "Project: --merge appends .opencode/llm-agent-workflow/skills to .opencode/opencode.json
    #   skills.paths, and uninstall removes only that entry."
    # AC-008 (part): "--uninstall shall delete ... the matching config leaves, keep leaves whose hash differs
    #   ([KEPT])"
    # User Journey: --merge install -> the user edits one of our leaves -> --uninstall
    # Given: a project whose .opencode/opencode.json (plain JSON) holds a user key ("theme"), skills.paths
    #   ["user/skills"], and a user mcp.mempalace entry that differs from our snippet (the live-machine conflict).
    # When: (1) --project <p> --plugin markdown-lsp --plugin mempalace-docker --merge --force; (2) the test edits
    #   lsp.rumdl.extensions in opencode.json; (3) --project <p> --uninstall.
    # Then: our leaves are added and recorded, user leaves are never touched, and uninstall removes only unchanged
    #   leaves of ours.
    # Verification items:
    #   - (1) "ADDED /lsp/rumdl" and "ADDED /skills/paths"; skills.paths == ["user/skills",
    #     ".opencode/llm-agent-workflow/skills"]
    #   - (1) lsp.rumdl.command[0], with {env:HOME} expanded, names an existing executable under
    #     .opencode/llm-agent-workflow/markdown-lsp/scripts/
    #   - (1) "CONFLICT /mcp/mempalace": the value is untouched even with --force, and the line names the pointer but
    #     not the existing value
    #   - (1) "theme" is unchanged; the first-lsp-key notice (N5) prints once
    #   - (1) the tracker has config rows for opencode.json#/lsp/rumdl and opencode.json#/skills/paths, and none for
    #     /mcp/mempalace
    #   - (3) "[KEPT] opencode.json#/lsp/rumdl" with the edited value still present; skills.paths == ["user/skills"];
    #     "theme" and mcp.mempalace unchanged; the file is still valid JSON
    # Pass criteria: every leaf matches the expected state after (1) and (3), and both runs exit 0.
    # ROI: 90 (BV:9 x Freq:9 + Legal:0 + Defect:9) | reserved slot: user-facing multi-step journey (CLI)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, convert.py merge, tracker config rows)
    # @real-dependency: filesystem
    # @complexity: high
    def test_project_merge_adds_our_leaves_and_uninstall_removes_only_ours(self):
        self.skipTest("skeleton: AC-012, AC-050, AC-008")

    # AC-013 (D10, [e2e]) + AC-050 (D13, [e2e], global half)
    # AC-013: "A .jsonc target shall stay byte-identical; the snippet is printed with no config rows."
    # AC-050 (part): "Global: the printed snippet contains skills.paths with
    #   ~/.config/opencode/llm-agent-workflow/skills."
    # Given: a global scope with ~/.config/opencode/opencode.jsonc containing comments and a user mcp entry (the live
    #   layout), and plugins/ symlinked into the dotfiles fixture.
    # When: --global --plugin markdown-lsp --merge --allow-repo <dotfiles>.
    # Then: the jsonc file is never written, and the snippet is printed for the user to paste.
    # Verification items:
    #   - opencode.jsonc is byte-identical; no opencode.json is created
    #   - stdout has a snippet with "skills": {"paths": ["~/.config/opencode/llm-agent-workflow/skills"]}
    #   - lsp.rumdl.command[0] starts with "{env:HOME}/.config/opencode/llm-agent-workflow/markdown-lsp/scripts/"
    #   - the printed snippet contains no absolute path under the temp HOME
    #   - the tracker has no config rows
    # Pass criteria: the file hash is unchanged, the snippet lines are present, no config rows exist, and exit 0.
    # ROI: 79 (BV:8 x Freq:9 + Legal:0 + Defect:7) | additional slot (> 50)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, convert.py merge/print)
    # @real-dependency: filesystem, git
    # @complexity: medium
    def test_jsonc_config_gets_a_printed_snippet_and_stays_byte_identical(self):
        self.skipTest("skeleton: AC-013, AC-050")


class SkillsMdTests(unittest.TestCase):
    """F6 skills-md integration: per-skill validation through the external converter."""

    # AC-036 (V5, [e2e]) + AC-037 (V5, [e2e]); EARS If-then: both source paths verified
    # AC-036: "SKILLS_MD_DIR=<fixture> --with-skills-md --dry-run --project <tmp> shall validate each fixture skill in
    #   a temp dir, write nothing, and report a duplicate of a dev or qa name as an ERROR."
    # AC-037: "With no SKILLS_MD_DIR, no cache, and a failing clone, skill units FAIL with one line, the others
    #   install, and the exit code is 1."
    # Branch coverage, one fresh sandbox per subTest:
    #   a. Given SKILLS_MD_DIR = a fixture skills-md repo holding scripts/opencode-convert-skill.sh (the stub) and
    #      skills/{alpha,coding-principles}/SKILL.md, where coding-principles duplicates a dev name. When --project <p>
    #      --plugin dev --with-skills-md --dry-run. Then the stub is called once per skill unit (ours and skills-md's),
    #      every DEST lies in a temp dir outside the project and HOME, snapshot(project) is unchanged, no skills-md
    #      cache exists under XDG_CACHE_HOME, and stderr has an ERROR naming coding-principles.
    #   b. Given no SKILLS_MD_DIR, an empty XDG_CACHE_HOME, and SKILLS_MD_REPO=file://<tmp>/missing.git (the clone
    #      fails). When --project <p> --plugin commit-guard --plugin markdown-lsp. Then each skill unit prints one
    #      [FAIL] line whose reason names SKILLS_MD_DIR; the payload, plugin-file and command units install ([OK])
    #      with tracker rows; the exit code is 1.
    # Pass criteria: a's stub-call count equals the number of skill units, a's snapshot is unchanged and the ERROR is
    #   present; b exits 1 with exactly one [FAIL] line per skill unit and the non-skill units installed.
    # Interpretation (Low): a's exit code follows the Error Handling row "Unit rejected; exit 3, then 1 at the end";
    #   the implementer asserts the value the installer settles on and records it here.
    # ROI: 80 (BV:8 x Freq:9 + Legal:0 + Defect:8) | every skill unit goes through IP-8 on every install
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, stub skills-md converter, git clone via file:// remote)
    # @real-dependency: git, filesystem
    # @complexity: high
    def test_skill_units_are_validated_per_skill_or_fail_without_a_source(self):
        self.skipTest("skeleton: AC-036, AC-037")


class RecipePolicyTests(unittest.TestCase):
    """F7 recipe access policy."""

    # AC-025 (D7, [e2e]); EARS If-then: the overlap path is verified too
    # AC-025: "The policy snippet shall deny recipe-* and start for build/plan only. If an agent is in both lists,
    #   then the installer exits 1 before any write."
    # Branch coverage, one fresh sandbox per subTest:
    #   a. Given a project whose .opencode/opencode.json is {}. When --project <p> --plugin dev --merge. Then
    #      agent.build.permission.skill and agent.plan.permission.skill both equal {"recipe-*": "deny", "start":
    #      "deny"}, no other agent key is written, and stdout carries the exact "[INFO] recipes: orchestrator is
    #      allowlisted; ..." guidance from Design Doc F7.
    #   b. Given the same project. When --project <p> --plugin dev --recipe-deny-agents build,orchestrator. Then the
    #      exit code is 1, stderr names orchestrator, and snapshot(project) is unchanged (no tracker, no opencode.json
    #      change, no .opencode/llm-agent-workflow/).
    # Pass criteria: a exits 0 with exactly two agent leaves and one guidance line; b exits 1 with an unchanged tree.
    # ROI: 63 (BV:7 x Freq:8 + Legal:0 + Defect:7) | additional slot (> 50)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, convert.py plan/merge)
    # @real-dependency: filesystem
    # @complexity: medium
    def test_recipe_policy_denies_build_and_plan_and_rejects_an_overlap(self):
        self.skipTest("skeleton: AC-025")


class RepositoryGuardTests(unittest.TestCase):
    """F8 write targets and repository guard."""

    # AC-049 (D12, [e2e]) + AC-048 (D12, [e2e], submodule bullet); the Design Doc's Early Verification Point
    # AC-048 (part): "If it lies in another submodule, non-TTY, without approval, then [BLOCKED] with the
    #   --allow-repo hint. With --allow-repo it is written and allowed_repos recorded."
    # AC-049: "Each unit whose realpath is in a git repo prints (repo: <toplevel> <origin>). The summary lists the
    #   files per repo with "commit these yourself", plus the superproject line for submodules."
    # User Journey: --dry-run -> install without approval -> install with --allow-repo -> re-run without the flag
    # Given: a temp HOME with ~/.config/opencode/{plugins,agents} symlinked into the dotfiles fixture (a submodule of
    #   a temp superproject), commands/ as a real dir, and the dotfiles origin set to
    #   https://user:token@example.invalid/dotfiles.git (credentials must never print).
    # When: (1) --global --plugin commit-guard --dry-run; (2) --global --plugin commit-guard;
    #   (3) --global --plugin commit-guard --allow-repo <dotfiles>; (4) --global --plugin commit-guard.
    # Then: submodule writes need one approval, which is remembered, and every write names its repo.
    # Verification items:
    #   - (1) the plugin-file unit shows approve:<dotfiles toplevel>; nothing is written
    #   - (2) "[BLOCKED] plugins/opencode-commit-guard.ts" with the --allow-repo hint; nothing is written into the
    #     dotfiles repo; the payload unit under <scope>/llm-agent-workflow/commit-guard (not in a repo) installs;
    #     exit 1
    #   - (3) the file is written at its realpath inside the dotfiles repo; its unit line ends with "(repo: <dotfiles
    #     toplevel> https://example.invalid/dotfiles.git)"; the summary groups it under the dotfiles toplevel with
    #     "commit these yourself in <toplevel>" and "then update the submodule pointer in <superproject>"; the
    #     tracker header allowed_repos holds the dotfiles toplevel; the row's realpath is the dotfiles path and its
    #     repo column is the toplevel
    #   - (1)-(4) no output line contains "user:token@"
    #   - (4) no BLOCKED line; the file prints [SAME]; exit 0 (the approval comes from the tracker header)
    # Pass criteria: the exit codes are 0, 1, 0, 0; the dotfiles repo is untouched until (3); the repo annotations and
    #   the summary lines are present in (3).
    # ROI: 110 (BV:10 x Freq:10 + Legal:0 + Defect:10) | reserved slot: user-facing multi-step journey (CLI)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, convert.py guard, tracker header, git submodules)
    # @real-dependency: git (submodule + superproject queries), filesystem (symlinks)
    # @complexity: high
    def test_submodule_write_needs_approval_once_and_reports_its_repo(self):
        sandbox = self.new_sandbox()
        superproject, dotfiles = sandbox.make_dotfiles_submodule()
        dotfiles_scope = dotfiles / ".config" / "opencode"
        scope = link_scope(sandbox.home / ".config" / "opencode", plugins=dotfiles_scope / "plugins",
                           agents=dotfiles_scope / "agents")
        (scope / "commands").mkdir()
        ts_realpath = dotfiles_scope / "plugins" / "opencode-commit-guard.ts"
        ts_source = REPO_ROOT / "plugin-commit-guard" / "plugins" / "opencode-commit-guard.ts"
        repo_note = f"(repo: {dotfiles} {DOTFILES_CLEAN_ORIGIN})"
        untouched = snapshot(superproject)
        home_before = snapshot(sandbox.home)
        install = ("--global", "--plugin", "commit-guard")

        dry_run = run_setup(*install, "--dry-run", sandbox=sandbox)

        self.assertEqual(dry_run.returncode, EXIT_OK, dry_run.stderr)
        self.assertIn(f"[DRY-RUN] NEW plugins/opencode-commit-guard.ts approve:{dotfiles} {repo_note}",
                      dry_run.stdout.splitlines())
        self.assertEqual((snapshot(sandbox.home), snapshot(superproject)), (home_before, untouched))

        unapproved = run_setup(*install, sandbox=sandbox)

        self.assertEqual(unapproved.returncode, EXIT_FAILED, unapproved.stdout + unapproved.stderr)
        self.assertIn(f"[BLOCKED] plugins/opencode-commit-guard.ts: G3 needs approval; re-run with --allow-repo "
                      f"{dotfiles} {repo_note}", unapproved.stdout.splitlines())
        self.assertIn("[OK] llm-agent-workflow/commit-guard", unapproved.stdout.splitlines())
        self.assertTrue((scope / "llm-agent-workflow" / "commit-guard" / "hooks" / "commit_guard_hook.py").is_file())
        self.assertEqual(snapshot(superproject), untouched)

        approved = run_setup(*install, "--allow-repo", dotfiles, sandbox=sandbox)

        self.assertEqual(approved.returncode, EXIT_OK, approved.stdout + approved.stderr)
        lines = approved.stdout.splitlines()
        self.assertIn(f"[OK] plugins/opencode-commit-guard.ts {repo_note}", lines)
        self.assertEqual(ts_realpath.read_bytes(), ts_source.read_bytes())
        written_in_dotfiles = snapshot(superproject)
        self.assertEqual(set(written_in_dotfiles) - set(untouched), {str(ts_realpath)})
        self.assertEqual({key: written_in_dotfiles[key] for key in untouched}, untouched)
        summary = lines[lines.index(f"Repo {dotfiles}"):]
        self.assertEqual(summary[:4], [f"Repo {dotfiles}", f"  written {ts_realpath}",
                                       f"  commit these yourself in {dotfiles}",
                                       f"  then update the submodule pointer in {superproject}"])
        tracker_lines = (scope / TRACKER_NAME).read_text().splitlines()
        self.assertIn(f"# allowed_repos: {dotfiles}", tracker_lines)
        ts_rows = [line.split("\t") for line in tracker_lines if line.startswith("file\tcommit-guard\tplugins/")]
        self.assertEqual([(row[3], row[5]) for row in ts_rows], [(str(ts_realpath), str(dotfiles))])

        rerun = run_setup(*install, sandbox=sandbox)

        self.assertEqual(rerun.returncode, EXIT_OK, rerun.stdout + rerun.stderr)
        self.assertNotIn("[BLOCKED]", rerun.stdout)
        self.assertIn(f"[SAME] plugins/opencode-commit-guard.ts {repo_note}", rerun.stdout.splitlines())
        for result in (dry_run, unapproved, approved, rerun):
            self.assertNotIn(CREDENTIALS, result.stdout + result.stderr)

    # AC-048 (D12, [e2e], skills-md and native skills bullets)
    # AC-048 (part): "If a target realpath lies in a checkout whose origin repo name is skills-md, or in a submodule
    #   of one, then [BLOCKED], nothing written, exit 1. ... Nothing shall ever be written under
    #   realpath(<scope>/skills)."
    # Given: the "skills-md" fixture (origin .../skills-md.git, nested submodule start); ~/.config/opencode/skills
    #   symlinks to that checkout; ~/.config/opencode/commands symlinks into <skills-md>/commands and
    #   ~/.config/opencode/agents symlinks into <skills-md>/start/agents (the nested submodule).
    # When: (1) --global --plugin gh-issue-to-pr --force; (2) --global --uninstall against a hand-edited v2 tracker
    #   whose row points at skills/<name>/SKILL.md inside the checkout.
    # Then: nothing inside the skills-md checkout or its submodule is ever written or deleted.
    # Verification items:
    #   - (1) "[BLOCKED] commands/gh-issue-to-pr.md" (G1, skills-md origin) and "[BLOCKED] agents/gh-issue-to-pr.md"
    #     (G1, superproject origin); --force does not override either; exit 1
    #   - (2) exit 1; the file under skills/ is still present
    #   - across (1) and (2): snapshot(realpath(<scope>/skills)) is unchanged, and `git status --porcelain` is empty
    #     in the checkout and in its start submodule
    # Pass criteria: both runs exit 1, the checkout snapshot is unchanged, and git status is clean.
    # ROI: 69 (BV:10 x Freq:6 + Legal:0 + Defect:9) | additional slot (> 50)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, convert.py guard, tracker validation, git submodules)
    # @real-dependency: git (origin and superproject queries), filesystem (symlinks)
    # @complexity: high
    def test_skills_md_checkout_and_native_skills_dir_are_never_written(self):
        sandbox = self.new_sandbox()
        skills_md = sandbox.make_skills_md()
        start = skills_md / "start"
        scope = link_scope(sandbox.home / ".config" / "opencode", commands=skills_md / "commands",
                           agents=start / "agents")
        (scope / "skills").symlink_to(skills_md)
        skill_file = skills_md / "skills" / "alpha" / "SKILL.md"
        checkout_before = snapshot(skills_md)

        forced = run_setup("--global", "--plugin", "gh-issue-to-pr", "--force", sandbox=sandbox)

        self.assertEqual(forced.returncode, EXIT_FAILED, forced.stdout + forced.stderr)
        lines = forced.stdout.splitlines()
        self.assertIn(f"[BLOCKED] commands/gh-issue-to-pr.md: G1 (repo: {skills_md} {SKILLS_MD_ORIGIN})", lines)
        # Today's hand-written agent name; Task 2.1 renames it to agents/gh-issue-to-pr.md.
        self.assertIn(f"[BLOCKED] agents/opencode-gh-issue-to-pr.md: G1 (repo: {start} {START_ORIGIN})", lines)
        self.assert_checkout_untouched(sandbox, skills_md, checkout_before)

        (scope / TRACKER_NAME).write_text(
            "# setup-opencode.sh tracker v2\n# installed_at:\n# repo_root:\n# scope: global\n# scope_root:\n"
            "# payload_root:\n# recipe_policy:\n# mcp_aliases:\n# allowed_repos:\n# skills_md:\n"
            "# columns: type plugin path realpath hash repo\n"
            f"file\tgh-issue-to-pr\tskills/alpha/SKILL.md\t{scope}/skills/alpha/SKILL.md\tsha256:{'1' * 64}\t-\n")

        uninstall = run_setup("--global", "--uninstall", sandbox=sandbox)

        self.assertEqual(uninstall.returncode, EXIT_FAILED, uninstall.stdout + uninstall.stderr)
        self.assertIn("tracker: path prefix not allowed", uninstall.stderr)
        self.assertTrue(skill_file.is_file())
        self.assert_checkout_untouched(sandbox, skills_md, checkout_before)

    def new_sandbox(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        return sandbox

    def assert_checkout_untouched(self, sandbox, skills_md, before):
        self.assertEqual(snapshot(skills_md), before)
        for repo in (skills_md, skills_md / "start"):
            self.assertEqual(sandbox.git(repo, "status", "--porcelain").stdout, "", repo)


class SkillUniquenessJourneyTests(unittest.TestCase):
    """F3 uniqueness across scanned roots (with the G1 guard)."""

    # AC-051 (D13, [e2e])
    # AC-051: "Given a scanned root that is a git checkout with origin ...skills-md.git, holding skills/start/SKILL.md
    #   with different content and skills/ruby-lsp/SKILL.md identical to ours (nested one level, like origin/main):
    #   [DUPLICATE] start (skill and command skipped; the line names both paths and "Rollout prerequisite") and
    #   [SAME-ELSEWHERE] ruby-lsp. --force changes neither and writes nothing into that checkout. When skills/start/
    #   is removed from that root and the installer re-runs, the start skill and commands/start.md install as NEW."
    # User Journey: install -> DUPLICATE reported -> the user resolves it on the skills-md side -> re-run -> NEW
    # Given: a global scope (plugins/, agents/, commands/ as real dirs) whose skills/ is the "skills-md" fixture
    #   checkout holding skills/start/SKILL.md (different from ours) and skills/ruby-lsp/SKILL.md (byte-identical to
    #   our staged ruby-lsp skill).
    # When: (1) --global --plugin dev --plugin ruby-lsp --force; (2) the test runs `git rm -r skills/start` and
    #   commits in the fixture; (3) --global --plugin dev --plugin ruby-lsp.
    # Then: the colliding skill waits for the user's fix, then installs without extra flags.
    # Verification items:
    #   - (1) "[DUPLICATE] start: ours <stage>, other <checkout>/skills/start" on one line, which also says "resolve
    #     on the skills-md side (Rollout prerequisite)"; no llm-agent-workflow/skills/start/ and no commands/start.md
    #   - (1) "[SAME-ELSEWHERE] ruby-lsp"; no llm-agent-workflow/skills/ruby-lsp/
    #   - (1) --force changes neither outcome; the checkout snapshot is unchanged and `git status --porcelain` is
    #     empty there
    #   - (3) the start skill installs as NEW at llm-agent-workflow/skills/start/ and commands/start.md as NEW, both
    #     with tracker rows; ruby-lsp is still [SAME-ELSEWHERE]
    # Pass criteria: (1) and (3) match their expected states, and the only write to the checkout is the test's own
    #   git rm.
    # ROI: 81 (BV:9 x Freq:8 + Legal:0 + Defect:9) | reserved slot: user-facing multi-step journey (CLI)
    # @category: e2e
    # @dependency: full-system (setup-opencode.sh, convert.py plan uniqueness scan, guard G1, git)
    # @real-dependency: git, filesystem
    # @complexity: high
    def test_duplicate_start_is_skipped_until_the_skills_md_side_resolves_it(self):
        self.skipTest("skeleton: AC-051")


if __name__ == "__main__":
    unittest.main()
