# Project migration — one repository

Translation rules for a single repo's Claude Code configuration. Return to `SKILL.md` for the phase flow and the write gate.

A project migration differs from a global one in ways that change the plan, not just the paths: the output gets **committed**, so other people inherit it; the repo may be shared with teammates who still use Claude Code; and a mistake here leaks into git history rather than sitting in one user's home directory. Default to additive, reviewable changes.

## Contents

- [Discovery](#discovery)
- [CLAUDE.md → AGENTS.md](#claudemd--agentsmd)
- [Path-scoped rules](#path-scoped-rules)
- [Settings → project opencode.json](#settings--project-opencodejson)
- [Assets: agents, commands, skills](#assets-agents-commands-skills)
- [Hooks](#hooks)
- [MCP servers](#mcp-servers)
- [What to commit](#what-to-commit)
- [Coexistence with Claude Code](#coexistence-with-claude-code)
- [Verification](#verification)

## Discovery

```bash
ls CLAUDE.md CLAUDE.local.md AGENTS.md opencode.json opencode.jsonc 2>/dev/null
ls -R .claude/ 2>/dev/null | head -40
ls .mcp.json 2>/dev/null
ls -d .opencode/ 2>/dev/null
git check-ignore -v CLAUDE.local.md .claude/settings.local.json 2>/dev/null
grep -n 'claude\|CLAUDE' .gitignore 2>/dev/null
```

Two answers shape the whole plan, so establish them first:

1. **Does `AGENTS.md` already exist?** Many repos have one for other tools. If so, the migration merges into it rather than generating a competing document.
2. **Is `.claude/` committed or ignored?** A committed `.claude/` means teammates depend on it, so removing it is out of scope; an ignored one means the config was personal all along and the opencode side should probably be ignored too.

## CLAUDE.md → AGENTS.md

`AGENTS.md` at the repo root is the direct counterpart. The mechanical part is easy; the judgement calls are these:

- **Prefer `/init` inside opencode** over hand-copying. It reads the existing `CLAUDE.md` and produces a structured `AGENTS.md`, which also strips Claude-Code-specific phrasing that would confuse a different runtime.
- **`@imports` do not work.** Every `@docs/foo.md` in the source becomes its own entry in `instructions[]`. Missing this is the single most common reason a migrated repo behaves as if it has no instructions at all.
- **`CLAUDE.local.md` → `AGENTS.local.md`**, gitignored, listed in `instructions[]`.
- **Runtime-specific content should be rewritten, not carried.** Instructions that name Claude Code tools (`AskUserQuestion`, `MultiEdit`), plan mode, or `/`-commands that do not exist in opencode are actively misleading once migrated. Flag each as a `manual` row rather than translating it silently — the author knows what they meant.
- **Keeping both files is legitimate.** If the team still uses Claude Code, `CLAUDE.md` stays and `AGENTS.md` is added. Duplication is the cost; a symlink (`ln -s CLAUDE.md AGENTS.md`) removes it at the price of runtime-specific wording, so only suggest that when the content is genuinely runtime-neutral.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": ["AGENTS.md", "AGENTS.local.md", "docs/architecture.md", "CONTRIBUTING.md"]
}
```

## Path-scoped rules

Claude Code can scope rules to paths (`.claude/rules/` with path globs, or nested `CLAUDE.md` files in subdirectories). opencode has no path *scoping* — it collects instruction files walking up from the working directory, but every file it finds applies to the whole session. Three honest options:

| Strategy | Fits | Cost |
| --- | --- | --- |
| Flatten into `AGENTS.md` | Small and medium repos | Every rule is always in context, whether or not it is relevant |
| One agent per area | Monorepos with distinct stacks | The user must pick the right agent; nothing enforces it |
| One command per area | Task-shaped rules ("when touching migrations…") | Only applies when the command is invoked |

Nested `CLAUDE.md` files in subdirectories are the case people forget. Claude Code loads one when you work inside that subtree; opencode's upward walk starts at the working directory, so it picks up ancestors of where the session was launched and nothing below. Launch location decides what applies, which is not something a config file can express — so flatten, use an agent, or accept that a nested file loads for every task in that subtree and never outside it.

State the trade-off you chose and why. A migration that silently flattens twelve scoped rulesets into one instructions file has quietly tripled the context cost of every request in that repo.

## Settings → project opencode.json

`.claude/settings.json` (team) and `.claude/settings.local.json` (personal) both land in the project `opencode.json`, but they should not land the same way: project config is committed, so anything personal belongs in the user's global config or an ignored file instead.

Project config **overrides** global for conflicting keys and merges for the rest. That makes the project file the right home for repo-specific permissions, MCP servers, and instructions — and the wrong home for the user's model choice or provider credentials.

The key mapping is the same as the global one; see `global.md` for the full table, permission semantics (last-match-wins), and `{env:VAR}` substitution. The project-specific additions:

```jsonc
// opencode.jsonc — prefer .jsonc over .json so the rules can carry their reasons
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "bash": {
      "*": "ask",
      "npm test": "allow",
      "npm run lint": "allow",
      "git status": "allow",
      "git diff *": "allow"
    }
  },
  "formatter": {
    "prettier": { "command": ["npx", "prettier", "--write", "$FILE"], "extensions": [".ts", ".tsx"] }
  }
}
```

If the repo runs its tooling in Docker, the allow-list should name the Docker invocation (`docker compose run --rm *`), not the host binaries — otherwise every real command still prompts and the allow-list is decoration.

## Assets: agents, commands, skills

| Source | Destination | Notes |
| --- | --- | --- |
| `.claude/agents/*.md` | `.opencode/agents/*.md` | Rewrite frontmatter: drop `name`/`tools`/`model`, add `mode`, add a `permission` map |
| `.claude/commands/*.md` | `.opencode/commands/*.md` | Body syntax is compatible; drop `allowed-tools` and `argument-hint` |
| `.claude/skills/*/SKILL.md` | leave in place | opencode reads `.claude/skills/` natively — copying creates a second source of truth |

Leaving skills where they are is the right default for a shared repo: teammates on Claude Code keep working, and there is one file to review when a skill changes. Copy into `.opencode/skills/` only when the repo is dropping Claude Code entirely.

Frontmatter details for agents and commands are in `global.md`; they are identical at project scope.

## Hooks

`.claude/hooks/` (a `hooks.json` plus scripts) becomes `.opencode/plugins/*.ts`. That is a rewrite — read `plugin-port.md` for the event mapping and module shape.

Before writing any TypeScript, check whether the hook is one of the two cases that need no code at all:

- **Format or lint after edit** → a `formatter` entry in `opencode.json`.
- **Block a dangerous command** → a `permission` deny pattern.

Those two cover a large share of real project hooks, and a config entry beats a plugin nobody will maintain.

Note the shape difference for anything that remains: project plugins live in `.opencode/plugins/` and load automatically, so a committed plugin runs for every teammate who opens the repo in opencode. That is a change in blast radius worth flagging in the plan — a hook that was one developer's preference becomes team policy.

## MCP servers

`.mcp.json` → the `mcp` key in the project `opencode.json`, with the same transport translation as global (`stdio` → `local` with a single `command` array; `http` → `remote`).

Two project-specific cautions:

- **Never inline a token.** `${GITHUB_TOKEN}` becomes `{env:GITHUB_TOKEN}`, and the value stays in the environment. This file gets committed; a resolved secret here is a secret in git history.
- **`${CLAUDE_PLUGIN_ROOT}` and similar runtime variables do not exist.** A server whose command depended on them needs an absolute path, a relative path from the repo root, or a wrapper script committed alongside.

## What to commit

| File | Commit? | Why |
| --- | --- | --- |
| `AGENTS.md` | Yes | It is the point of the migration |
| `opencode.json` | Yes | Repo-scoped permissions, instructions, MCP, formatter |
| `.opencode/agents/`, `.opencode/commands/` | Yes | Shared team assets |
| `.opencode/plugins/*.ts` | Yes, deliberately | Runs for every teammate — decide that on purpose |
| `AGENTS.local.md` | No | Personal overrides |
| `.opencode/**/*.local.*`, local state | No | Machine-specific |
| Anything with a resolved credential | No | Never |

Suggested `.gitignore` delta, mirroring whatever the repo already does for `.claude`:

```gitignore
AGENTS.local.md
.opencode/**/*.local.*
*.bak-*
```

The `*.bak-*` line matters more than it looks: the timestamped backups this skill writes land next to the file they back up, so inside a repo they are committable by default. A repo that already ignores `*.bak` will not match `*.bak-20260101T000000Z`. Either add the pattern or write backups outside the working tree.

If `.claude/settings.local.json` was ignored, the opencode equivalent should be ignored too — carrying the pattern across keeps the same personal/shared boundary the team already agreed on.

## Coexistence with Claude Code

Most repos will run both for a while, and that is fine as long as nobody has to keep two documents in sync by hand. What works:

- Keep skills in `.claude/skills/` — both runtimes read them.
- Keep hooks and plugins separate; they cannot share an implementation, so expect two files with a shared helper if the logic is non-trivial.
- Keep one instructions document if the content is runtime-neutral (symlink), and two if it is not.
- Add a line to `CONTRIBUTING.md` saying which files matter for which runtime. The next person will not infer it.

Because opencode has no auto-memory, conventions discovered while working no longer record themselves. The replacement is a review checklist item — "updated `AGENTS.md` if new conventions were discovered" — and it only works if it is written down where reviewers look.

## Verification

```bash
python3 -c "import json;json.load(open('opencode.json'));print('config OK')"
opencode run -p "Quote the first line of AGENTS.md"
opencode run -p "List available skills"
git status --short          # only the files in the approved plan
git diff --cached           # read it before suggesting a commit
```

Then check the two things a config parser cannot:

1. **No secrets staged.** `git diff --cached | grep -iE 'token|secret|password|api[_-]?key'` and read every hit.
2. **No stale runtime references.** `grep -rn 'CLAUDE\|MultiEdit\|plan mode' AGENTS.md .opencode/ 2>/dev/null` — anything left is either intentional coexistence documentation or a translation you missed.

Generate the commit message; leave the commit itself to the user.
