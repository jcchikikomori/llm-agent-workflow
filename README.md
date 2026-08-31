# Claude Code Workflows

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
| Native Windows | Supported |

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

# 2. Install the marketplace
/plugin marketplace add jcchikikomori/claude-workflow

# 3. Install plugins
/plugin install dev@claude-workflow
/plugin install qa@claude-workflow           # optional: QA workflows
/plugin install env-guard@claude-workflow    # optional: secrets protection

# 4. Reload plugins
/reload-plugins

# 5. Start building
/recipe-implement "Add user authentication with JWT"
```

For fullstack projects:

```bash
/recipe-fullstack-implement "Add user authentication with JWT + login form"
```

---

## OpenCode

Seven plugins also ship OpenCode-compatible sources — TypeScript hooks in `plugins/`, slash commands in `commands/`, and (for `gh-issue-to-pr`) an agent in `agents/`. Skills are reused as-is because OpenCode reads `.claude/skills/<name>/SKILL.md` natively.

### Install on OpenCode

OpenCode loads plugin `.ts` files from `.opencode/plugins/` (project) or `~/.config/opencode/plugins/` (global). Copy the plugin files and add command/agent entries to `opencode.json`:

`./setup-opencode.sh --global` (or `--project <path>`) does this copying for you and records what it installed so `--uninstall` can undo it; `--list` and `--dry-run` show what it would do. The manual recipe below is the same thing by hand.

```bash
# Project-local — copy the TypeScript plugin files into .opencode/plugins/
mkdir -p .opencode/plugins
cp plugin-{attribution,commit-guard,env-guard,markdown-format,memory-guard,token-saver,wandavision}/plugins/*.ts .opencode/plugins/

# Copy the command markdown files
mkdir -p .opencode/commands
cp plugin-{attribution,commit-guard,memory-guard,gh-issue-to-pr,wandavision}/commands/*.md .opencode/commands/

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

For the `wandavision` MCP server, run `/wandavision` after install — it prints the exact `docker build` and `opencode.json` `"mcp"` block to add (the MCP is the [jcchikikomori/mcp-vision fork](https://github.com/jcchikikomori/mcp-vision), not upstream).

### OpenCode compatibility

| Plugin | OpenCode support |
| ------ | ----------------- |
| `claude-attribution` | TypeScript hook (`tool.execute.before`) + `/claude-attribution` command |
| `commit-guard` | TypeScript hook with SHA256 approval + `/commit-guard` command |
| `env-guard` | TypeScript hook (`tool.execute.before`) — blocks `.env`/credential reads and secret-leaking bash; throws to block, mirrors the Claude Code PreToolUse hook |
| `markdown-format` | TypeScript hook (`tool.execute.after`) — runs `markdownlint-cli2 --fix` on `.md` writes (global binary → `npx` fallback), never throws |
| `memory-guard` | TypeScript hooks (`session.created` + `file.edited`) + cross-platform Python scripts + `/memory-guard` command |
| `token-saver` | TypeScript hooks (`session.created` + `tool.execute.after`) — compact-reminder + CLAUDE.md/AGENTS.md size guard; `prompt_quality` is surfaced as guidance only (OpenCode has no blocking pre-prompt event) |
| `gh-issue-to-pr` | `gh-issue-to-pr` subagent + `/gh-issue-to-pr` command |
| `wandavision` | TypeScript hook (`tool.execute.after`) + `/wandavision` command (MCP via `opencode.json`) |
| `opencode-migrate` | Claude Code-side skill — it *performs* the migration into OpenCode, so it stays installed in Claude Code rather than being ported |

The remaining Claude Code-only plugins are `dev` and `qa` (subagent definitions in Claude Code's `.md` format, not directly loadable by OpenCode) and the externally maintained `metronome`, `discover`, and `caveman`.

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
| `metronome` | behavior-control | Detects shortcut-taking and nudges Claude to proceed step by step |
| `discover` | product-quality | Turns feature ideas into evidence-backed PRDs through structured discovery |
| `caveman` | behavior-control | A plugin that makes agent talk like caveman |

The `dev` and `qa` plugins cover **workflow orchestration** — how to plan, build, and verify software using AI agents. Install the `skills` plugin from [skills-md](https://github.com/jcchikikomori/skills-md) for language/framework-specific rules (Ruby, Python, React, Node.js, Docker, etc.).

```bash
# Governance
/plugin install claude-attribution@claude-workflow

# Markdown auto-formatting
/plugin install markdown-format@claude-workflow

# Commit approval gate
/plugin install commit-guard@claude-workflow

# GitHub issue-to-PR workflow agent
/plugin install gh-issue-to-pr@claude-workflow

# Claude Code -> opencode migration
/plugin install opencode-migrate@claude-workflow

# External add-ons
/plugin install metronome@claude-workflow
/plugin install discover@claude-workflow

# Language/framework rules (from separate repo)
/plugin install skills@claude-workflow
```

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
├── LICENSE
└── README.md
```

Each plugin owns its agents and skills directly — no shared root directories, no symlinks.

---

## env-guard

`env-guard` prevents Claude from reading or leaking sensitive credential files (`.env`, SSH keys, cloud credentials, tokens). It works via a **PreToolUse hook** — the block happens before any tool call executes and cannot be overridden by prompt instructions.

It also ships a `secret-exposure-auditor` agent. Ask Claude to "audit for secrets" to scan a project for hardcoded keys, committed `.env` files, and `.gitignore` gaps.

```bash
/plugin install env-guard@claude-workflow
```

---

## claude-attribution

`claude-attribution` ensures every external post carries a visible AI-authorship line: **🤖 Written by Claude, reviewed by \<name\>**. It works with any MCP-connected platform — GitHub, JIRA, Confluence, Slack, or anything added later.

### How it works

- A **PreToolUse hook** matches `mcp__.*|Bash` — all MCP tools and CLI commands
- The hook scans `tool_input` for body-like fields (`body`, `content`, `message`, `comment`, `commentBody`, `description`, `text`)
- Posts missing the attribution line are **blocked before they are sent**
- A companion skill instructs Claude to **show the post to the user for approval** before sending

### Setup

```bash
/plugin install claude-attribution@claude-workflow

# Set your name (prompted on first use, or set manually)
echo "Your Name" > ~/.claude/claude-attribution-name.txt
```

### CLI coverage

The hook intercepts these Bash patterns:

| Pattern | Example |
| --------- | --------- |
| `gh pr create/comment/review/edit` | `gh pr comment 42 --body "..."` |
| `gh issue create/comment/edit` | `gh issue comment 1 --body "..."` |
| `gh api` with body field | `gh api repos/o/r/issues/1/comments -f body=...` |
| `curl` POST/PUT/PATCH | `curl -X POST -d '...'` |

> **Important:** Always include the attribution text **inline** in the body string. Do not use shell variable expansion (e.g., `${ATTR}`) to inject the attribution — the hook inspects raw command text before shell expansion.

---

## markdown-format

`markdown-format` silently fixes markdown lint errors in every `.md` file Claude writes or edits.
It works via a **PostToolUse hook** — `markdownlint-cli2 --fix` runs after each write and is
completely non-blocking. The companion skill also teaches Claude to write clean markdown from
the start.

### How it works

- A **PostToolUse hook** fires on `Write`, `Edit`, and `MultiEdit` for `.md` files
- Runs `markdownlint-cli2 --fix` with a bundled config tuned for LLM-generated markdown
- Accepts exit codes 0 and 1 (unfixable violations) as success — writes are never blocked
- Tries a globally installed `markdownlint-cli2` binary first; falls back to `npx markdownlint-cli2`

### Bundled config

| Rule | Setting | Reason |
| ---- | ------- | ------ |
| MD013 | disabled | LLMs don't wrap at 80 chars — enforcing creates noisy diffs |
| MD041 | disabled | Fragments and skill files legitimately lack a leading H1 |
| MD033 | disabled | Claude emits valid HTML (badges, `<details>`, table cells) |
| MD024 | `siblings_only` | Same-name headings allowed under different parents |

### Setup

```bash
/plugin install markdown-format@claude-workflow

# Optional: install globally to skip npx download overhead on first run
npm install -g markdownlint-cli2
```

To override rules for a specific project, place a `.markdownlint.json` in the project root —
`markdownlint-cli2` picks it up automatically and the bundled config is not applied.

---

## gh-issue-to-pr

`gh-issue-to-pr` ships a single agent that drives one GitHub issue from "reported" to "merged
and closed," in small, reversible steps, stopping hard at anything shared-state or
hard-to-reverse.

### Phases

Investigate to Plan to Branch (git-flow aware) to Scout for reusable code to Implement to Test
locally to Stage to draft a commit message and **stop** to (once pushed) open the PR to
self-review the diff to update the Test Plan checklist to merge (with confirmation) to close
the issue (with confirmation).

It always reads the target repo's own `CLAUDE.md`/`AGENTS.md`/`CONTRIBUTING.md` first and
defers to those conventions over its own defaults — it's portable across repos.

### Hard rules

- Never runs `git commit`, `git push`, `gh pr merge`, or `gh issue close` without the user
  explicitly approving that specific action in that turn
- Never force-pushes, never `--no-verify`, never skips hooks

### Setup

```bash
/plugin install gh-issue-to-pr@claude-workflow
```

Trigger with `/gh-issue-to-pr #42`, or natural language: "pick up issue #42", "work ticket #17
end to end", or resume mid-flow with "I've pushed, open the PR" / "checks are green, merge it".

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
