#!/usr/bin/env python3
# Skill conversion Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.4)
# Generated: 2026-09-24 | Budget Used: 6 integration over 5 features (F3 validation 2/3, F3 skill transform 1/3,
# F3 rewrite rules 1/3 with 1 more in test_convert_agents.py, F3 uniqueness 1/3, F6 skills-md 1/3), 0/2 E2E
"""Integration tests for skill conversion through `convert.py plan` (skeleton).

Each test runs the real planner in a subprocess against this repo's sources (or a small fixture repo) and inspects
the manifest, stderr and the staged tree:

  python3 scripts/opencode/convert.py plan --repo DIR --scope-root DIR --scope global|project --stage DIR [...]

Manifest rows: plugin, unit, stage_rel, target_rel, sha256, realpath, repo, verdict (tab-separated). Exit 0, 3 when
some units are rejected, 1 fatal, 2 usage.

  python3 -m unittest discover -s scripts/opencode/tests
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONVERT = REPO_ROOT / "scripts" / "opencode" / "convert.py"

# Harness (to implement): one tempfile.TemporaryDirectory() per test holding home/, scope/ and stage/, plus a
# fixture repo when a test needs bad inputs (a copy of .claude-plugin/marketplace.json, mapping.json, and one
# plugin-fixture/ dir with .claude-plugin/plugin.json). HOME=<tmp>/home; XDG_* unset. Real git where a scanned root
# must be a checkout. No mocks.

# The 15 recipe skills (Design Doc F-31; the recipe policy patterns are recipe-* and start).
RECIPE_SKILLS = (
    "recipe-build", "recipe-design", "recipe-diagnose", "recipe-generate-claude-md", "recipe-implement",
    "recipe-plan", "recipe-pr-review", "recipe-reverse-engineer", "recipe-review", "recipe-task",
    "recipe-update-doc", "recipe-add-integration-tests", "recipe-backend-integration-test", "recipe-web-qa",
    "start",
)


class SkillValidationTests(unittest.TestCase):
    """F3 frontmatter validation."""

    # AC-018 (V1, [unit], integration-level: plan's exit-3 contract)
    # AC-018: "Negative cases: an unknown tool, or an unknown subagent_type, gives a WARN; placeholders (post-strip)
    #   give no warning; missing frontmatter gives a WARN and a skip; a name/dir mismatch, a bad name regex, a
    #   backslash, or an unquoted space-then-# gives an ERROR, exit 3, and the other units stay staged"
    # Given: a fixture repo holding
    #   - an agent whose tools list "Frobnicate", with subagent_type "unknown-agent" and the placeholder
    #     "[Update Agent from Step 2]" in its body
    #   - a skill with no frontmatter
    #   - skills with a name/dir mismatch, the name "Bad_Name", a backslash in the frontmatter, and the plain
    #     scalar "description: fixes things #tag"
    #   - one valid skill and one valid agent
    # When: plan --repo <fixture> --scope project --stage <stage>.
    # Then: bad units are rejected with file:line, and everything else still stages.
    # Verification items:
    #   - exit 3
    #   - stderr has "WARN <path>:<line>: <rule>:" for Frobnicate and for unknown-agent, and nothing for the
    #     placeholder
    #   - stderr has a WARN for the skill with no frontmatter, which is not in the manifest
    #   - stderr has one "ERROR <path>:<line>: <rule>:" per mismatch, regex, backslash and space-then-# case; none of
    #     those units are in the manifest or the stage
    #   - the valid skill, the valid agent and the WARN agent are in the manifest and staged; the WARN agent's
    #     permission has no key for Frobnicate
    # Pass criteria: the exit code, the WARN/ERROR set and the staged set all match.
    # ROI: 49 (BV:8 x Freq:5 + Legal:0 + Defect:9)
    # @category: edge-case
    # @dependency: convert.py plan, frontmatter.py, rewrite.py rule 9, units.py
    # @real-dependency: filesystem
    # @complexity: medium
    def test_plan_rejects_invalid_units_with_exit_3_and_stages_the_rest(self):
        self.skipTest("skeleton: AC-018")

    # AC-021 (B, [unit], integration-level)
    # AC-021: "Every staged SKILL.md description shall be <=250 UTF-8 bytes, measured on the full value (every
    #   continuation line of a plain scalar, or the whole folded/literal block), not the first line. The 14 sources
    #   named in the slices shall comply."
    # Given: the real repo (every plugin plus wandavision/skill/*), and a fixture skill whose plain-scalar description
    #   has a short first line and a continuation line that takes the full value over 250 bytes.
    # When: plan --repo REPO_ROOT --scope global --stage <stage> (all plugins); plan against the fixture.
    # Then: every description fits, measured on the full value.
    # Verification items:
    #   - for every staged SKILL.md, len(parsed_description.encode("utf-8")) <= 250
    #   - the 14 sources are <= 250 bytes in the source tree: ai-attribution, commit-guard, start, env-guard,
    #     gh-issue-to-pr, markdown-lsp, memory-guard, mempalace-docker, opencode-migrate, coverage-quality,
    #     recipe-backend-integration-test, recipe-web-qa, ruby-lsp, wandavision
    #   - the fixture gives an ERROR at its path:line, and exit 3
    # Pass criteria: no staged description is over 250 bytes, and the fixture is rejected.
    # ROI: 86 (BV:8 x Freq:10 + Legal:0 + Defect:6) | opencode drops a skill whose frontmatter is invalid
    # @category: core-functionality
    # @dependency: convert.py plan, frontmatter.py
    # @real-dependency: filesystem (real sources)
    # @complexity: low
    def test_every_staged_skill_description_fits_250_bytes_on_the_full_value(self):
        self.skipTest("skeleton: AC-021")


class SkillTransformTests(unittest.TestCase):
    """F3 skill transform and rewrite scoping."""

    # AC-019 (V1, [unit], integration-level)
    # AC-019: "The 15 recipe skills shall stage a skill and a command with description, $ARGUMENTS and the
    #   base-directory trailer."
    # Given: the real dev and qa sources; global scope.
    # When: plan --repo REPO_ROOT --scope global --plugin dev --plugin qa --stage <stage>.
    # Then: each recipe gets a skill unit and a paired command unit.
    # Verification items:
    #   - for each name in RECIPE_SKILLS: staged llm-agent-workflow/skills/<n>/SKILL.md and commands/<n>.md exist
    #   - the command frontmatter has only a single-quoted description
    #   - the command body keeps "$ARGUMENTS" wherever the source SKILL.md has it
    #   - the command ends with "Base directory for this skill: ~/.config/opencode/llm-agent-workflow/skills/<n>"
    #   - no staged skill keeps context, agent, argument-hint, allowed-tools or model keys
    #   - commands/recipe-implement.md equals EXPECTED/commands/recipe-implement.md (golden)
    # Pass criteria: 15 skill/command pairs meet every check, and the golden diff is empty.
    # ROI: 79 (BV:9 x Freq:8 + Legal:0 + Defect:7)
    # @category: core-functionality
    # @dependency: convert.py plan, units.py, rewrite.py
    # @real-dependency: filesystem (real sources)
    # @complexity: medium
    def test_recipe_skills_stage_a_skill_and_a_paired_command(self):
        self.skipTest("skeleton: AC-019")

    # AC-022 (D, [unit]) + AC-023 (V1, [unit]); both check rewrite outcomes over one real-repo stage
    # AC-022: "Rewrite scoping applies to converted Markdown units only (agents, skills, commands). Payload
    #   directories and TS files are copied verbatim and exempt ... the two exempt files keep CLAUDE.md, and so does
    #   every unit generated from them (the recipe-generate-claude-md command included); context-keeper,
    #   context-scouter and the memory-guard skill reference {memory_root} and not ~/.claude/projects/; the mempalace
    #   skill keeps ~/.claude/projects (lines 18 and 23); rule-advisor references {skills_root}; no other converted
    #   Markdown unit contains CLAUDE.md"
    # AC-023: "Under plugin-*/agents/, plugin-*/skills/ and plugin-*/commands/ (not scripts/opencode/ and not any
    #   tests/), no file contains qa-workflows: or dev:<qa-agent>; the staged output has no
    #   (dev|qa|qa-workflows):<known agent> token."
    # Given: the real repo; global scope; XDG_DATA_HOME unset.
    # When: plan --repo REPO_ROOT --scope global --stage <stage> (all plugins).
    # Then: each rewrite applies only where it is scoped to.
    # Verification items:
    #   - the staged claude-md-generator agent, the recipe-generate-claude-md skill and its command contain
    #     "CLAUDE.md"
    #   - staged context-keeper, context-scouter and memory-guard SKILL.md contain
    #     "~/.local/share/com.jcchikikomori.llmworkflow/opencode-memory/" and not "~/.claude/projects/"
    #   - the staged mempalace-docker SKILL.md keeps "~/.claude/projects" on its source lines 18 and 23
    #   - the staged rule-advisor contains "~/.config/opencode/llm-agent-workflow/skills/"
    #   - no other staged .md unit contains "CLAUDE.md"
    #   - staged payload files (for example memory-guard config/watched-paths.json and hooks/*.py) and every TS
    #     file are byte-identical to their sources
    #   - no staged file matches (dev|qa|qa-workflows):<known agent>; no source file under
    #     plugin-*/{agents,skills,commands}/ contains "qa-workflows:" or "dev:<qa-agent>"
    # Pass criteria: every check holds.
    # ROI: 64 (BV:8 x Freq:7 + Legal:0 + Defect:8)
    # @category: core-functionality
    # @dependency: convert.py plan, rewrite.py rules 2-8, mapping.json paths
    # @real-dependency: filesystem (real sources)
    # @complexity: medium
    def test_staged_units_apply_scoped_rewrites_and_carry_no_namespace_tokens(self):
        self.skipTest("skeleton: AC-022, AC-023")


class SkillUniquenessTests(unittest.TestCase):
    """F3 uniqueness across scanned roots."""

    # AC-056 (DR, [unit], integration-level)
    # AC-056: "The uniqueness scan shall find SKILL.md at any depth under a scanned root and register the parent
    #   directory name: a fixture root with skills/deep/nested/start/SKILL.md collides with our start; a root holding
    #   both start/SKILL.md and skills/start/SKILL.md is reported once as [DUPLICATE] start naming both paths. A
    #   SKILL.md whose parent dir fails the name regex is skipped with a WARN, not registered."
    # Given (one subTest each, fresh HOME):
    #   a. ~/.agents/skills/skills/deep/nested/start/SKILL.md, whose content differs from ours
    #   b. ~/.claude/skills holding both start/SKILL.md and skills/start/SKILL.md (different content)
    #   c. ~/.agents/skills/Bad_Name/SKILL.md
    # When: plan --repo REPO_ROOT --scope global --plugin dev --stage <stage>.
    # Then: the scan is recursive and registers the parent dir name.
    # Verification items:
    #   - a: our start skill row has verdict duplicate:<deep path>, and its paired commands/start.md row is skipped
    #     with it
    #   - b: exactly one "[DUPLICATE] start" report, naming both paths
    #   - c: a WARN names the Bad_Name path; nothing named Bad_Name is registered, and our skills keep verdict ok
    # Pass criteria: the verdicts and report lines match in all three subTests.
    # ROI: 56 (BV:8 x Freq:6 + Legal:0 + Defect:8)
    # @category: edge-case
    # @dependency: convert.py plan, units.py uniqueness scan, mapping.json skills.scanned_roots
    # @real-dependency: filesystem
    # @complexity: medium
    def test_uniqueness_scan_registers_skill_md_at_any_depth(self):
        self.skipTest("skeleton: AC-056")


class SkillsMdSelectionTests(unittest.TestCase):
    """F6 skills-md: the --with-skills-md skip decision (plan level)."""

    # AC-052 (D, [e2e], pushed down: plan emits the units, so plan decides the skip)
    # AC-052: "Given <scope>/skills is a git checkout whose origin ends in /skills-md (or /skills-md.git),
    #   --with-skills-md shall print [SKIP] skills-md: already present at <path> and emit no skills-md units."
    # Given (one subTest each): the scope's skills/ is a checkout with origin
    #   (a) https://example.invalid/jcchikikomori/skills-md.git or (b) https://example.invalid/jcchikikomori/skills-md;
    #   (c) control: skills/ does not exist. SKILLS_MD_DIR is a fixture with two skills.
    # When: plan --scope global --with-skills-md --skills-md <fixture> --stage <stage>.
    # Then: an existing skills-md checkout suppresses every skills-md unit.
    # Verification items:
    #   - a and b: no manifest row has plugin "skills-md"; the output reports "[SKIP] skills-md: already present at
    #     <realpath of skills/>" with the credential-free origin
    #   - c: the two fixture skills appear as plugin "skills-md" rows
    #   - the same holds with --scope project and <project>/.opencode/skills (the check runs for both scopes)
    # Pass criteria: the manifest rows and the skip report match in all subTests.
    # Interpretation (Low): if the installer rather than plan prints the [SKIP] line, the text check moves to
    #   SkillsMdTests in test_setup_opencode.py; the "no skills-md rows" check stays here.
    # ROI: 56 (BV:8 x Freq:6 + Legal:0 + Defect:8) | the live global case
    # @category: core-functionality
    # @dependency: convert.py plan (--with-skills-md), git (origin query)
    # @real-dependency: git, filesystem
    # @complexity: low
    def test_with_skills_md_is_skipped_when_the_native_skills_dir_is_a_skills_md_checkout(self):
        self.skipTest("skeleton: AC-052")


if __name__ == "__main__":
    unittest.main()
