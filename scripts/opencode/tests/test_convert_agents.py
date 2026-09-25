#!/usr/bin/env python3
# Agent conversion Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.4)
# Generated: 2026-09-24 | Budget Used: 3/3 integration (F3 agent transform 2/3, F3 rewrite rules 1/3), 0/2 E2E
"""Integration tests for agent conversion through `convert.py plan` (skeleton).

Each test runs the real planner in a subprocess against this repo's sources (or a small fixture repo) and inspects
the staged output:

  python3 scripts/opencode/convert.py plan --repo DIR --scope-root DIR --scope global|project --stage DIR [...]

Golden files live in scripts/opencode/tests/fixtures/expected/ and are compared with difflib.unified_diff. Golden
updates are reviewed by the user (Design Doc "Output Comparison").

  python3 -m unittest discover -s scripts/opencode/tests
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONVERT = REPO_ROOT / "scripts" / "opencode" / "convert.py"
EXPECTED = Path(__file__).resolve().parent / "fixtures" / "expected"

# Harness (to implement): one tempfile.TemporaryDirectory() per test holding scope/ and stage/. HOME points at the
# tempdir; XDG_DATA_HOME and XDG_CONFIG_HOME are unset so placeholders render their defaults. No mocks: plan reads
# real sources and mapping.json. The 13 per-row rewrite positives are unit tests in test_rewrite.py (Design Doc
# Suites table), not here.


class AgentConversionTests(unittest.TestCase):
    # AC-015 (V1, [unit], integration-level: the whole agent pipeline through plan)
    # AC-015: "task-executor.md shall convert to its golden file: dropped keys; mode: subagent; '*': deny first; the
    #   tool allows; todowrite; 4 skills; the preamble; a single-quoted description."
    # Given: the real plugin-dev/agents/task-executor.md and mapping.json; global scope.
    # When: plan --repo REPO_ROOT --scope global --plugin dev --stage <stage>.
    # Then: the staged agent equals EXPECTED/agents/task-executor.md.
    # Verification items:
    #   - unified_diff(staged, golden) is empty
    #   - frontmatter keys are exactly description, mode, permission (no name, model, color, tools or skills);
    #     mode is "subagent"
    #   - description is a single-quoted scalar whose parsed value equals the source description
    #   - permission order: '*': deny; read/edit/bash/grep/glob/list allow; todowrite allow; skill {'*': deny, the
    #     4 skills allow}; external_directory {'*': deny, '~/.config/opencode/llm-agent-workflow/skills/*': allow}
    #   - the body starts with "Before starting, load these skills with the `skill` tool: " naming the 4 skills
    #   - subTests run the same golden diff for context-keeper, pr-creator and the verbatim opencode-gh-issue-to-pr
    #     (installed as agents/gh-issue-to-pr.md), per Design Doc "Output Comparison"
    # Pass criteria: every golden diff is empty.
    # ROI: 99 (BV:10 x Freq:9 + Legal:0 + Defect:9)
    # @category: core-functionality
    # @dependency: convert.py plan, frontmatter.py, rewrite.py, units.py, mapping.json
    # @real-dependency: filesystem (real sources)
    # @complexity: high
    def test_task_executor_converts_to_its_golden_file(self):
        self.skipTest("skeleton: AC-015")

    # AC-016 (V1, [unit], integration-level)
    # AC-016: "web-qa-reviewer shall get read, todowrite and the 7 chrome-devtools_* keys."
    # Given: the real plugin-qa/agents/web-qa-reviewer.md (with Read added to its tools at the source, N10).
    # When: plan --repo REPO_ROOT --scope global --plugin qa --stage <stage>.
    # Then: the staged permission map grants exactly the tools the agent needs.
    # Verification items:
    #   - the source frontmatter tools list includes Read
    #   - staged permission: '*': deny first; read: allow; todowrite: allow
    #   - exactly 7 keys match ^chrome-devtools_, each mapped from an mcp__chrome-devtools__<tool> entry, with the
    #     hyphen kept (N1), each at most 64 characters and allowed
    # Pass criteria: the permission map holds those keys and no other chrome-devtools key.
    # ROI: 72 (BV:8 x Freq:8 + Legal:0 + Defect:8)
    # @category: core-functionality
    # @dependency: convert.py plan, units.py, mapping.json (tools, mcp)
    # @real-dependency: filesystem (real sources)
    # @complexity: medium
    def test_web_qa_reviewer_gets_read_todowrite_and_chrome_devtools_keys(self):
        self.skipTest("skeleton: AC-016")

    # AC-017 (V1, [unit], integration-level half: alias flag -> mapping -> staged bodies)
    # AC-017: "One positive test per rewrite row (13). With --mcp-alias github=github-mcp, bodies shall read
    #   github-mcp_get_file_contents. "Read the file", "Edit the config" and npm run dev:server shall stay unchanged."
    # Given: the real dev and qa sources, plus a fixture agent whose body holds "Read the file", "Edit the config",
    #   "`npm run dev:server`" and "mcp__github__get_file_contents", and whose tools list the same MCP tool.
    # When: plan --repo <repo + fixture> --scope global --plugin dev --plugin qa --mcp-alias github=github-mcp.
    # Then: the alias reaches bodies and permission keys, and plain prose is untouched.
    # Verification items:
    #   - no staged file contains "mcp__github__"; every former reference reads "github-mcp_get_file_contents"
    #   - the fixture agent's permission has both the sanitized original key and the alias key (F3 "MCP keys,
    #     original plus alias")
    #   - "Read the file", "Edit the config" and "`npm run dev:server`" are byte-identical in the staged fixture body
    #   - without --mcp-alias (a subTest), the same reference reads "github_get_file_contents"
    # Pass criteria: all four checks hold, and plan exits 0.
    # ROI: 58 (BV:7 x Freq:7 + Legal:0 + Defect:9)
    # @category: integration
    # @dependency: convert.py plan (--mcp-alias), rewrite.py rules 10-12, mapping.json mcp
    # @real-dependency: filesystem (real sources)
    # @complexity: medium
    def test_mcp_alias_rewrites_bodies_and_plain_prose_stays_unchanged(self):
        self.skipTest("skeleton: AC-017")


if __name__ == "__main__":
    unittest.main()
