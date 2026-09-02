# My LLM Agent Workflow

[![Claude Code](https://img.shields.io/badge/Claude%20Code-Plugin-purple)](https://claude.ai/code)
[![GitHub Stars](https://img.shields.io/github/stars/jcchikikomori/claude-workflow?style=social)](https://github.com/jcchikikomori/claude-workflow)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/jcchikikomori/claude-workflow/pulls)

End-to-end development workflows for [Claude Code](https://claude.ai/code) and [OpenCode](https://opencode.ai) — specialized agents handle requirements, design, implementation, and quality checks so you get reviewable code, not just generated code.

A personal fork of [@shinpr](https://github.com/shinpr)'s [claude-code-workflows](https://github.com/shinpr/claude-code-workflows), extended with a QA plugin, env-guard, and Windows compatibility.

---

## Platform Support

| Platform | Status |
| -------- | ------ |
| macOS | Supported |
| Linux | Supported |
| WSL (Windows) | Supported |
| Native Windows | N/A |
| Termux | N/A |
| Android | N/A |

Plugin files are copied directly — no symlinks. Full compatibility across all platforms.

---

## Recommended Settings

Before using these workflows, configure `~/.claude/settings.json` to pre-approve the read-only commands that orchestrators run automatically. Without these, Claude will prompt for permission on every git inspection call.

```json
{
  "permissions": {
    "allow": [
      "Bash(git remote:*)",
      "Bash(git status:*)",
      "Bash(git diff:*)",
      "Bash(git log:*)",
      "Bash(gh repo:*)",
      "Bash(gh pr:*)"
    ]
  }
}
```

For PR review workflows that post inline comments, also add:

```json
{
  "permissions": {
    "allow": [
      "mcp__github__pull_request_read",
      "mcp__github__list_pull_requests",
      "mcp__github__get_file_contents",
      "mcp__github__search_code",
      "mcp__github__pull_request_review_write",
      "mcp__github__add_comment_to_pending_review"
    ]
  }
}
```

For `claude-attribution` plugin (posting to external platforms via CLI):

```json
{
  "permissions": {
    "allow": [
      "Bash(gh pr create:*)",
      "Bash(gh pr comment:*)",
      "Bash(gh pr review:*)",
      "Bash(gh pr edit:*)",
      "Bash(gh issue create:*)",
      "Bash(gh issue comment:*)",
      "Bash(gh issue edit:*)",
      "Bash(gh api:*)"
    ]
  }
}
```

> **Note:** The attribution hook runs as a PreToolUse gate — it blocks the command before execution if the attribution line is missing. These permissions allow the CLI commands to proceed _after_ the hook approves them.

Merge these into your existing `permissions.allow` array — do not replace it.

---

## Quick Start

```bash
# 1. Start Claude Code
claude

# 2. On a separate terminal, clone the repo and add it as a local marketplace
git clone https://github.com/jcchikikomori/claude-workflow <install-path>

# Now, going back to the Claude Code
/plugin marketplace add <install-path>

# 3. Install some plugins
/plugin install dev@claude-workflow
/plugin install qa@claude-workflow
/plugin install env-guard@claude-workflow

# 4. Reload plugins after installation
/reload-plugins

# 5. Start building
/recipe-implement "Add user authentication with JWT"
```

For fullstack projects:

```bash
/recipe-fullstack-implement "Add user authentication with JWT + login form"
```

### Wandavision Setup

For `wandavision`, follow the initial steps below:

1. `./setup-wandavision.sh` from the repo root copies `wandavision/{bin,skill,opencode-plugin}/` into the XDG location. Idempotent.
2. Add the `wandavision` MCP entry to `opencode.json` pointing at the XDG wrapper:

   ```json
   "mcp": {
     "wandavision": {
       "type": "local",
       "command": ["{env:HOME}/.local/share/com.jcchikikomori.llmworkflow/wandavision/bin/run-wandavision.sh"],
       "enabled": true
     }
   }
   ```

The reminder hook is copied into `.opencode/plugins/` by the line above; the slash command `/wandavision` is owned by the dotfiles project (or you can copy `wandavision/skill/wandavision/SKILL.md`'s command-file equivalent manually). See `~/.local/share/com.jcchikikomori.llmworkflow/wandavision/dotfiles-migration.md` for the keep-in-sync procedure.

---

## Plugins

| Plugin | Category | What it provides |
| ------ | -------- | ---------------- |
| `dev` | workflow-orchestration | Agent-driven recipes for web, mobile, and integration development |
| `qa` | product-quality | Agent-driven recipes for acceptance tests, E2E, and browser-layer QA |
| `env-guard` | behavior-control | Hook enforcement to prevent leaking `.env` and secrets |
| `claude-attribution` | governance | Ensures all external posts carry "🤖 Written by Claude, reviewed by \<user\>" attribution |
| `markdown-format` | quality-enforcement | PostToolUse hook that runs `markdownlint-cli2 --fix` on every `.md` write — non-blocking |
| `commit-guard` | behavior-control | PreToolUse hook that intercepts every `git commit`, shows staged files + message for user approval before the commit runs |
| `gh-issue-to-pr` | workflow-orchestration | Agent that drives a single GitHub issue end-to-end to a merged PR — investigate, plan, branch, implement, test, commit (with confirmation), PR, review, merge, close |
| `opencode-migrate` | workflow-orchestration | Skill that migrates a Claude Code setup into [opencode](https://opencode.ai) — global config, one repository, or a Claude Code plugin's own source — behind a plan-then-approve gate |
| `mempalace-docker` | behavior-control | Runs [MemPalace](https://github.com/MemPalace/mempalace) entirely from Docker — MCP server, CLI, and save hooks. Picks the CUDA image when an NVIDIA GPU is actually usable, keeps one palace in a named volume, mounts the current project, and auto-mines it per project. **Replaces** the official `mempalace` plugin |
| `metronome` | behavior-control | Detects shortcut-taking and nudges Claude to proceed step by step |
| `discover` | product-quality | Turns feature ideas into evidence-backed PRDs through structured discovery |
| `caveman` | behavior-control | A plugin that makes agent talk like caveman |
| `wandavision` | quality-enforcement | Bundles the `mcp-vision` MCP server and enforces deterministic, pixel-accurate reads of every image instead of LLM guesswork |

The `dev` and `qa` plugins cover **workflow orchestration** — how to plan, build, and verify software using AI agents. Install the `skills` plugin from [skills-md](https://github.com/jcchikikomori/skills-md) for language/framework-specific rules (Ruby, Python, React, Node.js, Docker, etc.).s

---

## How It Works

### The Workflow

```mermaid
graph TB
    A[User Request] --> B[requirement-analyzer]

    B --> |Large 6+ files| C[prd-creator]
    B --> |Medium 3-5 files| CA[codebase-analyzer]
    B --> |Small 1-2 files| E[Direct Implementation]

    C --> CA
    CA --> D[technical-designer]
    D --> CV[code-verifier]
    CV --> DR[document-reviewer]
    DR --> DS[design-sync]
    DS --> F[acceptance-test-generator]
    F --> G[work-planner]
    G --> H[task-decomposer]

    H --> I[task-executor]
    E --> I

    I --> J[quality-fixer]
    J --> K[Ready to Commit]
```

### The Diagnosis Workflow

```mermaid
graph LR
    P[Problem] --> INV[investigator]
    INV --> |Failure Points| VER[verifier]
    VER --> |Coverage Check| COV{Sufficient?}
    COV --> |Yes| SOL[solver]
    COV --> |No| INV
    SOL --> |Solutions + Steps| R[Report]
```

### What Happens Behind the Scenes

1. **Analysis** — requirement-analyzer determines scale and picks the right workflow
2. **Codebase Understanding** — codebase-analyzer informs design decisions
3. **Planning** — technical-designer (+ ui-spec-designer for frontend) produces testable specs
4. **Execution** — task-executor / task-executor-frontend builds and tests each task
5. **Quality** — quality-fixer runs tests, fixes type errors, verifies before commit
6. **Review** — acceptance criteria trace from design through test skeletons

---

## Workflow Recipes

All workflow entry points use the `recipe-` prefix. Type `/recipe-` and use tab completion.

### Development (plugin: dev)

| Recipe | Purpose |
| -------- | --------- |
| `/recipe-implement` | End-to-end feature development |
| `/recipe-fullstack-implement` | End-to-end fullstack (backend + frontend) |
| `/recipe-task` | Single task with precision — bug fixes, small changes |
| `/recipe-design` | Create design documentation |
| `/recipe-plan` | Generate work plan from design doc |
| `/recipe-build` | Execute from existing task plan |
| `/recipe-fullstack-build` | Execute fullstack task plan |
| `/recipe-front-design` | Create UI Spec + frontend Design Doc |
| `/recipe-front-plan` | Generate frontend work plan |
| `/recipe-front-build` | Execute frontend task plan |
| `/recipe-front-review` | Verify frontend code against design docs |
| `/recipe-review` | Verify code against design docs |
| `/recipe-diagnose` | Investigate problems, derive solutions |
| `/recipe-reverse-engineer` | Generate PRD/Design Docs from existing code |
| `/recipe-update-doc` | Update existing design documents |
| `/recipe-generate-claude-md` | Generate a CLAUDE.md for a project |
| `/recipe-pr-review` | Review a PR from another developer with codebase context |

### QA (plugin: qa)

| Recipe                           | Purpose                                      |
| -------------------------------- | -------------------------------------------- |
| `/recipe-add-integration-tests`  | Add integration/E2E tests to existing code   |
| `/recipe-web-qa`                 | Browser-layer QA on a live running web app   |

---

## Specialized Agents

### plugin-dev Agents (27)

| Agent | What It Does |
| ------- | -------------- |
| **requirement-analyzer** | Determines task scale and selects the right workflow |
| **codebase-analyzer** | Analyzes existing codebase to inform design |
| **prd-creator** | Writes product requirement docs for complex features |
| **technical-designer** | Plans architecture and tech stack decisions |
| **technical-designer-frontend** | Plans React component architecture and state management |
| **ui-spec-designer** | Creates UI Specifications from PRD and prototype code |
| **scope-discoverer** | Discovers functional scope from codebase for reverse engineering |
| **work-planner** | Breaks down design docs into actionable tasks |
| **task-decomposer** | Splits work into small, commit-ready chunks |
| **task-executor** | Implements backend/general features with TDD |
| **task-executor-frontend** | Implements React components with Testing Library |
| **quality-fixer** | Runs tests, fixes type errors, handles linting |
| **quality-fixer-frontend** | Handles React-specific tests, TypeScript checks, and builds |
| **code-verifier** | Validates consistency between documentation and code |
| **code-reviewer** | Checks code against design docs for completeness |
| **pr-reviewer** | Reviews a PR diff against codebase patterns — no Design Doc needed |
| **document-reviewer** | Reviews document quality and rule compliance |
| **design-sync** | Verifies consistency across multiple Design Docs |
| **security-reviewer** | Reviews implementation for security compliance |
| **investigator** | Maps execution paths, identifies failure points |
| **verifier** | Validates failure points using Devil's Advocate method |
| **solver** | Generates solutions with tradeoff analysis |
| **rule-advisor** | Picks the best coding rules for your current task |
| **claude-md-generator** | Generates a CLAUDE.md by analyzing project structure and stack |
| **context-keeper** | Captures gotchas, learnings, and corrections to persist across sessions |
| **context-scouter** | Loads accumulated project memory for agents to consume at session start |

### plugin-qa Agents (3)

| Agent | What It Does |
| ------- | -------------- |
| **acceptance-test-generator** | Creates E2E and integration test scaffolds from requirements |
| **integration-test-reviewer** | Reviews integration/E2E tests for skeleton compliance and quality |
| **web-qa-reviewer** | Browser-layer QA via Chrome DevTools — Lighthouse, console errors, network failures |

---

## Repository Structure

```text
claude-workflow/
├── .claude-plugin/
│   └── marketplace.json          # Marketplace registry
│
├── plugin-dev/                   # dev plugin — web, mobile, integrations
│   ├── agents/                   # 23 DEV agents
│   ├── skills/                   # DEV skills (recipes, coding principles, guides)
│   └── .claude-plugin/
│       └── plugin.json
│
├── plugin-qa/                    # qa plugin — web, mobile, integration testing
│   ├── agents/                   # 3 QA agents
│   ├── skills/                   # QA skills (testing principles, E2E design, recipes)
│   └── .claude-plugin/
│       └── plugin.json
│
├── plugin-env-guard/             # env-guard plugin — secrets leak prevention
│   ├── hooks/
│   │   ├── hooks.json            # PreToolUse hook registration
│   │   └── env_guard_hook.py     # Blocking script (exits 2 on sensitive path)
│   ├── skills/
│   │   └── env-guard/SKILL.md   # Behavioral guidance injected into Claude context
│   ├── agents/
│   │   └── secret-exposure-auditor.md
│   └── .claude-plugin/
│       └── plugin.json
│
├── plugin-attribution/           # claude-attribution plugin — AI authorship attribution
│   ├── hooks/                    # Claude Code: PreToolUse hook
│   │   ├── hooks.json            # PreToolUse hook on mcp__.*|Bash
│   │   └── attribution_hook.py   # Dynamic body-field detection + attribution check
│   ├── skills/                   # (shared — OpenCode reads .claude/skills/ natively)
│   │   └── claude-attribution/SKILL.md
│   ├── commands/                 # OpenCode: /claude-attribution slash command (NEW)
│   ├── plugins/                  # OpenCode: TypeScript plugin (NEW)
│   ├── package.json              # OpenCode: npm manifest (NEW)
│   └── .claude-plugin/
│       └── plugin.json
│
├── plugin-markdown-format/       # markdown-format plugin — auto-fix .md files on write
│   ├── config/
│   │   └── .markdownlint.json    # Bundled ruleset (MD013/041/033 off, MD024 siblings_only)
│   ├── hooks/
│   │   ├── hooks.json            # PostToolUse hook on Write|Edit|MultiEdit
│   │   └── markdown_format_hook.py  # Runs markdownlint-cli2 --fix, always exits 0
│   ├── skills/
│   │   └── markdown-format/SKILL.md
│   └── .claude-plugin/
│       └── plugin.json
│
├── plugin-gh-issue-to-pr/        # gh-issue-to-pr plugin — GitHub issue-to-merged-PR agent
│   ├── agents/
│   │   └── gh-issue-to-pr.md
│   ├── skills/
│   │   └── gh-issue-to-pr/SKILL.md
│   └── .claude-plugin/
│       └── plugin.json
│
├── plugin-opencode-migrate/      # opencode-migrate plugin — Claude Code -> opencode migration
│   ├── skills/
│   │   └── opencode-migrate/
│   │       ├── SKILL.md          # orchestrator: discover -> plan -> gate -> apply -> verify
│   │       └── references/       # global.md, project.md, plugin-port.md
│   └── .claude-plugin/
│       └── plugin.json
│
├── plugin-mempalace-docker/      # mempalace-docker plugin — Dockerized MemPalace, GPU-aware
│   ├── .mcp.json                 # mempalace server -> scripts/run-mempalace.sh
│   ├── NOTICE                    # MIT attribution for vendored MemPalace code
│   ├── scripts/
│   │   ├── run-mempalace.sh      # MCP wrapper: image selection + mounts
│   │   ├── lib/common.sh         # shared GPU detection / mount assembly
│   │   ├── bin/mempalace         # containerized CLI (HOME stays /data)
│   │   ├── bin/mempalace-python3 # MEMPAL_PYTHON target for the vendored hooks
│   │   ├── build-image.sh        # builds mempalace:gpu (upstream ships no GPU tag)
│   │   ├── migrate-host-palace.sh# split-palace consolidation, backup-first
│   │   └── mark_mined.py         # per-project mine stamp
│   ├── hooks/
│   │   ├── session_start_hook.py # conflict scan + auto-mine prompt
│   │   └── vendor/               # MemPalace's own hooks, byte-identical
│   ├── skills/
│   │   └── mempalace-docker/SKILL.md
│   └── .claude-plugin/
│       └── plugin.json
│
├── LICENSE
└── README.md
```

Each plugin owns its agents and skills directly — no shared root directories, no symlinks.

---

## OpenCode

Seven plugins also ship OpenCode-compatible sources — TypeScript hooks in `plugins/`, slash commands in `commands/`, and (for `gh-issue-to-pr`) an agent in `agents/`. Skills are reused as-is because OpenCode reads `.claude/skills/<name>/SKILL.md` natively.

### Install on OpenCode

OpenCode loads plugin `.ts` files from `.opencode/plugins/` (project) or `~/.config/opencode/plugins/` (global). Copy the plugin files and add command/agent entries to `opencode.json`:

`./setup-opencode.sh --global` (or `--project <path>`) does this copying for you and records what it installed so `--uninstall` can undo it; `--list` and `--dry-run` show what it would do. The manual recipe below is the same thing by hand.

```bash
# Project-local — copy the TypeScript plugin files into .opencode/plugins/
mkdir -p .opencode/plugins
cp plugin-{attribution,commit-guard,env-guard,markdown-format,memory-guard,token-saver}/plugins/*.ts .opencode/plugins/
cp wandavision/opencode-plugin/*.ts .opencode/plugins/

# Copy the command markdown files
mkdir -p .opencode/commands
cp plugin-{attribution,commit-guard,memory-guard,gh-issue-to-pr}/commands/*.md .opencode/commands/
```

#### Installing custom agents (e.g. gh-issue-to-pr)

```bash
# (gh-issue-to-pr only) — copy the OpenCode agent
mkdir -p .opencode/agents
cp plugin-gh-issue-to-pr/agents/opencode-gh-issue-to-pr.md .opencode/agents/
```

Then add the `gh-issue-to-pr` agent to your `opencode.json`:

```json
{
  "agent": {
    "gh-issue-to-pr": {
      "description": "Drives a single GitHub issue end-to-end to a merged PR",
      "mode": "subagent",
      "permission": { "edit": "allow", "bash": "allow", "webfetch": "allow" }
    }
  }
}
```

---

## FAQ

**Q: Which plugin should I install?**

Install `dev` for development work (backend, frontend, mobile, integrations). Install `qa` for testing workflows — acceptance test generation, integration test review, or browser-layer QA. Both can run side-by-side.

**Q: Can I use both plugins at the same time?**

Yes. The `dev` plugin handles planning, implementation, and code review; the `qa` plugin handles test generation and browser QA. When both are installed, dev recipes automatically call qa agents for test generation using the `qa-workflows:` plugin prefix.

**Q: What about frontend development?**

Frontend recipes (`/recipe-front-design`, `/recipe-front-plan`, `/recipe-front-build`, `/recipe-front-review`) are included in the `dev` plugin. No separate frontend plugin needed.

**Q: What if there are errors?**

The `quality-fixer` and `quality-fixer-frontend` agents automatically fix most issues — test failures, type errors, lint problems. If something can't be auto-fixed, you'll get clear guidance on what needs attention.

**Q: What does env-guard do and do I need it?**

It adds a security enforcement layer that blocks Claude from reading or leaking sensitive files. The block happens at the tool level before execution — it cannot be bypassed by prompt instructions. Install it alongside any other plugin.

**Q: What does claude-attribution do?**

It ensures every external post (GitHub PRs, JIRA comments, Slack messages, etc.) carries a "🤖 Written by Claude, reviewed by \<name\>" attribution line. A PreToolUse hook blocks posts missing the line, and a companion skill ensures Claude shows the post to you for approval before sending.

**Q: What does markdown-format do?**

It auto-fixes lint errors in `.md` files after every write. A PostToolUse hook runs `markdownlint-cli2 --fix` silently — no writes are ever blocked. Requires Node.js; `npx` handles the download automatically on first use so no global install is needed.

---

## Contributing External Plugins

This marketplace supports the full lifecycle of building products with AI. If your plugin helps developers build better products with AI coding agents, see [CONTRIBUTING.md](CONTRIBUTING.md) for submission guidelines.

---

## License

MIT License — free to use, modify, and distribute.

See [LICENSE](LICENSE) for full details.

---

Built and maintained by [@jcchikikomori](https://github.com/jcchikikomori). Originally forked from [@shinpr](https://github.com/shinpr)'s [claude-code-workflows](https://github.com/shinpr/claude-code-workflows).
