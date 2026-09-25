#!/usr/bin/env python3
# Tracker v2 Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.4)
# Generated: 2026-09-24 | Budget Used: 3/3 integration, 0/2 E2E (F4 E2E lives in test_setup_opencode.py)
"""Integration tests for `convert.py tracker merge|read`, plus unit cases for tracker.py.

Each integration test runs the real CLI in a subprocess against temp tracker files:

  python3 scripts/opencode/convert.py tracker merge --tracker FILE --add ROWS [--remove ROWS] --header KEY=VALUE...
  python3 scripts/opencode/convert.py tracker read  --tracker FILE [--plugin ID]

This is the level the installer's tracker state rules (AC-007, AC-008, AC-009) are pushed down to. Unit cases for
tracker.py itself are added here by the implementer (Design Doc Suites row "test_tracker.py").

  python3 -m unittest discover -s scripts/opencode/tests
"""

import errno
import os
import sys
import unittest
from unittest import mock

from _support import REPO_ROOT, Sandbox, link_scope, run_convert

# Harness: one _support.Sandbox per test (or per subTest). The scope root is <sandbox home>/.config/opencode and the
# tracker is <scope>/.opencode-setup-tracker; ROWS files live in fixtures/, outside the scope, so a listing of the
# scope dir shows any temp or partial file a merge leaves behind. Where a test needs a repo column, plugins/ is a
# symlink into a real `git init` repo. HOME points at the sandbox. No mocks for the CLI: the tracker is plain files
# (Mock Boundary "File system: No"); the one unit case that needs a failing write patches os.replace / os.fsync.

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2
TRACKER_NAME = ".opencode-setup-tracker"
NO_VALUE = "-"
WRITABLE_SCOPE_DIRS = ("plugins", "agents", "commands", "llm-agent-workflow")

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_4 = "sha256:" + "4" * 64
HASH_5 = "sha256:" + "5" * 64

MAGIC_V1 = "# setup-opencode.sh tracker v1"
MAGIC_V2 = "# setup-opencode.sh tracker v2"
COLUMNS_LINE = "# columns: type plugin path realpath hash repo"
HEADER_ARGS = (
    "installed_at=2026-09-24T12:00:00Z",
    "repo_root=/srv/llm-agent-workflow",
    "scope=global",
    "scope_root=/home/u/.config/opencode",
    "payload_root=llm-agent-workflow",
    "recipe_policy=deny=build,plan allow=orchestrator",
    "mcp_aliases=github=github-mcp",
    "allowed_repos=",
    "skills_md=/srv/skills-md@3f2c1ab",
)
HEADER_LINES = [
    MAGIC_V2,
    "# installed_at: 2026-09-24T12:00:00Z",
    "# repo_root: /srv/llm-agent-workflow",
    "# scope: global",
    "# scope_root: /home/u/.config/opencode",
    "# payload_root: llm-agent-workflow",
    "# recipe_policy: deny=build,plan allow=orchestrator",
    "# mcp_aliases: github=github-mcp",
    "# allowed_repos:",
    "# skills_md: /srv/skills-md@3f2c1ab",
    COLUMNS_LINE,
]
FIRST_ROW_LINE = len(HEADER_LINES) + 1
V1_HEADER_LINES = [
    MAGIC_V1,
    "# installed_at: 2026-01-02T03:04:05Z",
    "# repo_root: /srv/llm-agent-workflow",
    "# scope: global",
]


def import_tracker():
    """Import scripts/opencode/tracker.py without writing __pycache__/ into the repo."""
    scripts_dir = str(REPO_ROOT / "scripts" / "opencode")
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, scripts_dir)
    try:
        import tracker
    finally:
        sys.path.remove(scripts_dir)
        sys.dont_write_bytecode = previous
    return tracker


def make_scope(sandbox, **links):
    """<sandbox home>/.config/opencode, with each named subdir symlinked to its (created) target."""
    return link_scope(sandbox.home / ".config" / "opencode", **links)


def row(*fields):
    return "\t".join(fields)


def text_of(*lines):
    return "".join(f"{line}\n" for line in lines)


def write_rows(path, *rows):
    path.write_text(text_of(*rows))
    return path


def entries(directory):
    return sorted(os.listdir(directory))


class TrackerCliTestCase(unittest.TestCase):
    def new_sandbox(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        return sandbox

    def read(self, sandbox, tracker, *args, env=None, cwd=None):
        return run_convert("tracker", "read", "--tracker", tracker, *args, sandbox=sandbox, env=env, cwd=cwd)

    def merge(self, sandbox, tracker, add, *args, env=None, cwd=None):
        return run_convert("tracker", "merge", "--tracker", tracker, "--add", add, *args, sandbox=sandbox, env=env,
                           cwd=cwd)

    def empty_rows(self, sandbox):
        return write_rows(sandbox.fixtures / "empty.rows")

    def assert_ok(self, result, stdout=""):
        self.assertEqual((result.returncode, result.stderr), (EXIT_OK, ""))
        self.assertEqual(result.stdout, stdout)

    def assert_fatal(self, result, stderr_line):
        self.assertEqual((result.returncode, result.stdout), (EXIT_FATAL, ""), result.stderr)
        self.assertEqual(result.stderr, f"{stderr_line}\n")

    def assert_refused(self, sandbox, scope, content, stderr_line, env=None, cwd=None):
        """Both commands exit 1 with STDERR_LINE, and the tracker and the scope dir listing stay as they were."""
        tracker = scope / TRACKER_NAME
        tracker.write_bytes(content if isinstance(content, bytes) else content.encode())
        before = (tracker.read_bytes(), entries(scope))
        valid_add = write_rows(sandbox.fixtures / "valid.rows",
                               row("file", "commit-guard", "commands/new.md", f"{scope}/commands/new.md", HASH_5,
                                   NO_VALUE))

        self.assert_fatal(self.read(sandbox, tracker, env=env, cwd=cwd), stderr_line)
        self.assert_fatal(self.merge(sandbox, tracker, valid_add, "--header", "scope=project", env=env, cwd=cwd),
                          stderr_line)

        self.assertEqual((tracker.read_bytes(), entries(scope)), before)


class TrackerMergeReadTests(TrackerCliTestCase):
    # Supports AC-007 (D8) and AC-008 (D8)
    # AC-007 (part): "the tracker shall start with # setup-opencode.sh tracker v2; each written unit shall have one
    #   row with 6 columns ... a second --plugin markdown-lsp run shall keep the first run's rows"
    # AC-008 (part): "delete the tracker file only when no rows remain"
    # Given: an empty scope root and two ROWS files (commit-guard rows, markdown-lsp rows) in the 6-column format.
    # When: (1) merge --add <commit-guard rows> with the header keys; (2) merge --add <markdown-lsp rows>;
    #   (3) merge --add <one commit-guard row with the same (type, path) and a new hash>; (4) merge --remove <every
    #   row>.
    # Then: rows are keyed by (type, path), merged cumulatively and written atomically.
    # Verification items:
    #   - after (1): line 1 is "# setup-opencode.sh tracker v2"; the header has installed_at, repo_root, scope,
    #     scope_root, payload_root, recipe_policy, mcp_aliases, allowed_repos, skills_md and columns; rows are sorted
    #     and have exactly 6 whitespace-separated columns
    #   - after (2): every row from (1) is present and unchanged
    #   - after (3): one row for that (type, path), carrying the new hash (the SAME-tracked hash refresh), not two
    #   - after every step: no temp or partial file is left beside the tracker (atomic rename)
    #   - after (4): the tracker file no longer exists
    # Pass criteria: every step exits 0, and `tracker read` output matches the expected rows after each step.
    # ROI: 90 (BV:9 x Freq:9 + Legal:0 + Defect:9)
    # @category: core-functionality
    # @dependency: convert.py tracker, tracker.py
    # @real-dependency: filesystem
    # @complexity: medium
    def test_merge_is_cumulative_by_type_and_path_and_deletes_an_empty_tracker(self):
        sandbox = self.new_sandbox()
        scope = make_scope(sandbox)
        tracker = scope / TRACKER_NAME
        cg_file = row("file", "commit-guard", "plugins/opencode-commit-guard.ts",
                      f"{scope}/plugins/opencode-commit-guard.ts", HASH_1, NO_VALUE)
        cg_dir = row("dir", "commit-guard", "llm-agent-workflow/commit-guard",
                     f"{scope}/llm-agent-workflow/commit-guard", HASH_2, NO_VALUE)
        md_config = row("config", "markdown-lsp", "opencode.json#/lsp/rumdl", f"{scope}/opencode.json", HASH_3,
                        NO_VALUE)
        md_dir = row("dir", "markdown-lsp", "llm-agent-workflow/markdown-lsp",
                     f"{scope}/llm-agent-workflow/markdown-lsp", HASH_4, NO_VALUE)
        cg_file_refreshed = row("file", "commit-guard", "plugins/opencode-commit-guard.ts",
                                f"{scope}/plugins/opencode-commit-guard.ts", HASH_5, NO_VALUE)
        # Unsorted on purpose: the tracker must sort them.
        commit_guard_rows = write_rows(sandbox.fixtures / "commit-guard.rows", cg_file, cg_dir)
        markdown_lsp_rows = write_rows(sandbox.fixtures / "markdown-lsp.rows", md_dir, md_config)
        refreshed_rows = write_rows(sandbox.fixtures / "refreshed.rows", cg_file_refreshed)

        # (1)
        self.assert_ok(self.merge(sandbox, tracker, commit_guard_rows, "--header", *HEADER_ARGS))
        lines = tracker.read_text().splitlines()
        self.assertEqual(lines, HEADER_LINES + [cg_dir, cg_file])
        self.assertEqual([len(line.split()) for line in lines[len(HEADER_LINES):]], [6, 6])
        self.assert_ok(self.read(sandbox, tracker), stdout=text_of(cg_dir, cg_file))
        self.assertEqual(entries(scope), [TRACKER_NAME])

        # (2)
        self.assert_ok(self.merge(sandbox, tracker, markdown_lsp_rows))
        self.assertEqual(tracker.read_text(), text_of(*HEADER_LINES, md_config, cg_dir, md_dir, cg_file))
        self.assert_ok(self.read(sandbox, tracker), stdout=text_of(md_config, cg_dir, md_dir, cg_file))
        self.assertEqual(entries(scope), [TRACKER_NAME])

        # (3)
        self.assert_ok(self.merge(sandbox, tracker, refreshed_rows))
        self.assertEqual(tracker.read_text(), text_of(*HEADER_LINES, md_config, cg_dir, md_dir, cg_file_refreshed))
        read_all = self.read(sandbox, tracker)
        self.assert_ok(read_all, stdout=text_of(md_config, cg_dir, md_dir, cg_file_refreshed))
        self.assertEqual(entries(scope), [TRACKER_NAME])

        # (4): the remove file is the read output, as the installer's uninstall passes it.
        remove_all = sandbox.fixtures / "remove-all.rows"
        remove_all.write_text(read_all.stdout)
        self.assert_ok(self.merge(sandbox, tracker, self.empty_rows(sandbox), "--remove", remove_all))
        self.assertFalse(os.path.lexists(tracker))
        self.assertEqual(entries(scope), [])

    # Supports AC-009 (D8) and AC-008 (D8, --plugin half)
    # AC-009 (part): "A v1 tracker shall be read as rows with plugin ? and hash -."
    # AC-008 (part): "--uninstall --plugin X keeps the other rows."
    # Given: (a) a v1 tracker ("# setup-opencode.sh tracker v1" followed by logical paths), one of which resolves
    #   through a symlinked plugins/ into a real git repo; (b) a v2 tracker holding rows for commit-guard and
    #   markdown-lsp.
    # When: (a) tracker read --tracker <v1>; (b) tracker read --tracker <v2> --plugin commit-guard.
    # Then: v1 rows are normalized at read time, and --plugin filters by plugin.json id.
    # Verification items:
    #   - (a) each v1 line comes back as "file ? <path> <current realpath> - <repo>"; the symlinked path's realpath
    #     is the repo path, and its repo column is that repo's toplevel; a non-repo path has repo "-"
    #   - (a) the v1 file on disk is byte-identical after the read
    #   - (b) only commit-guard rows are printed, in sorted order
    # Pass criteria: both reads exit 0 and print exactly the expected normalized rows.
    # ROI: 63 (BV:9 x Freq:6 + Legal:0 + Defect:9)
    # @category: core-functionality
    # @dependency: convert.py tracker, tracker.py, git (repo column)
    # @real-dependency: git, filesystem (symlinks)
    # @complexity: medium
    def test_read_normalizes_v1_rows_and_filters_by_plugin(self):
        sandbox = self.new_sandbox()
        dotfiles = sandbox.make_repo(sandbox.fixtures / "dotfiles", files={
            ".config/opencode/plugins/opencode-commit-guard.ts": "// v1 copy\n",
        })
        scope = make_scope(sandbox, plugins=dotfiles / ".config" / "opencode" / "plugins")
        (scope / "commands").mkdir()
        (scope / "commands" / "recipe-implement.md").write_text("v1 copy\n")
        v1_tracker = scope / TRACKER_NAME
        v1_tracker.write_text(text_of(*V1_HEADER_LINES, "plugins/opencode-commit-guard.ts",
                                      "commands/recipe-implement.md"))
        v1_bytes = v1_tracker.read_bytes()
        toplevel = sandbox.git(dotfiles / ".config", "rev-parse", "--show-toplevel").stdout.strip()

        # (a)
        result = self.read(sandbox, v1_tracker)

        self.assertEqual(toplevel, str(dotfiles))
        self.assert_ok(result, stdout=text_of(
            row("file", "?", "commands/recipe-implement.md", f"{scope}/commands/recipe-implement.md", NO_VALUE,
                NO_VALUE),
            row("file", "?", "plugins/opencode-commit-guard.ts",
                f"{dotfiles}/.config/opencode/plugins/opencode-commit-guard.ts", NO_VALUE, toplevel),
        ))
        self.assertEqual(v1_tracker.read_bytes(), v1_bytes)

        # (b): the rows are stored unsorted, so the sorted output comes from read.
        v2_scope = sandbox.root / "project" / ".opencode"
        v2_scope.mkdir()
        v2_tracker = v2_scope / TRACKER_NAME
        cg_file = row("file", "commit-guard", "plugins/opencode-commit-guard.ts",
                      f"{v2_scope}/plugins/opencode-commit-guard.ts", HASH_1, NO_VALUE)
        cg_dir = row("dir", "commit-guard", "llm-agent-workflow/commit-guard",
                     f"{v2_scope}/llm-agent-workflow/commit-guard", HASH_2, NO_VALUE)
        md_config = row("config", "markdown-lsp", "opencode.json#/lsp/rumdl", f"{v2_scope}/opencode.json", HASH_3,
                        NO_VALUE)
        v2_tracker.write_text(text_of(*HEADER_LINES, cg_file, md_config, cg_dir))

        self.assert_ok(self.read(sandbox, v2_tracker, "--plugin", "commit-guard"), stdout=text_of(cg_dir, cg_file))
        self.assert_ok(self.read(sandbox, v2_tracker, "--plugin", "markdown-lsp"), stdout=text_of(md_config))

    # Supports AC-007 and AC-008 (D8); Design Doc F4 "Validation on read" and Security "G0 containment stops a
    #   tampered tracker from deleting files outside the scope"
    # F4 (revision 1.5): "Structural failures -> exit 1 and no write. These are a malformed header, a row with the
    #   wrong column count, a bad type or prefix, a path that is absolute or contains .., or a relative realpath.
    #   Duplicate (type, path) rows in one file -> exit 1 with tracker: duplicate row"
    # Given (one subTest each): a tracker with a malformed header; a row with 5 columns; an absolute path; a path
    #   containing ".."; a relative realpath; two rows with the same (type, path). Revision 1.5 made an absolute
    #   realpath outside the scope realpaths a per-row [KEPT] instead of a refusal; TrackerKeptRowTests covers it.
    # When: tracker read, then tracker merge --add <one valid row>, against each file.
    # Then: every invalid tracker is refused without a write.
    # Verification items:
    #   - both commands exit 1 for every case
    #   - stderr names the problem; the duplicate case prints "tracker: duplicate row"
    #   - the tracker file is byte-identical after both commands
    # Pass criteria: exit 1 and an unchanged file in all six cases.
    # ROI: 40 (BV:10 x Freq:3 + Legal:0 + Defect:10) | security: tampered-tracker defence
    # @category: edge-case
    # @dependency: convert.py tracker, tracker.py
    # @real-dependency: filesystem
    # @complexity: low
    def test_invalid_or_tampered_tracker_exits_1_without_writing(self):
        # Each case: (scope -> tracker text, the stderr line after "ERROR <tracker>:"). Only the named check can
        # fire in each case, and the exact stderr line shows which check did.
        cases = {
            "malformed header": (
                lambda scope, root: text_of(*HEADER_LINES[:3], "# scope global", *HEADER_LINES[4:],
                                            self.valid_row(scope)),
                '4: tracker: malformed header: expected "# <key>: <value>"'),
            "a row with 5 columns": (
                lambda scope, root: text_of(*HEADER_LINES, row("file", "commit-guard", "plugins/x.ts",
                                                               f"{scope}/plugins/x.ts", HASH_1)),
                f"{FIRST_ROW_LINE}: tracker: wrong column count: 5, expected 6"),
            "an absolute path": (
                lambda scope, root: text_of(*HEADER_LINES, row("file", "commit-guard", "/etc/opencode/x.ts",
                                                               f"{scope}/plugins/x.ts", HASH_1, NO_VALUE)),
                f"{FIRST_ROW_LINE}: tracker: absolute path"),
            "a path containing ..": (
                lambda scope, root: text_of(*HEADER_LINES, row("file", "commit-guard", "plugins/../../outside.ts",
                                                               f"{scope}/plugins/x.ts", HASH_1, NO_VALUE)),
                f"{FIRST_ROW_LINE}: tracker: path contains .."),
            "a relative realpath": (
                lambda scope, root: text_of(*HEADER_LINES, row("file", "commit-guard", "plugins/x.ts",
                                                               "plugins/x.ts", HASH_1, NO_VALUE)),
                f"{FIRST_ROW_LINE}: tracker: realpath not absolute"),
            "two rows with the same (type, path)": (
                lambda scope, root: text_of(*HEADER_LINES, self.valid_row(scope, HASH_1),
                                            self.valid_row(scope, HASH_2)),
                f"{FIRST_ROW_LINE + 1}: tracker: duplicate row"),
        }
        for name, (build, stderr_tail) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                scope = make_scope(sandbox)
                tracker = scope / TRACKER_NAME

                # cwd is the scope root, where a relative realpath would resolve inside the scope.
                self.assert_refused(sandbox, scope, build(scope, sandbox.root), f"ERROR {tracker}:{stderr_tail}",
                                    cwd=scope)

    @staticmethod
    def valid_row(scope, digest=HASH_1):
        return row("file", "commit-guard", "plugins/opencode-commit-guard.ts",
                   f"{scope}/plugins/opencode-commit-guard.ts", digest, NO_VALUE)


class TrackerValidationTests(TrackerCliTestCase):
    """Every other read check fails closed the same way: exit 1 from both commands and no write."""

    def test_header_checks_refuse_the_tracker(self):
        row_text = TrackerMergeReadTests.valid_row
        without_skills_md = [line for line in HEADER_LINES if not line.startswith("# skills_md")]
        cases = {
            "an unknown first line": (
                lambda scope: text_of("# setup-opencode.sh tracker v3", *HEADER_LINES[1:], row_text(scope)),
                "1: tracker: malformed header: line 1 is not a tracker v1 or v2 line"),
            "a first line that is a row": (
                lambda scope: text_of(row_text(scope), *HEADER_LINES[1:]),
                "1: tracker: malformed header: line 1 is not a tracker v1 or v2 line"),
            "a v1 path list without the v1 line": (
                lambda scope: text_of("plugins/opencode-commit-guard.ts"),
                "1: tracker: malformed header: line 1 is not a tracker v1 or v2 line"),
            "an empty file": (
                lambda scope: "",
                "1: tracker: malformed header: line 1 is not a tracker v1 or v2 line"),
            "a missing key": (
                lambda scope: text_of(*without_skills_md, row_text(scope)),
                ' tracker: malformed header: missing key "skills_md"'),
            "an unknown key": (
                lambda scope: text_of(*HEADER_LINES[:2], "# colour: blue", *HEADER_LINES[2:], row_text(scope)),
                '3: tracker: malformed header: unknown key "colour"'),
            "a key given twice": (
                lambda scope: text_of(*HEADER_LINES[:4], "# scope: project", *HEADER_LINES[4:], row_text(scope)),
                '5: tracker: malformed header: key "scope" appears twice'),
            "a columns value in another order": (
                lambda scope: text_of(*HEADER_LINES[:-1], "# columns: type plugin path realpath repo hash",
                                      row_text(scope)),
                f'{FIRST_ROW_LINE - 1}: tracker: malformed header: columns must be "type plugin path realpath hash '
                'repo"'),
            "a comment line after the first row": (
                lambda scope: text_of(*HEADER_LINES, row_text(scope), "# scope: project"),
                f"{FIRST_ROW_LINE + 1}: tracker: malformed header: comment line after the first row"),
            "a v1 header with a v2-only key": (
                lambda scope: text_of(*V1_HEADER_LINES, COLUMNS_LINE, "plugins/opencode-commit-guard.ts"),
                '5: tracker: malformed header: unknown key "columns"'),
            "a stored value with a carriage return": (
                lambda scope: text_of(*HEADER_LINES[:3], "# scope: global\rX", *HEADER_LINES[4:], row_text(scope)),
                '4: tracker: malformed header: the value of "scope" has a control character'),
            "a stored value with a Unicode line separator": (
                lambda scope: text_of(*HEADER_LINES[:3], "# scope: global\u2028X", *HEADER_LINES[4:],
                                      row_text(scope)),
                '4: tracker: malformed header: the value of "scope" has a control character'),
            "a stored value with a Unicode paragraph separator": (
                lambda scope: text_of(*HEADER_LINES[:3], "# scope: global\u2029X", *HEADER_LINES[4:],
                                      row_text(scope)),
                '4: tracker: malformed header: the value of "scope" has a control character'),
            "bytes that are not UTF-8": (
                lambda scope: text_of(*HEADER_LINES).encode() + b"file\tcommit-guard\tplugins/\xff.ts\n",
                " tracker: unreadable: not UTF-8"),
        }
        self.assert_cases_refused(cases)

    def test_row_checks_refuse_the_tracker(self):
        first = FIRST_ROW_LINE

        def v2(scope, row_type, path, realpath, digest=HASH_1):
            return text_of(*HEADER_LINES, row(row_type, "commit-guard", path, realpath, digest, NO_VALUE))

        cases = {
            "a path with .. inside a file name": (
                lambda scope, root: v2(scope, "file", "plugins/a..b.ts", f"{scope}/plugins/a..b.ts"),
                f"{first}: tracker: path contains .."),
            "a hash that is too short": (
                lambda scope, root: v2(scope, "file", "plugins/x.ts", f"{scope}/plugins/x.ts", "sha256:abc"),
                f"{first}: tracker: malformed hash (expected sha256:<64 lowercase hex> or -)"),
            "a hash in upper case": (
                lambda scope, root: v2(scope, "file", "plugins/x.ts", f"{scope}/plugins/x.ts", "sha256:" + "A" * 64),
                f"{first}: tracker: malformed hash (expected sha256:<64 lowercase hex> or -)"),
            "a hash with another algorithm": (
                lambda scope, root: v2(scope, "file", "plugins/x.ts", f"{scope}/plugins/x.ts", "md5:" + "1" * 64),
                f"{first}: tracker: malformed hash (expected sha256:<64 lowercase hex> or -)"),
            "a hash with 65 hex digits": (
                lambda scope, root: v2(scope, "file", "plugins/x.ts", f"{scope}/plugins/x.ts", HASH_1 + "1"),
                f"{first}: tracker: malformed hash (expected sha256:<64 lowercase hex> or -)"),
            "a malformed hash on a row whose realpath is outside the roots": (
                lambda scope, root: v2(scope, "file", "plugins/x.ts", f"{root}/elsewhere/x.ts", "sha256:abc"),
                f"{first}: tracker: malformed hash (expected sha256:<64 lowercase hex> or -)"),
            "an absolute path on a row whose realpath is outside the roots": (
                lambda scope, root: v2(scope, "file", "/plugins/x.ts", f"{root}/elsewhere/x.ts"),
                f"{first}: tracker: absolute path"),
            "a disallowed prefix on a row whose realpath is outside the roots": (
                lambda scope, root: v2(scope, "file", "opencode.json", f"{root}/elsewhere/x.ts"),
                f"{first}: tracker: path prefix not allowed"),
            "an unknown type on a row whose realpath is outside the roots": (
                lambda scope, root: v2(scope, "link", "plugins/x.ts", f"{root}/elsewhere/x.ts"),
                f"{first}: tracker: unknown type (expected file, dir or config)"),
            "a path containing .. on a row whose realpath is outside the roots": (
                lambda scope, root: v2(scope, "file", "plugins/../x.ts", f"{root}/elsewhere/x.ts"),
                f"{first}: tracker: path contains .."),
            "a row outside the roots and a row with the same (type, path)": (
                lambda scope, root: text_of(*HEADER_LINES,
                                            row("file", "commit-guard", "plugins/x.ts", f"{root}/elsewhere/x.ts",
                                                HASH_1, NO_VALUE),
                                            row("file", "commit-guard", "plugins/x.ts", f"{scope}/plugins/x.ts",
                                                HASH_2, NO_VALUE)),
                f"{first + 1}: tracker: duplicate row"),
            "a v1 line whose realpath is the scope root": (
                lambda scope, root: text_of(*V1_HEADER_LINES, "commands/to-root"),
                "5: tracker: realpath is a scope root"),
            "an unknown type": (
                lambda scope, root: v2(scope, "link", "plugins/x.ts", f"{scope}/plugins/x.ts"),
                f"{first}: tracker: unknown type (expected file, dir or config)"),
            "an empty path": (
                lambda scope, root: v2(scope, "file", "", f"{scope}/plugins/x.ts"),
                f"{first}: tracker: path not normalized"),
            "a path with a . segment": (
                lambda scope, root: v2(scope, "file", "plugins/./x.ts", f"{scope}/plugins/x.ts"),
                f"{first}: tracker: path not normalized"),
            "a path with a trailing slash": (
                lambda scope, root: v2(scope, "dir", "llm-agent-workflow/commit-guard/",
                                       f"{scope}/llm-agent-workflow/commit-guard"),
                f"{first}: tracker: path not normalized"),
            "a file path in the scope root (the user's opencode.json)": (
                lambda scope, root: v2(scope, "file", "opencode.json", f"{scope}/opencode.json"),
                f"{first}: tracker: path prefix not allowed"),
            "a path naming a writable scope dir itself": (
                lambda scope, root: v2(scope, "dir", "plugins", f"{scope}/plugins/x"),
                f"{first}: tracker: path prefix not allowed"),
            "a path in the native skills dir": (
                lambda scope, root: v2(scope, "dir", "skills/alpha", f"{scope}/plugins/x"),
                f"{first}: tracker: path prefix not allowed"),
            "a config path that is not an opencode.json pointer": (
                lambda scope, root: v2(scope, "config", "plugins/x.json#/lsp/rumdl", f"{scope}/opencode.json"),
                f"{first}: tracker: path prefix not allowed"),
            "a file row with a config pointer path": (
                lambda scope, root: v2(scope, "file", "opencode.json#/lsp/rumdl", f"{scope}/opencode.json"),
                f"{first}: tracker: path prefix not allowed"),
            "a relative realpath": (
                lambda scope, root: v2(scope, "file", "plugins/x.ts", "plugins/x.ts"),
                f"{first}: tracker: realpath not absolute"),
            "a realpath equal to the scope root": (
                lambda scope, root: v2(scope, "dir", "llm-agent-workflow/x", str(scope)),
                f"{first}: tracker: realpath is a scope root"),
            "a realpath equal to a writable scope dir": (
                lambda scope, root: v2(scope, "dir", "llm-agent-workflow/x", f"{scope}/llm-agent-workflow"),
                f"{first}: tracker: realpath is a scope root"),
            "a v1 line that is absolute": (
                lambda scope, root: text_of(*V1_HEADER_LINES, "/etc/passwd"),
                "5: tracker: absolute path"),
            "a v1 line containing ..": (
                lambda scope, root: text_of(*V1_HEADER_LINES, "plugins/../../../etc/passwd"),
                "5: tracker: path contains .."),
            "a v1 line in the scope root (the user's opencode.json)": (
                lambda scope, root: text_of(*V1_HEADER_LINES, "opencode.json"),
                "5: tracker: path prefix not allowed"),
            "a v1 line given twice": (
                lambda scope, root: text_of(*V1_HEADER_LINES, "commands/x.md", "commands/x.md"),
                "6: tracker: duplicate row"),
            "a v2 row and a v1 line for the same (type, path)": (
                lambda scope, root: text_of(*HEADER_LINES, row("file", "commit-guard", "commands/x.md",
                                                               f"{scope}/commands/x.md", HASH_1, NO_VALUE),
                                            "commands/x.md"),
                f"{first + 1}: tracker: duplicate row"),
            "a carriage return in a path": (
                lambda scope, root: v2(scope, "file", "plugins/x\r.ts", f"{scope}/plugins/x.ts"),
                f'{first}: tracker: the "path" column has a control character'),
            "a Unicode line separator in a plugin": (
                lambda scope, root: text_of(*HEADER_LINES, row("file", "commit\u2028guard", "plugins/x.ts",
                                                               f"{scope}/plugins/x.ts", HASH_1, NO_VALUE)),
                f'{first}: tracker: the "plugin" column has a control character'),
            "a NEL in a repo": (
                lambda scope, root: text_of(*HEADER_LINES, row("file", "commit-guard", "plugins/x.ts",
                                                               f"{scope}/plugins/x.ts", HASH_1, "/srv/x\x85y")),
                f'{first}: tracker: the "repo" column has a control character'),
            "a carriage return in a realpath": (
                lambda scope, root: v2(scope, "file", "plugins/x.ts", f"{scope}/plugins/x\r.ts"),
                f'{first}: tracker: the "realpath" column has a control character'),
            "a control character on a row whose realpath is outside the roots": (
                lambda scope, root: text_of(*HEADER_LINES, row("file", "commit-guard", "plugins/x.ts",
                                                               f"{root}/elsewhere/x.ts", HASH_1, "/srv/x\x85y")),
                f'{first}: tracker: the "repo" column has a control character'),
            "a v1 line with a carriage return": (
                lambda scope, root: text_of(*V1_HEADER_LINES, "commands/x\r.md"),
                '5: tracker: the "path" column has a control character'),
        }
        for name, (build, stderr_tail) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                (sandbox.root / "elsewhere").mkdir()
                scope = make_scope(sandbox)
                (scope / "commands").mkdir()
                (scope / "llm-agent-workflow").mkdir()
                (scope / "commands" / "escape").symlink_to(sandbox.root / "elsewhere")
                (scope / "commands" / "to-root").symlink_to(scope)
                tracker = scope / TRACKER_NAME

                # cwd is the scope root, where a relative realpath would resolve inside the scope.
                self.assert_refused(sandbox, scope, build(scope, sandbox.root), f"ERROR {tracker}:{stderr_tail}",
                                    cwd=scope)

    def test_v1_rows_fail_closed_when_git_cannot_name_their_repo(self):
        # IP-9: a v1 row below a .git ancestor needs git for its repo column; without git the tracker is refused.
        sandbox = self.new_sandbox()
        dotfiles = sandbox.make_repo(sandbox.fixtures / "dotfiles")
        scope = make_scope(sandbox, plugins=dotfiles / "plugins")
        no_git = sandbox.env(PATH=sandbox.tools_without("git"))
        tracker = scope / TRACKER_NAME

        self.assert_refused(sandbox, scope, text_of(*V1_HEADER_LINES, "commands/x.md", "plugins/x.ts"),
                            f"ERROR {tracker}:6: tracker: repo unknown: git is not on PATH", env=no_git)

        tracker.write_text(text_of(*V1_HEADER_LINES, "commands/x.md"))
        self.assert_ok(self.read(sandbox, tracker, env=no_git),
                       stdout=text_of(row("file", "?", "commands/x.md", f"{scope}/commands/x.md", NO_VALUE, NO_VALUE)))

        # A v1 row outside the roots is kept without a repo lookup, so it needs no git even inside a repo.
        (scope / "commands").mkdir()
        (scope / "commands" / "escape").symlink_to(dotfiles / "outside-the-roots", target_is_directory=True)
        tracker.write_text(text_of(*V1_HEADER_LINES, "commands/escape/x.md"))
        result = self.read(sandbox, tracker, env=no_git)
        self.assertEqual((result.returncode, result.stdout), (EXIT_OK, ""), result.stderr)
        self.assertEqual(result.stderr, f"WARN {tracker}:5: tracker: realpath outside the scope (kept)\n")

        # A git on PATH that answers nothing (exit 0, no output) below a .git ancestor is refused the same way.
        sandbox.stub("git", "#!/bin/sh\nexit 0\n")
        self.assert_refused(sandbox, scope, text_of(*V1_HEADER_LINES, "plugins/x.ts"),
                            f"ERROR {tracker}:5: tracker: repo unknown: git gave no work tree", env=no_git)

    def test_a_tracker_path_that_is_not_a_readable_file_exits_1(self):
        sandbox = self.new_sandbox()
        scope = make_scope(sandbox)
        tracker = scope / TRACKER_NAME
        tracker.mkdir()

        self.assert_fatal(self.read(sandbox, tracker), f"ERROR {tracker}: tracker: unreadable: Is a directory")
        self.assert_fatal(self.merge(sandbox, tracker, self.empty_rows(sandbox)),
                          f"ERROR {tracker}: tracker: unreadable: Is a directory")
        self.assertEqual(entries(scope), [TRACKER_NAME])
        self.assertEqual(entries(tracker), [])

    def assert_cases_refused(self, cases):
        for name, (build, stderr_tail) in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                scope = make_scope(sandbox)
                tracker = scope / TRACKER_NAME

                self.assert_refused(sandbox, scope, build(scope), f"ERROR {tracker}:{stderr_tail}")


class TrackerKeptRowTests(TrackerCliTestCase):
    """F4 revision 1.5: an absolute realpath outside the current roots is a per-row [KEPT], never a read-out row.

    Such a row stays in the file verbatim and is left out of `tracker read` stdout, so the installer can never turn it
    into a delete. read warns once per kept row and exits 0; structural problems anywhere still exit 1.
    """

    KEPT_WARNING = "tracker: realpath outside the scope (kept)"

    def assert_warned(self, result, stdout, stderr):
        self.assertEqual((result.returncode, result.stdout, result.stderr), (EXIT_OK, stdout, stderr))

    def retargeted_scope(self, sandbox):
        """<scope>/plugins now points at dotfiles-new; rows recorded under dotfiles-old lie outside the roots."""
        old = sandbox.fixtures / "dotfiles-old" / "plugins"
        old.mkdir(parents=True)
        new = sandbox.fixtures / "dotfiles-new" / "plugins"
        return make_scope(sandbox, plugins=new), old, new

    # Supports AC-008 and AC-009 (D8); Design Doc F4 revision 1.5 "An absolute realpath outside the current
    #   containment roots is a per-row [KEPT]"
    # Given: a tracker recorded while plugins/ pointed at dotfiles-old, holding two stale rows (lines 13 and 15);
    #   plugins/ now points at dotfiles-new.
    # When: (1) tracker read, with and without --plugin; (2) merge --add <one new row> --header scope=project;
    #   (3) merge --remove <the first stale (type, path)>.
    # Then: stale rows are never printed, each gets one WARN naming its line, merge keeps them byte-identical, and
    #   only --remove drops one.
    def test_a_retargeted_scope_symlink_keeps_the_stale_row_out_of_read(self):
        sandbox = self.new_sandbox()
        scope, old, new = self.retargeted_scope(sandbox)
        tracker = scope / TRACKER_NAME
        config = row("config", "markdown-lsp", "opencode.json#/lsp/rumdl", f"{scope}/opencode.json", HASH_3,
                     NO_VALUE)
        stale = row("file", "commit-guard", "plugins/opencode-commit-guard.ts", f"{old}/opencode-commit-guard.ts",
                    HASH_1, NO_VALUE)
        live = row("file", "markdown-lsp", "plugins/opencode-markdown-lsp.ts", f"{new}/opencode-markdown-lsp.ts",
                   HASH_2, NO_VALUE)
        stale_2 = row("file", "token-saver", "plugins/opencode-token-saver.ts", f"{old}/opencode-token-saver.ts",
                      HASH_5, NO_VALUE)
        added = row("file", "dev", "agents/task-executor.md", f"{scope}/agents/task-executor.md", HASH_4, NO_VALUE)
        tracker.write_text(text_of(*HEADER_LINES, config, stale, live, stale_2))
        warnings = (f"WARN {tracker}:{FIRST_ROW_LINE + 1}: {self.KEPT_WARNING}\n"
                    f"WARN {tracker}:{FIRST_ROW_LINE + 3}: {self.KEPT_WARNING}\n")
        header = list(HEADER_LINES)
        header[3] = "# scope: project"

        # (1)
        self.assert_warned(self.read(sandbox, tracker), text_of(config, live), warnings)
        self.assert_warned(self.read(sandbox, tracker, "--plugin", "commit-guard"), "", warnings)

        # (2)
        self.assert_ok(self.merge(sandbox, tracker, write_rows(sandbox.fixtures / "add.rows", added),
                                  "--header", "scope=project"))
        self.assertEqual(tracker.read_text(), text_of(*header, config, stale, added, live, stale_2))
        self.assertEqual(entries(scope), [TRACKER_NAME, "plugins"])

        # (3): the remove row's other columns do not matter.
        remove = write_rows(sandbox.fixtures / "remove.rows",
                            row("file", "?", "plugins/opencode-commit-guard.ts", "-", "-", "-"))
        self.assert_ok(self.merge(sandbox, tracker, self.empty_rows(sandbox), "--remove", remove))
        self.assertEqual(tracker.read_text(), text_of(*header, config, added, live, stale_2))
        self.assert_warned(self.read(sandbox, tracker), text_of(config, added, live),
                           f"WARN {tracker}:{FIRST_ROW_LINE + 3}: {self.KEPT_WARNING}\n")

    def test_a_kept_row_keeps_the_file_until_an_added_row_with_its_key_replaces_it(self):
        sandbox = self.new_sandbox()
        scope, old, new = self.retargeted_scope(sandbox)
        tracker = scope / TRACKER_NAME
        stale = row("file", "commit-guard", "plugins/opencode-commit-guard.ts", f"{old}/opencode-commit-guard.ts",
                    HASH_1, NO_VALUE)
        live = row("file", "markdown-lsp", "plugins/opencode-markdown-lsp.ts", f"{new}/opencode-markdown-lsp.ts",
                   HASH_2, NO_VALUE)
        reinstalled = row("file", "commit-guard", "plugins/opencode-commit-guard.ts",
                          f"{new}/opencode-commit-guard.ts", HASH_5, NO_VALUE)
        tracker.write_text(text_of(*HEADER_LINES, stale, live))

        # Removing the only in-scope row leaves the kept row, so the file stays.
        self.assert_ok(self.merge(sandbox, tracker, self.empty_rows(sandbox), "--remove",
                                  write_rows(sandbox.fixtures / "remove-live.rows", live)))
        self.assertEqual(tracker.read_text(), text_of(*HEADER_LINES, stale))
        self.assert_warned(self.read(sandbox, tracker), "", f"WARN {tracker}:{FIRST_ROW_LINE}: {self.KEPT_WARNING}\n")

        # Re-installing commit-guard under the new target replaces the kept row by (type, path).
        self.assert_ok(self.merge(sandbox, tracker, write_rows(sandbox.fixtures / "add.rows", reinstalled)))
        self.assertEqual(tracker.read_text(), text_of(*HEADER_LINES, reinstalled))
        self.assert_ok(self.read(sandbox, tracker), stdout=text_of(reinstalled))

    def test_every_kind_of_realpath_outside_the_roots_is_kept(self):
        # Each case: (scope, root -> (tracker text, the kept line, its line number, the expected read stdout)).
        def valid(scope):
            return row("file", "commit-guard", "commands/valid.md", f"{scope}/commands/valid.md", HASH_2, NO_VALUE)

        def v2_case(scope, kept_line, header_lines=HEADER_LINES):
            return text_of(*header_lines, valid(scope), kept_line), kept_line, FIRST_ROW_LINE + 1, text_of(valid(scope))

        def v1_case(scope, kept_line):
            normalized = row("file", "?", "commands/valid.md", f"{scope}/commands/valid.md", NO_VALUE, NO_VALUE)
            return text_of(*V1_HEADER_LINES, "commands/valid.md", kept_line), kept_line, 6, text_of(normalized)

        widened_header = HEADER_LINES[:4] + ["# scope_root: /"] + HEADER_LINES[5:]
        cases = {
            "a v2 realpath that leaves the scope through ..": lambda scope, root: v2_case(
                scope, row("file", "commit-guard", "commands/x.md", f"{scope}/commands/../../../elsewhere/x.md",
                           HASH_1, NO_VALUE)),
            "a v2 realpath that leaves the scope through a symlink": lambda scope, root: v2_case(
                scope, row("file", "commit-guard", "commands/escape/x.md", f"{scope}/commands/escape/x.md", HASH_1,
                           NO_VALUE)),
            "a v2 realpath outside, with a header scope_root that would widen the scope": lambda scope, root: v2_case(
                scope, row("file", "commit-guard", "commands/x.md", f"{root}/elsewhere/x.md", HASH_1, NO_VALUE),
                widened_header),
            "a v1 line that leaves the scope through a symlink": lambda scope, root: v1_case(
                scope, "commands/escape/x.md"),
        }
        for name, build in cases.items():
            with self.subTest(name):
                sandbox = self.new_sandbox()
                (sandbox.root / "elsewhere").mkdir()
                scope = make_scope(sandbox)
                (scope / "commands").mkdir()
                (scope / "commands" / "escape").symlink_to(sandbox.root / "elsewhere")
                tracker = scope / TRACKER_NAME
                text, kept_line, kept_lineno, stdout = build(scope, sandbox.root)
                tracker.write_text(text)
                added = row("file", "dev", "agents/task-executor.md", f"{scope}/agents/task-executor.md", HASH_4,
                            NO_VALUE)

                self.assert_warned(self.read(sandbox, tracker), stdout,
                                   f"WARN {tracker}:{kept_lineno}: {self.KEPT_WARNING}\n")
                self.assert_ok(self.merge(sandbox, tracker, write_rows(sandbox.fixtures / "add.rows", added)))
                merged_lines = tracker.read_text().split("\n")
                self.assertEqual(merged_lines[0], MAGIC_V2)
                self.assertEqual(merged_lines.count(kept_line), 1)
                self.assert_warned(self.read(sandbox, tracker), stdout + text_of(added),
                                   f"WARN {tracker}:{merged_lines.index(kept_line) + 1}: {self.KEPT_WARNING}\n")

    def test_a_structural_error_elsewhere_still_refuses_a_file_with_a_kept_row(self):
        sandbox = self.new_sandbox()
        scope, old, _new = self.retargeted_scope(sandbox)
        tracker = scope / TRACKER_NAME
        stale = row("file", "commit-guard", "plugins/opencode-commit-guard.ts", f"{old}/opencode-commit-guard.ts",
                    HASH_1, NO_VALUE)
        five_columns = row("file", "markdown-lsp", "plugins/x.ts", f"{scope}/plugins/x.ts", HASH_2)
        absolute = row("file", "markdown-lsp", "/plugins/x.ts", f"{scope}/plugins/x.ts", HASH_2, NO_VALUE)
        cases = {
            "a 5-column row after the kept row": (
                text_of(*HEADER_LINES, stale, five_columns),
                f"{FIRST_ROW_LINE + 1}: tracker: wrong column count: 5, expected 6"),
            "an absolute path before the kept row": (
                text_of(*HEADER_LINES, absolute, stale),
                f"{FIRST_ROW_LINE}: tracker: absolute path"),
            "a malformed header above the kept row": (
                text_of(*HEADER_LINES[:3], "# scope global", *HEADER_LINES[4:], stale),
                '4: tracker: malformed header: expected "# <key>: <value>"'),
        }
        for name, (text, stderr_tail) in cases.items():
            with self.subTest(name):
                self.assert_refused(sandbox, scope, text, f"ERROR {tracker}:{stderr_tail}")


class TrackerMergeInputTests(TrackerCliTestCase):
    """merge validates its ROWS files and --header arguments before it writes anything."""

    def test_invalid_rows_files_and_header_arguments_exit_1_without_writing(self):
        # Each case: (sandbox, scope -> extra merge args, the --add rows, the expected stderr line).
        def rows_file(sandbox, name, *rows):
            return write_rows(sandbox.fixtures / name, *rows)

        def good(scope, path="commands/new.md", row_type="file"):
            return row(row_type, "commit-guard", path, f"{scope}/{path}", HASH_2, NO_VALUE)

        cases = {
            "an --add row with 5 columns": lambda sb, scope: (
                [], rows_file(sb, "add.rows", row("file", "commit-guard", "commands/new.md", "x", HASH_2)),
                "{add}:1: tracker: wrong column count: 5, expected 6"),
            "an --add row with an unknown type": lambda sb, scope: (
                [], rows_file(sb, "add.rows", good(scope, row_type="link")),
                "{add}:1: tracker: unknown type (expected file, dir or config)"),
            "an --add row with an absolute path": lambda sb, scope: (
                [], rows_file(sb, "add.rows", row("file", "commit-guard", "/commands/new.md",
                                                  f"{scope}/commands/new.md", HASH_2, NO_VALUE)),
                "{add}:1: tracker: absolute path"),
            "an --add row with a realpath outside the scope": lambda sb, scope: (
                [], rows_file(sb, "add.rows", row("file", "commit-guard", "commands/new.md",
                                                  f"{sb.root}/elsewhere/new.md", HASH_2, NO_VALUE)),
                "{add}:1: tracker: realpath outside the scope"),
            "an --add row with a relative realpath": lambda sb, scope: (
                [], rows_file(sb, "add.rows", row("file", "commit-guard", "commands/new.md", "commands/new.md", HASH_2,
                                                  NO_VALUE)),
                "{add}:1: tracker: realpath not absolute"),
            "an --add row with a malformed hash": lambda sb, scope: (
                [], rows_file(sb, "add.rows", row("file", "commit-guard", "commands/new.md",
                                                  f"{scope}/commands/new.md", "sha256:" + "g" * 64, NO_VALUE)),
                "{add}:1: tracker: malformed hash (expected sha256:<64 lowercase hex> or -)"),
            "an --add file with the same (type, path) twice": lambda sb, scope: (
                [], rows_file(sb, "add.rows", good(scope), good(scope)),
                "{add}:2: tracker: duplicate row"),
            "an --add row with a carriage return in a path": lambda sb, scope: (
                [], rows_file(sb, "add.rows", row("file", "commit-guard", "commands/new\r.md",
                                                  f"{scope}/commands/new.md", HASH_2, NO_VALUE)),
                '{add}:1: tracker: the "path" column has a control character'),
            "an --add row with a Unicode line separator in a plugin": lambda sb, scope: (
                [], rows_file(sb, "add.rows", row("file", "commit\u2028guard", "commands/new.md",
                                                  f"{scope}/commands/new.md", HASH_2, NO_VALUE)),
                '{add}:1: tracker: the "plugin" column has a control character'),
            "an --add row with a Unicode paragraph separator in a realpath": lambda sb, scope: (
                [], rows_file(sb, "add.rows", row("file", "commit-guard", "commands/new.md",
                                                  f"{scope}/commands/new\u2029.md", HASH_2, NO_VALUE)),
                '{add}:1: tracker: the "realpath" column has a control character'),
            "an --add row with a NEL in a repo": lambda sb, scope: (
                [], rows_file(sb, "add.rows", row("file", "commit-guard", "commands/new.md",
                                                  f"{scope}/commands/new.md", HASH_2, "/srv/x\x85y")),
                '{add}:1: tracker: the "repo" column has a control character'),
            "a --remove row with a carriage return in a path": lambda sb, scope: (
                ["--remove", rows_file(sb, "remove.rows", row("file", "commit-guard", "commands/new\r.md", "-", "-",
                                                              "-"))],
                rows_file(sb, "add.rows"),
                '{remove}:1: tracker: the "path" column has a control character'),
            "an --add file that does not exist": lambda sb, scope: (
                [], sb.fixtures / "missing.rows",
                "{add}: tracker: unreadable: No such file or directory"),
            "a --remove row with 2 columns": lambda sb, scope: (
                ["--remove", rows_file(sb, "remove.rows", row("file", "commands/new.md"))], rows_file(sb, "add.rows"),
                "{remove}:1: tracker: wrong column count: 2, expected 6"),
            "a --remove row with an unknown type": lambda sb, scope: (
                ["--remove", rows_file(sb, "remove.rows", good(scope, row_type="link"))], rows_file(sb, "add.rows"),
                "{remove}:1: tracker: unknown type (expected file, dir or config)"),
            "a --remove row with an absolute path": lambda sb, scope: (
                ["--remove", rows_file(sb, "remove.rows", row("file", "commit-guard", "/etc/passwd", "-", "-", "-"))],
                rows_file(sb, "add.rows"),
                "{remove}:1: tracker: absolute path"),
            "a --remove row with a path containing ..": lambda sb, scope: (
                ["--remove", rows_file(sb, "remove.rows", row("file", "commit-guard", "commands/../../x", "-", "-",
                                                              "-"))],
                rows_file(sb, "add.rows"),
                "{remove}:1: tracker: path contains .."),
            "a row in both --add and --remove": lambda sb, scope: (
                ["--remove", rows_file(sb, "remove.rows", good(scope))], rows_file(sb, "add.rows", good(scope)),
                "tracker merge: tracker: a (type, path) is in both --add and --remove"),
            "an unknown header key": lambda sb, scope: (
                ["--header", "colour=blue"], rows_file(sb, "add.rows", good(scope)),
                '--header: tracker: not a settable header key: "colour"'),
            "the fixed columns header key": lambda sb, scope: (
                ["--header", "columns=type path"], rows_file(sb, "add.rows", good(scope)),
                '--header: tracker: not a settable header key: "columns"'),
            "a header argument without =": lambda sb, scope: (
                ["--header", "scope"], rows_file(sb, "add.rows", good(scope)),
                "--header: tracker: expected KEY=VALUE"),
            "a header value with a newline that would inject a row": lambda sb, scope: (
                ["--header", f"scope=global\n{good(scope, path='commands/injected.md')}"],
                rows_file(sb, "add.rows", good(scope)),
                '--header: tracker: the value of "scope" has a control character'),
            "a header value with a carriage return": lambda sb, scope: (
                ["--header", "scope=global\rX"], rows_file(sb, "add.rows", good(scope)),
                '--header: tracker: the value of "scope" has a control character'),
            "a header value with a Unicode line separator": lambda sb, scope: (
                ["--header", "scope=global\u2028X"], rows_file(sb, "add.rows", good(scope)),
                '--header: tracker: the value of "scope" has a control character'),
            "a header value with a Unicode paragraph separator": lambda sb, scope: (
                ["--header", "scope=global\u2029X"], rows_file(sb, "add.rows", good(scope)),
                '--header: tracker: the value of "scope" has a control character'),
            "a header key given twice": lambda sb, scope: (
                ["--header", "scope=global", "--header", "scope=project"], rows_file(sb, "add.rows", good(scope)),
                '--header: tracker: "scope" is given twice'),
        }
        for existing in (True, False):
            for name, build in cases.items():
                with self.subTest(name, existing_tracker=existing):
                    sandbox = self.new_sandbox()
                    (sandbox.root / "elsewhere").mkdir()
                    scope = make_scope(sandbox)
                    tracker = scope / TRACKER_NAME
                    if existing:
                        tracker.write_text(text_of(*HEADER_LINES, TrackerMergeReadTests.valid_row(scope)))
                    before = (tracker.read_bytes() if existing else None, entries(scope))
                    args, add, stderr_template = build(sandbox, scope)
                    remove = args[1] if args[:1] == ["--remove"] else None

                    result = self.merge(sandbox, tracker, add, *args)

                    self.assert_fatal(result, "ERROR " + stderr_template.format(add=add, remove=remove))
                    self.assertEqual((tracker.read_bytes() if existing else None, entries(scope)), before)
                    self.assertEqual(os.path.lexists(tracker), existing)


class TrackerMergeTests(TrackerCliTestCase):
    """Unit-level merge behaviour through the CLI (header round trip, no-op removal, v1 upgrade, lifetime)."""

    def test_header_keys_round_trip_and_only_given_keys_are_replaced(self):
        sandbox = self.new_sandbox()
        scope = make_scope(sandbox)
        tracker = scope / TRACKER_NAME
        add = write_rows(sandbox.fixtures / "add.rows", TrackerMergeReadTests.valid_row(scope))
        repeated_header_args = [arg for pair in HEADER_ARGS for arg in ("--header", pair)]

        self.assert_ok(self.merge(sandbox, tracker, add, *repeated_header_args))
        self.assertEqual(tracker.read_text(), text_of(*HEADER_LINES, TrackerMergeReadTests.valid_row(scope)))

        self.assert_ok(self.merge(sandbox, tracker, self.empty_rows(sandbox), "--header", "scope=project",
                                  "allowed_repos=/srv/dotfiles", "mcp_aliases="))
        expected = list(HEADER_LINES)
        expected[3] = "# scope: project"
        expected[7] = "# mcp_aliases:"
        expected[8] = "# allowed_repos: /srv/dotfiles"
        self.assertEqual(tracker.read_text(), text_of(*expected, TrackerMergeReadTests.valid_row(scope)))

    def test_a_new_tracker_without_header_arguments_writes_every_key_empty(self):
        sandbox = self.new_sandbox()
        scope = make_scope(sandbox)
        tracker = scope / TRACKER_NAME
        add = write_rows(sandbox.fixtures / "add.rows", TrackerMergeReadTests.valid_row(scope))

        self.assert_ok(self.merge(sandbox, tracker, add))

        self.assertEqual(tracker.read_text(), text_of(
            MAGIC_V2, "# installed_at:", "# repo_root:", "# scope:", "# scope_root:", "# payload_root:",
            "# recipe_policy:", "# mcp_aliases:", "# allowed_repos:", "# skills_md:", COLUMNS_LINE,
            TrackerMergeReadTests.valid_row(scope)))

    def test_remove_of_a_missing_row_is_a_no_op(self):
        sandbox = self.new_sandbox()
        scope = make_scope(sandbox)
        tracker = scope / TRACKER_NAME
        tracker.write_text(text_of(*HEADER_LINES, TrackerMergeReadTests.valid_row(scope)))
        before = tracker.read_bytes()
        remove = write_rows(sandbox.fixtures / "remove.rows",
                            row("file", "commit-guard", "plugins/never-installed.ts", "-", "-", "-"),
                            row("dir", "commit-guard", "plugins/opencode-commit-guard.ts", "-", "-", "-"))

        self.assert_ok(self.merge(sandbox, tracker, self.empty_rows(sandbox), "--remove", remove))

        self.assertEqual(tracker.read_bytes(), before)
        self.assertEqual(entries(scope), [TRACKER_NAME])

    def test_remove_matches_by_type_and_path_and_ignores_the_other_columns(self):
        sandbox = self.new_sandbox()
        scope = make_scope(sandbox)
        tracker = scope / TRACKER_NAME
        kept = row("dir", "commit-guard", "llm-agent-workflow/commit-guard", f"{scope}/llm-agent-workflow/commit-guard",
                   HASH_2, NO_VALUE)
        tracker.write_text(text_of(*HEADER_LINES, kept, TrackerMergeReadTests.valid_row(scope)))
        remove = write_rows(sandbox.fixtures / "remove.rows",
                            row("file", "another-plugin", "plugins/opencode-commit-guard.ts", "/elsewhere/x", "junk",
                                "junk"))

        self.assert_ok(self.merge(sandbox, tracker, self.empty_rows(sandbox), "--remove", remove))

        self.assertEqual(tracker.read_text(), text_of(*HEADER_LINES, kept))

    def test_merge_against_a_v1_tracker_writes_v2_with_the_normalized_rows(self):
        # 1.3c's upgrade path: v1 rows become "file ? <path> <realpath> - <repo>" v2 rows, a row with the same
        # (type, path) replaces its v1 row (SAME-v1), and the v1 header values are kept.
        sandbox = self.new_sandbox()
        dotfiles = sandbox.make_repo(sandbox.fixtures / "dotfiles")
        scope = make_scope(sandbox, plugins=dotfiles / "plugins")
        tracker = scope / TRACKER_NAME
        tracker.write_text(text_of(*V1_HEADER_LINES, "plugins/opencode-commit-guard.ts",
                                   "commands/recipe-implement.md", "plugins/opencode-token-saver.ts"))
        upgraded = row("file", "commit-guard", "plugins/opencode-commit-guard.ts",
                       f"{dotfiles}/plugins/opencode-commit-guard.ts", HASH_1, str(dotfiles))
        added = row("file", "dev", "agents/task-executor.md", f"{scope}/agents/task-executor.md", HASH_2, NO_VALUE)
        add = write_rows(sandbox.fixtures / "add.rows", added, upgraded)

        self.assert_ok(self.merge(sandbox, tracker, add, "--header", f"scope_root={scope}"))

        self.assertEqual(tracker.read_text(), text_of(
            MAGIC_V2, *V1_HEADER_LINES[1:], f"# scope_root: {scope}", "# payload_root:", "# recipe_policy:",
            "# mcp_aliases:", "# allowed_repos:", "# skills_md:", COLUMNS_LINE,
            row("file", "?", "commands/recipe-implement.md", f"{scope}/commands/recipe-implement.md", NO_VALUE,
                NO_VALUE),
            row("file", "?", "plugins/opencode-token-saver.ts", f"{dotfiles}/plugins/opencode-token-saver.ts",
                NO_VALUE, str(dotfiles)),
            upgraded,
            added,
        ))
        self.assertEqual(entries(scope), [TRACKER_NAME, "plugins"])

    def test_removing_every_row_of_a_v1_tracker_deletes_it(self):
        sandbox = self.new_sandbox()
        scope = make_scope(sandbox)
        tracker = scope / TRACKER_NAME
        tracker.write_text(text_of(*V1_HEADER_LINES, "commands/recipe-implement.md"))
        remove = write_rows(sandbox.fixtures / "remove.rows",
                            row("file", "?", "commands/recipe-implement.md", "-", "-", "-"))

        self.assert_ok(self.merge(sandbox, tracker, self.empty_rows(sandbox), "--remove", remove))

        self.assertFalse(os.path.lexists(tracker))

    def test_a_missing_tracker_reads_as_no_rows_and_an_empty_merge_creates_no_file(self):
        sandbox = self.new_sandbox()
        scope = make_scope(sandbox)
        tracker = scope / TRACKER_NAME

        self.assert_ok(self.read(sandbox, tracker))
        self.assert_ok(self.merge(sandbox, tracker, self.empty_rows(sandbox), "--header", "scope=global"))

        self.assertEqual(entries(scope), [])

    def test_tracker_usage_errors_exit_2(self):
        sandbox = self.new_sandbox()
        cases = {
            "no tracker subcommand": ("tracker",),
            "an unknown tracker subcommand": ("tracker", "frobnicate"),
            "read without --tracker": ("tracker", "read"),
            "merge without --add": ("tracker", "merge", "--tracker", sandbox.home / TRACKER_NAME),
            "merge without --tracker": ("tracker", "merge", "--add", sandbox.fixtures / "empty.rows"),
        }
        for name, args in cases.items():
            with self.subTest(name):
                result = run_convert(*args, sandbox=sandbox)

                self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage:", result.stderr)


class TrackerModuleTests(unittest.TestCase):
    """In-process unit cases for tracker.py: the pure merge, and the atomic write under injected failures."""

    def setUp(self):
        self.tracker = import_tracker()
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.scope_dir = make_scope(self.sandbox)
        self.path = self.scope_dir / TRACKER_NAME
        self.scope = self.tracker.Scope.of_tracker(self.path, WRITABLE_SCOPE_DIRS)

    def make_row(self, row_type, path, digest, plugin="commit-guard"):
        return self.tracker.Row(row_type, plugin, path, f"{self.scope_dir}/{path}", digest, NO_VALUE)

    def test_a_row_with_the_same_type_and_path_replaces_the_stored_row(self):
        # SAME-tracked hash refresh: one row per (type, path), carrying the new hash.
        stored = self.make_row("file", "plugins/opencode-commit-guard.ts", HASH_1)
        other = self.make_row("dir", "llm-agent-workflow/commit-guard", HASH_2)
        state = self.tracker.Tracker(rows={stored.key: stored, other.key: other})
        refreshed = self.make_row("file", "plugins/opencode-commit-guard.ts", HASH_3)

        merged = self.tracker.merge(state, [refreshed], set(), {})

        self.assertEqual(merged.sorted_rows(), [other, refreshed])

    def test_rows_are_keyed_by_type_and_path_together(self):
        as_file = self.make_row("file", "llm-agent-workflow/commit-guard", HASH_1)
        as_dir = self.make_row("dir", "llm-agent-workflow/commit-guard", HASH_2)

        merged = self.tracker.merge(self.tracker.Tracker(rows={as_file.key: as_file}), [as_dir], set(), {})

        self.assertEqual(merged.sorted_rows(), [as_dir, as_file])

    def test_a_failed_write_leaves_the_old_tracker_and_no_temp_file(self):
        self.path.write_text(text_of(*HEADER_LINES, TrackerMergeReadTests.valid_row(self.scope_dir)))
        before = self.path.read_bytes()
        state = self.tracker.load(self.path, self.scope)
        added = self.make_row("file", "commands/new.md", HASH_2)
        merged = self.tracker.merge(state, [added], set(), {})
        io_error = OSError(errno.EIO, "Input/output error")
        for failing in ("fsync", "replace"):
            with self.subTest(failing):
                with mock.patch.object(self.tracker.os, failing, side_effect=io_error):
                    with self.assertRaises(self.tracker.mapping.ConvertError) as raised:
                        self.tracker.save(self.path, merged)

                self.assertEqual(str(raised.exception),
                                 f"ERROR {self.path}: tracker: cannot write: Input/output error")
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(entries(self.scope_dir), [TRACKER_NAME])


if __name__ == "__main__":
    unittest.main()
