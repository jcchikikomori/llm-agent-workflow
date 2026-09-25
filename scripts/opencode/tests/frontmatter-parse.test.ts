// Frontmatter parse Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.4)
// Generated: 2026-09-24 | Budget Used: 2/3 integration (F3 emitter), 0/2 E2E
//
// Skeleton. Run with:
//
//   bun test scripts/opencode/tests
//
// Harness (to implement; Design Doc "Test Strategy > TS" and N24/N25):
// - Staged tree: Bun.spawnSync(["python3", "scripts/opencode/convert.py", "plan", "--repo", <repo>, "--scope-root",
//   <tmp>/scope, "--scope", "global", "--stage", <tmp>/stage]) with HOME=<tmp>/home.
// - Installed tree: Bun.spawnSync(["bash", "setup-opencode.sh", "--project", <tmp>/project]) with the stub
//   skills-md converter from test_setup_opencode.py, or the real one at SKILLS_MD_DIR when SKILLS_MD_REAL=1.
//   The real converter is where the `awk -v` quirk turns \" into " (N24), so that run carries the most weight.
// - The frontmatter block is sliced from the leading "---" to the next "---" line and parsed with Bun.YAML.parse
//   (bun 1.3.11). The source description is parsed the same way from the source file.

import { describe, test } from "bun:test"

describe("frontmatter of converted units parses and keeps its description", () => {
  // AC-047 (D002/D013, [bun]), staged half
  // AC: "Every staged and installed SKILL.md, agent and command frontmatter shall parse with Bun.YAML.parse, and the
  //   parsed description shall equal the YAML-parsed source description byte for byte. That covers the 5 skills and
  //   26 agents with ", and the two gh-issue-to-pr descriptions with #N."
  // Given: the staged tree for every plugin, global scope.
  // When: every staged agents/*.md, commands/*.md and llm-agent-workflow/skills/*/SKILL.md frontmatter is parsed.
  // Then: nothing is truncated, unquoted or lost.
  // Verification items:
  //   - Bun.YAML.parse does not throw for any file
  //   - for units with a source description, parsed.description === the parsed source description (same bytes)
  //   - that set includes the 5 skills and 26 agents whose descriptions contain a double quote, and both
  //     gh-issue-to-pr descriptions (the hand-written agent and the command) that contain "#N"
  //   - no staged frontmatter contains a backslash (single-quoted emitter, D002)
  // Pass criteria: 0 parse errors and 0 description mismatches.
  // ROI: 99 (BV:9 x Freq:10 + Legal:0 + Defect:9) | opencode drops a skill or agent whose frontmatter is invalid
  // @category: core-functionality
  // @dependency: convert.py plan, the frontmatter emitter, Bun.YAML
  // @real-dependency: filesystem (real sources)
  // @complexity: medium
  test.todo("AC-047: every staged agent, command and SKILL.md frontmatter parses and keeps its description")

  // AC-047 (D002/D013, [bun]), installed half
  // AC: (as above) "Every staged and installed SKILL.md, agent and command frontmatter shall parse ..."
  // Given: a project-scope install of every plugin through setup-opencode.sh (real skills-md converter when
  //   SKILLS_MD_REAL=1, stub otherwise).
  // When: every .opencode/agents/*.md, .opencode/commands/*.md and .opencode/llm-agent-workflow/skills/*/SKILL.md
  //   frontmatter is parsed.
  // Then: the skills-md validation step did not corrupt any frontmatter on the way in.
  // Verification items:
  //   - Bun.YAML.parse does not throw for any installed file
  //   - each installed description equals its source description byte for byte (the same set as the staged half)
  //   - with the real converter, a description holding a colon still parses to the same string (F3: single-quoted
  //     values pass the sanitizer unchanged)
  // Pass criteria: 0 parse errors and 0 mismatches; the run records which converter it used.
  // ROI: 81 (BV:9 x Freq:8 + Legal:0 + Defect:9)
  // @category: integration
  // @dependency: setup-opencode.sh, convert.py, skills-md converter (stub or real), Bun.YAML
  // @real-dependency: filesystem; the skills-md converter when SKILLS_MD_REAL=1
  // @complexity: medium
  test.todo("AC-047: every installed agent, command and SKILL.md frontmatter parses and keeps its description")
})
