#!/usr/bin/env python3
# Config snippet merge Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.4)
# Generated: 2026-09-24 | Budget Used: 3/3 integration, 0/2 E2E (F5 E2E lives in test_setup_opencode.py)
"""Integration tests for `convert.py merge` (skeleton).

Each test runs the real CLI in a subprocess against temp config files:

  python3 scripts/opencode/convert.py merge --config FILE --snippet FILE [--dry-run]

stdout contract: "ADDED|SAME|CONFLICT <pointer> sha256:<hex>" per leaf; exit 0, or 1 on error. This is the level
AC-012, AC-013 and AC-050 are pushed down to. Unit cases for merge.py are added here by the implementer (Design Doc
Suites row "test_merge.py").

  python3 -m unittest discover -s scripts/opencode/tests
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONVERT = REPO_ROOT / "scripts" / "opencode" / "convert.py"

# Harness (to implement): one tempfile.TemporaryDirectory() per test holding a config file and a snippet file (the
# Design Doc "Snippets (opencode.json)" block, or a subset of it). HOME points at the tempdir. No mocks.


class MergeTests(unittest.TestCase):
    # Supports AC-012 (D10) and AC-050 (D13)
    # AC-012 (part): "--merge into a plain opencode.json adds lsp.rumdl pointing at an executable payload script,
    #   preserves the other keys and records a config row."
    # AC-050 (part): "--merge appends .opencode/llm-agent-workflow/skills to .opencode/opencode.json skills.paths"
    # Given: a plain opencode.json with a user key ("theme") and skills.paths ["user/skills"]; a snippet holding
    #   skills.paths, lsp.rumdl, lsp.ruby-lsp, mcp.mempalace and agent.build.permission.skill.
    # When: (1) merge; (2) merge again with the same snippet.
    # Then: leaves are added once, reported SAME afterwards, and the rest of the file is kept.
    # Verification items:
    #   - (1) "ADDED /lsp/rumdl", "ADDED /lsp/ruby-lsp", "ADDED /mcp/mempalace", "ADDED /skills/paths" and "ADDED
    #     /agent/build/permission/skill/recipe-*", each followed by a sha256
    #   - (1) /lsp/* and /mcp/* land as whole objects (atomic), equal to the snippet's objects
    #   - (1) skills.paths == ["user/skills", <our entry>]: appended, never replaced
    #   - (1) home paths are written as "{env:HOME}/..." and the skills.paths entry keeps "~/"
    #   - (1) "theme" is unchanged and the file is valid JSON
    #   - (2) every leaf reports SAME and the file is byte-identical to the result of (1)
    # Pass criteria: both runs exit 0, and the leaf lines and file contents match the expected values.
    # ROI: 90 (BV:9 x Freq:9 + Legal:0 + Defect:9)
    # @category: core-functionality
    # @dependency: convert.py merge, merge.py
    # @real-dependency: filesystem
    # @complexity: medium
    def test_merge_adds_leaves_once_and_appends_skills_paths(self):
        self.skipTest("skeleton: AC-012, AC-050")

    # Supports AC-012 (D10, CONFLICT half)
    # AC-012 (part): "A differing lsp.rumdl gives CONFLICT, untouched."
    # Given (one subTest each): (a) an opencode.json whose lsp.rumdl differs from the snippet; (b) an opencode.json
    #   that is not valid JSON; (c) a fresh opencode.json, merged with --dry-run.
    # When: merge with the Design Doc snippet.
    # Then: nothing the user owns is ever overwritten, and nothing is written on error or in dry-run.
    # Verification items:
    #   - (a) "CONFLICT /lsp/rumdl sha256:<hex>"; the lsp.rumdl value is byte-identical; the line names the pointer
    #     but not the existing value (Design Doc "Logging: CONFLICT lines name the pointer only"); the other leaves
    #     are ADDED
    #   - (b) exit 1; the file is byte-identical
    #   - (c) the leaf lines print; the file is byte-identical
    # Pass criteria: (a) and (c) exit 0, (b) exits 1, and all three files keep their original hash at the checked
    #   leaves.
    # ROI: 72 (BV:9 x Freq:7 + Legal:0 + Defect:9)
    # @category: edge-case
    # @dependency: convert.py merge, merge.py
    # @real-dependency: filesystem
    # @complexity: medium
    def test_differing_leaf_is_conflict_and_invalid_json_is_never_written(self):
        self.skipTest("skeleton: AC-012")

    # Supports AC-013 (D10)
    # AC-013: "A .jsonc target shall stay byte-identical; the snippet is printed with no config rows."
    # F5 table: "both files present | none | print + WARN"
    # Given (one subTest each): (a) a scope root with only opencode.jsonc (comments + a user mcp entry); (b) a scope
    #   root with both opencode.json and opencode.jsonc.
    # When: merge against the scope's config.
    # Then: the snippet is printed instead of merged.
    # Verification items:
    #   - (a) the snippet prints on stdout; opencode.jsonc is byte-identical; no opencode.json is created
    #   - (b) the snippet prints, a WARN names both files, and both files are byte-identical
    #   - no ADDED line is reported in either case, so the installer records no config rows
    # Pass criteria: every file keeps its original hash, and the printed snippet parses as JSON.
    # ROI: 79 (BV:8 x Freq:9 + Legal:0 + Defect:7) | the live global config is jsonc
    # @category: core-functionality
    # @dependency: convert.py merge, merge.py
    # @real-dependency: filesystem
    # @complexity: low
    def test_jsonc_or_dual_config_prints_the_snippet_and_keeps_files(self):
        self.skipTest("skeleton: AC-013")


if __name__ == "__main__":
    unittest.main()
