# My LLM Agent Workflow

[![Claude Code](https://img.shields.io/badge/Claude%20Code-Plugin-purple)](https://claude.ai/code)
[![GitHub Stars](https://img.shields.io/github/stars/jcchikikomori/llm-agent-workflow?style=social)](https://github.com/jcchikikomori/llm-agent-workflow)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/jcchikikomori/llm-agent-workflow/pulls)

Agent-first workflows for [Claude Code](https://claude.ai/code) and [OpenCode](https://opencode.ai).
This fork of [shinpr/claude-code-workflows](https://github.com/shinpr/claude-code-workflows)
adds QA-focused workflows, security guardrails, and cross-platform plugin support.

## Release Note

`llm-agent-workflow` is the renamed major release baseline for this repository.

- Marketplace install target is now `@llm-agent-workflow`.
- Core fork-qualified plugins (`dev`, `qa`, `env-guard`) moved to `1.0.0-jcc.1`.
- Independently versioned plugins moved to `1.0.0`.

## Tag Policy

To keep releases predictable and avoid accidental major-version drift:

- The canonical repository tag baseline is `v1.0.0` for this rename release.
- Do not create `v2.x` tags unless there is an intentional breaking-change release.
- For fork-qualified plugin releases, follow `v<upstream>-jcc.<n>` tags (for example, `v1.0.0-jcc.1`).
- For independently versioned plugins, follow plain SemVer tags (for example, `v1.0.0`).

## Quick Start

```bash
# clone marketplace repo
git clone https://github.com/jcchikikomori/llm-agent-workflow <install-path>

# inside Claude Code
/plugin marketplace add <install-path>
/plugin install dev@llm-agent-workflow
/plugin install qa@llm-agent-workflow
/plugin install env-guard@llm-agent-workflow
/reload-plugins

# run a workflow
/recipe-implement "Add user authentication with JWT"
```

Platform support: macOS, Linux, WSL, native Windows.

## Plugin Catalog

| Plugin | Category | Purpose |
| ------ | -------- | ------- |
| `skills-md` | skills | Language/framework coding rules (Ruby, Python, React, Node.js, Docker, more) |
| `dev` | agentic-coding | Main development workflows for design, planning, implementation, and review |
| `qa` | product-quality | Acceptance test generation, integration/E2E testing, browser QA |
| `env-guard` | behavior-control | Blocks accidental `.env` and secret exposure |
| `claude-attribution` | governance | Anti-AI-slop + attribution governance on external posts and commits |
| `markdown-format` | quality-enforcement | Auto-fixes markdown lint issues after writes |
| `commit-guard` | behavior-control | Adds user approval gate before `git commit` |
| `gh-issue-to-pr` | workflow-orchestration | Drives one GitHub issue from investigation to PR lifecycle |
| `memory-guard` | behavior-control | Watches sensitive docs and routes changes to memory + remove/stash policy |
| `token-saver` | behavior-control | Enforces token-efficient prompting and session hygiene |
| `opencode-migrate` | workflow-orchestration | Migrates Claude Code setup into OpenCode |
| `mempalace-docker` | behavior-control | Runs MemPalace through Docker with GPU-aware runtime selection |
| `wandavision` | quality-enforcement | Deterministic image analysis via `mcp-vision` |
| `metronome` | behavior-control | Keeps workflows procedural and step-driven |
| `discover` | product-quality | Turns ideas into evidence-backed PRDs |
| `caveman` | behavior-control | Token-light response style plugin |

## How It Works

### Development Workflow

| Stage | Primary Agents | Output |
| ----- | -------------- | ------ |
| Intake | `requirement-analyzer` | Scope and workflow path (small/medium/large) |
| Discovery | `codebase-analyzer`, `prd-creator` | Context and requirements for implementation |
| Design | `technical-designer`, `ui-spec-designer` | Architecture and testable design docs |
| Planning | `work-planner`, `task-decomposer` | Ordered, commit-ready tasks |
| Execution | `task-executor`, `task-executor-frontend` | Working code per task |
| Quality | `quality-fixer`, `code-verifier`, `code-reviewer` | Test/lint/type fixes and doc-to-code verification |

### Diagnosis Workflow

| Stage | Primary Agents | Output |
| ----- | -------------- | ------ |
| Investigate | `investigator` | Execution-path map and failure candidates |
| Validate | `verifier` | Confirmed root causes with evidence |
| Solve | `solver` | Tradeoff-based fix options and action steps |

## Workflow Recipes

All entry points use `/recipe-*`.

### Development Recipes (`dev`)

| Recipe | Purpose |
| ------ | ------- |
| `/recipe-implement` | End-to-end feature development |
| `/recipe-task` | Focused change or bug fix |
| `/recipe-design` | Produce design documentation |
| `/recipe-plan` | Convert design docs to task plan |
| `/recipe-build` | Execute an existing task plan |
| `/recipe-review` | Validate code against design docs |
| `/recipe-diagnose` | Root-cause investigation and solution path |
| `/recipe-reverse-engineer` | Generate PRD/design docs from existing code |
| `/recipe-pr-review` | Review an external PR with local codebase context |

### Fullstack and Frontend Recipes (`dev`)

| Recipe | Purpose |
| ------ | ------- |
| `/recipe-fullstack-implement` | End-to-end backend + frontend delivery |
| `/recipe-fullstack-build` | Build from an existing fullstack plan |
| `/recipe-front-design` | UI spec + frontend design docs |
| `/recipe-front-plan` | Frontend task planning |
| `/recipe-front-build` | Frontend implementation from plan |
| `/recipe-front-review` | Frontend review against design docs |

### QA Recipes (`qa`)

| Recipe | Purpose |
| ------ | ------- |
| `/recipe-add-integration-tests` | Add integration/E2E tests to existing features |
| `/recipe-web-qa` | Browser-level QA on running web apps |

## Specialized Agents

### `plugin-dev` (27 agents)

| Capability | Agents |
| ---------- | ------ |
| Intake and scoping | `requirement-analyzer`, `scope-discoverer` |
| Codebase and requirements | `codebase-analyzer`, `prd-creator`, `rule-advisor` |
| System and UI design | `technical-designer`, `technical-designer-frontend`, `ui-spec-designer`, `design-sync` |
| Planning and decomposition | `work-planner`, `task-decomposer` |
| Implementation | `task-executor`, `task-executor-frontend` |
| Quality and review | `quality-fixer`, `quality-fixer-frontend`, `code-verifier`, `code-reviewer`, `pr-reviewer`, `security-reviewer`, `document-reviewer` |
| Diagnostics | `investigator`, `verifier`, `solver` |
| Knowledge and project memory | `context-keeper`, `context-scouter`, `claude-md-generator`, `pr-creator` |

### `plugin-qa` (4 agents)

| Capability | Agents |
| ---------- | ------ |
| Test generation | `acceptance-test-generator`, `api-endpoint-tester` |
| Test quality review | `integration-test-reviewer` |
| Browser QA | `web-qa-reviewer` |

## OpenCode

OpenCode-compatible files are included for supported plugins (`plugins/*.ts`,
`commands/*.md`, and selected custom agents). Use:

```bash
./setup-opencode.sh --global
```

Use `--project <path>` for project-local install and `--dry-run` to preview.

## Detailed Plugin Docs

Deep setup and internals live in each plugin README:

- [`plugin-wandavision/README.md`](plugin-wandavision/README.md)
- [`plugin-mempalace-docker/README.md`](plugin-mempalace-docker/README.md)
- [`plugin-memory-guard/README.md`](plugin-memory-guard/README.md)
- [`plugin-opencode-migrate/README.md`](plugin-opencode-migrate/README.md)
- [`plugin-token-saver/README.md`](plugin-token-saver/README.md)
- [`plugin-attribution/README.md`](plugin-attribution/README.md)
- [`plugin-commit-guard/README.md`](plugin-commit-guard/README.md)
- [`plugin-markdown-format/README.md`](plugin-markdown-format/README.md)
- [`plugin-gh-issue-to-pr/README.md`](plugin-gh-issue-to-pr/README.md)

## Recommended Permissions

Pre-approve common inspection commands in `~/.claude/settings.json` to avoid
constant permission prompts:

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

For inline PR review posting, include GitHub MCP write permissions too.

## Contributing

For marketplace/plugin contribution rules, see `CONTRIBUTING.md`.

## License

MIT. See `LICENSE`.

Built and maintained by [@jcchikikomori](https://github.com/jcchikikomori).
