# Global migration — `~/.claude` → `~/.config/opencode`

Translation rules for a whole machine's Claude Code setup. Return to `SKILL.md` for the phase flow and the write gate; this file is the mapping.

## Contents

- [Discovery paths](#discovery-paths)
- [Where opencode config actually lives](#where-opencode-config-actually-lives)
- [settings.json → opencode config](#settingsjson--opencode-config)
- [Permissions](#permissions)
- [Model IDs](#model-ids)
- [CLAUDE.md → AGENTS.md](#claudemd--agentsmd)
- [Skills](#skills)
- [Commands](#commands)
- [Agents](#agents)
- [Hooks](#hooks)
- [MCP servers](#mcp-servers)
- [Memories and auto-memory](#memories-and-auto-memory)
- [Gap table](#gap-table)
- [Excluded from migration](#excluded-from-migration)
- [Validation and troubleshooting](#validation-and-troubleshooting)

## Discovery paths

Probe each one. Absence is data — record it, do not treat it as failure.

```bash
# Settings and keybindings
ls ~/.claude/settings.json ~/.claude/settings.local.json ~/.claude/keybindings.json 2>/dev/null

# Instructions
ls ~/.claude/CLAUDE.md ~/.claude/CLAUDE.local.md 2>/dev/null

# Assets
ls ~/.claude/skills/ ~/.claude/commands/ ~/.claude/agents/ ~/.claude/hooks/ 2>/dev/null

# Plugins and marketplaces
cat ~/.claude/plugins/installed_plugins.json 2>/dev/null
cat ~/.claude/plugins/known_marketplaces.json 2>/dev/null

# MCP servers (Claude Code keeps these in several places)
ls ~/.mcp.json ~/.claude.json 2>/dev/null
python3 -c "import json,os;p=os.path.expanduser('~/.claude/settings.json');d=json.load(open(p));print(list(d.keys()))" 2>/dev/null

# Plugin-managed memories, one directory per project
find ~/.claude/projects -type d -name memory 2>/dev/null
```

Counting matters more than listing. `ls ~/.claude/skills | wc -l` in the plan tells the user what scale of change they are approving.

## Where opencode config actually lives

| Scope | Path |
| --- | --- |
| Global | `~/.config/opencode/opencode.jsonc` **or** `opencode.json` |
| Relocated | whatever `OPENCODE_CONFIG` points at |
| Project | `opencode.jsonc` or `opencode.json` in the repo root, plus `.opencode/opencode.json(c)` |

Both extensions are real at both scopes — the binary looks for `opencode.json` and `opencode.jsonc` and walks up from the working directory to find them. **Prefer `.jsonc`**: a config that permits comments is a config whose permission rules can explain themselves, and matching whatever the user already has avoids the two-competing-configs failure below.

Precedence, lowest to highest: remote (`.well-known/opencode`) → global → `OPENCODE_CONFIG` → project → `.opencode` directories → `OPENCODE_CONFIG_CONTENT` → managed config → macOS managed preferences. Conflicting keys are overridden by the higher layer; non-conflicting keys merge.

Verified top-level keys:

`$schema`, `model`, `small_model`, `instructions`, `permission`, `agent`, `command`, `mcp`, `plugin`, `formatter`, `lsp`, `tools`, `share`, `theme`, `autoupdate`, `compaction`

Anything not in that list is either a nested key or does not exist — check the schema at `https://opencode.ai/config.json` before inventing one.

## settings.json → opencode config

| Claude Code | opencode | Notes |
| --- | --- | --- |
| `permissions.allow` | `permission` (per tool) | See [Permissions](#permissions) — do not blanket-allow |
| `permissions.deny` | `permission` (per tool, `"deny"`) | Always carry these across |
| `permissions.ask` | `permission` (per tool, `"ask"`) | Also the landing spot for anything ambiguous |
| `permissions.defaultMode` | no equivalent | opencode has no mode system; express intent per tool |
| `permissions.additionalDirectories` | `permission.external_directory` | Or launch opencode from the wider root |
| `model` | `model` | Provider prefix required |
| `modelSettings`, `effortLevel` | no equivalent | Thinking budgets are provider-side; note as a gap |
| `env` | `{env:VAR}` substitution | Reference, do not resolve |
| `hooks` | `plugin` event hooks | See [Hooks](#hooks) |
| `enabledPlugins` | `plugin` array + `.opencode/plugins/` files | Two different mechanisms; npm names in the array, local files on disk |
| `extraKnownMarketplaces` | no equivalent | No marketplace concept; distribute via npm or git |
| `statusLine` | no equivalent | opencode TUI is not configurable this way |
| `autoMemoryEnabled` | no equivalent | See [Memories](#memories-and-auto-memory) |
| `sandbox` | no equivalent | Docker, macOS sandbox profiles, or Linux seccomp, externally |
| `verbose`, `spinnerVerbs` | cosmetic, drop | Not worth a `manual` row |

`tools` is worth knowing even though Claude Code has no direct counterpart: it disables tools globally and lets an agent re-enable them.

```json
{
  "tools": { "github_*": false },
  "agent": {
    "build": { "tools": { "github_*": true } }
  }
}
```

That pattern is the closest thing opencode has to per-agent tool allow-lists, which is what `allowed-tools` in a Claude Code skill was doing.

## Permissions

opencode permission values are `"allow"`, `"ask"`, `"deny"`, either as a bare string per tool or as a wildcard map. **Last matching rule wins**, so order general-to-specific.

```json
{
  "permission": {
    "edit": "ask",
    "webfetch": "allow",
    "bash": {
      "*": "ask",
      "git status": "allow",
      "git diff *": "allow",
      "npm test": "allow",
      "rm *": "deny",
      "sudo *": "deny"
    }
  }
}
```

Translating Claude Code's granular `Bash(...)` rules is where migrations quietly go wrong. Claude Code entries like `Bash(git diff:*)` are command-scoped; the opencode equivalent is a wildcard key inside `bash`, not a global `"bash": "allow"`.

Rules that hold in every case:

- A `deny` in the source is a `deny` in the target. No exceptions, no "it was probably fine".
- Unmapped or ambiguous becomes `ask`. The cost of a wrong `ask` is one keystroke.
- Never widen scope. One command-scoped allow does not license the tool.
- Put `"*": "ask"` first inside a wildcard map, then the specific allows, then the specific denies last so they cannot be overridden by an earlier general rule.
- Claude Code's `Read`/`Edit` path-deny rules (gitignore-style patterns) have no target equivalent — that is a `manual` row, and the honest workaround is filesystem permissions or a container, not a config key.

## Model IDs

opencode requires a provider prefix and cannot switch models mid-session.

```json
{
  "model": "anthropic/claude-sonnet-4-5",
  "small_model": "anthropic/claude-haiku-4-5"
}
```

Where the user parameterises this, `{env:VAR}` works (`"model": "{env:OPENCODE_MODEL}"`), which keeps one config working across machines with different provider access.

`/model` mid-session does not exist. The practical replacement is per-agent models plus `Tab` to switch agents — worth saying out loud, because it is the change people notice first.

## CLAUDE.md → AGENTS.md

- **Run `/init` inside opencode** rather than hand-copying. It reads an existing `CLAUDE.md` (also `.cursorrules`, `.windsurfrules`) and generates a structured `AGENTS.md`.
- **`instructions[]` is for the extras**, not for `AGENTS.md` itself — see the discovery note below:

```jsonc
{
  "instructions": ["CONTRIBUTING.md", "docs/guidelines/*.md"]
}
```

- **`@imports` inside `CLAUDE.md` are not supported.** Each imported file becomes its own `instructions[]` entry.
- **`CLAUDE.local.md` → `AGENTS.local.md`**, gitignored, listed in `instructions[]`. For personal global overrides, `~/.config/opencode/AGENTS.md` is the counterpart of `~/.claude/CLAUDE.md`.

### What opencode discovers on its own

Verify this against the installed version rather than trusting a mapping table — it has changed, and the widely-circulated migration guides predate the change. On opencode 1.17.10 the binary resolves instruction files as:

- **Global:** `~/.config/opencode/AGENTS.md`, plus `~/.claude/CLAUDE.md` unless `OPENCODE_DISABLE_CLAUDE_CODE` is set.
- **Project:** walks *up* from the working directory collecting `AGENTS.md` — and `CLAUDE.md` too, under the same flag — unless `OPENCODE_DISABLE_PROJECT_CONFIG` is set.

So the old advice ("nothing is auto-discovered, list everything") is now wrong in a way that matters: a migration that copies `CLAUDE.md` into `AGENTS.md` and lists *both* in `instructions[]` can load the same guidance three times over. Check what the running version already picks up before adding entries.

Confirm it the cheap way — `opencode run -p "Quote the first line of AGENTS.md"` — rather than reasoning about it. If the answer is wrong, then look at `instructions[]`.

## Skills

opencode reads `.claude/skills/<name>/SKILL.md` natively, so skills are the cheapest part of the migration. Three options:

| Option | Command | Trade-off |
| --- | --- | --- |
| Leave in place | — | Zero work; both runtimes read one source |
| Symlink | `ln -s ~/.claude/skills ~/.config/opencode/skills` | One source of truth, but a `~/.claude` cleanup breaks opencode |
| Copy | `cp -r ~/.claude/skills ~/.config/opencode/skills` | Independent, and now you have two copies to keep in sync |

Symlink when the user is keeping both runtimes; copy when they are leaving Claude Code behind. Say which assumption you are making.

Frontmatter opencode does not support, and what each becomes:

| Field | Conversion |
| --- | --- |
| `model` | A dedicated agent with its own `model` |
| `allowed-tools` | An agent with a `permission` map, or a `tools` block |
| `context: fork` | A command with `"subtask": true` |
| `arguments` | A command using `$ARGUMENTS` or `$1`, `$2` |
| `disable-model-invocation` | Drop it — opencode skills load on request, not by pattern match |

That last row is the behavioural difference that matters: **opencode skills do not auto-invoke on description match.** The agent calls the skill tool when it decides the skill is relevant. A skill that relied on aggressive description-based triggering in Claude Code needs its trigger conditions restated in `AGENTS.md`, or it will simply never fire.

## Commands

```bash
cp -r ~/.claude/commands/. ~/.config/opencode/commands/
```

Template features: `$ARGUMENTS`, positional `$1`/`$2`, `` `!`cmd` `` for shell injection, `@filename` for file references. Frontmatter supports `description`, `agent`, `model`, and `subtask`.

The JSON form is equivalent, useful when the command is short enough not to deserve a file:

```json
{
  "command": {
    "deploy": {
      "template": "Deploy to $ARGUMENTS after running tests",
      "description": "Deploy to a target environment",
      "agent": "build",
      "subtask": true
    }
  }
}
```

Claude Code frontmatter fields with no counterpart — `allowed-tools`, `argument-hint` — get dropped; fold anything load-bearing into the body text.

## Agents

`~/.claude/agents/*.md` → `~/.config/opencode/agents/*.md`, with a different frontmatter shape:

| Claude Code | opencode |
| --- | --- |
| `name` | dropped — the filename is the identity |
| `description` | `description` |
| `tools: Read, Grep, Bash` | a `permission` map: `{ edit, bash, webfetch }` |
| `model: inherit` | dropped, or an explicit provider-prefixed model |
| — | `mode: primary` \| `subagent` \| `all` (required) |

Agents can also be declared inline, which is the only way to set `steps`:

```json
{
  "agent": {
    "security-reviewer": {
      "description": "Reviews code for OWASP Top 10 issues",
      "mode": "subagent",
      "prompt": "{file:./agents/security-reviewer.md}",
      "permission": { "edit": "deny", "bash": "deny" },
      "steps": 25
    }
  }
}
```

A file dropped into `agents/` is not necessarily enough — if the agent needs permissions or a step budget, it needs a config entry too. Check both.

## Hooks

Claude Code's lifecycle hooks (shell commands keyed by event) become opencode **plugin event hooks** — TypeScript modules in `~/.config/opencode/plugins/`. That is a rewrite, not a copy, and it is covered in `plugin-port.md`. Read that file rather than duplicating the mapping here.

Two shortcuts worth taking before writing any plugin, because they replace the two most common hooks outright:

```json
{
  "formatter": {
    "prettier": {
      "command": ["npx", "prettier", "--write", "$FILE"],
      "extensions": [".ts", ".tsx", ".js", ".jsx"]
    }
  },
  "permission": { "bash": { "rm -rf *": "deny", "sudo *": "deny" } }
}
```

A format-on-write hook becomes a `formatter` entry, and a dangerous-command guard becomes a `deny` pattern. Neither needs code.

## MCP servers

Claude Code's `.mcp.json` becomes the `mcp` key.

| Aspect | Claude Code | opencode |
| --- | --- | --- |
| Local transport | `"type": "stdio"` with `command` + `args` | `"type": "local"` with `command` as one array |
| Remote transport | `"type": "http"` | `"type": "remote"` |
| Env substitution | `${VAR}` | `{env:VAR}` |
| Enable/disable | omit the entry | `"enabled": false` |
| OAuth | limited | `clientId`, `clientSecret`, `scope` |

```json
{
  "mcp": {
    "github": {
      "type": "remote",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": { "Authorization": "Bearer {env:GITHUB_TOKEN}" },
      "enabled": true
    },
    "local-db": {
      "type": "local",
      "command": ["npx", "-y", "@bytebase/dbhub"],
      "enabled": true
    }
  }
}
```

Two traps: merging `command` and `args` into a single array is easy to get subtly wrong when `args` contains flags with values, so translate element by element; and a server that fails to start takes its tools with it silently, so check availability from inside a session rather than assuming.

If the user already keeps MCP servers in a separate file that their config pulls in, add to that file — do not introduce a competing `mcp` block.

## Memories and auto-memory

Claude Code's file-based memories live at `~/.claude/projects/<slug>/memory/`, where the slug is the project path with `/` replaced by `-`. There is no auto-memory in opencode, so this is a `manual` row every time.

```bash
find ~/.claude/projects -type d -name memory 2>/dev/null
cat ~/.claude/projects/*/memory/MEMORY.md 2>/dev/null
```

The workable replacement is curation, not conversion: read the memory files, keep the durable conventions (build commands, debugging patterns, decisions with reasons), and fold them into the relevant `AGENTS.md`. Session-specific notes should be dropped rather than migrated — a memory that says "the user is currently debugging X" is noise in an instructions file.

Then make the maintenance explicit, because nothing will do it automatically any more: add "updated `AGENTS.md` if new conventions were discovered" to the review checklist.

If the user runs a semantic memory MCP server, it migrates as an MCP entry and keeps working — worth checking for before hand-curating anything.

## Gap table

| Claude Code feature | opencode status | Workaround |
| --- | --- | --- |
| Agent teams / multi-session coordination | Not available | Parallel sessions writing to a shared task file; external orchestration if the DAG is real |
| Auto-memory | Not available | Curate into `AGENTS.md`; add it to code review |
| OS-level sandboxing | Not available | Docker, macOS sandbox profiles, Linux seccomp |
| `CLAUDE.md` auto-discovery / directory walking | Available as of 1.17.10 — `AGENTS.md` (and `CLAUDE.md`) are collected walking up the tree | Verify on the installed version; use `instructions[]` only for files outside that set |
| Path-scoped rules | Not available | Flatten into `AGENTS.md`, or a per-area agent, or a command |
| Read/Edit path deny patterns | Not available | Filesystem permissions or a container |
| Skill auto-invocation by description | Partial — agent calls the skill tool | State trigger conditions in `AGENTS.md` |
| Extended thinking controls | Partial — provider-dependent | A dedicated agent with a high `steps` budget |
| Mid-session model switch | Partial — restart or switch agents | Per-agent models, `Tab` to switch |
| Plugin marketplace | Not available | npm or git distribution; document what the team should install |
| Plugin namespacing | Not available | Prefix command names to avoid collisions |
| Hooks as shell commands | Replaced | Plugin event hooks, `formatter`, or `permission` denies |

Gaps belong in the plan and in `migration-report.md`, not only in conversation — they are the part the user will need again in three months.

## Excluded from migration

Never copy these without an explicit request, and if asked, explain the cost first:

| Path | Why |
| --- | --- |
| `~/.claude/credentials.json`, `.credentials.json` | Live auth tokens. Re-authenticate with `/connect`; rotate if they ever left the machine |
| `~/.claude/history.jsonl`, `transcripts/`, `sessions/` | Conversation content — large, personal, not configuration |
| `~/.claude/session-env/`, `shell-snapshots/` | Captured shell environments, frequently containing exported tokens |
| `~/.claude/plugins/cache/` | Thousands of files; reinstall from source |
| `~/.claude/file-history/`, `paste-cache/`, `downloads/` | Local artifacts, not portable |
| `~/.claude/telemetry/`, `stats-cache.json` | Machine-local counters |

## Validation and troubleshooting

```bash
opencode run -p "reply with OK"                     # config parses and a provider answers
opencode run -p "List available skills"             # skills resolved
opencode run -p "What are my current permissions?"  # permission map as intended
opencode run -p "Quote the first line of AGENTS.md" # instructions actually loaded
```

| Symptom | Likely cause |
| --- | --- |
| opencode will not start | Malformed config. Validate the JSON/JSONC; a trailing comma in `.json` is fatal |
| Instructions ignored | File exists but is missing from `instructions[]`, or you wrote `opencode.json` while `opencode.jsonc` is the live file |
| Model not found | Missing provider prefix, or the provider is not connected — run `/connect` |
| MCP tools missing | Server failed to start; check `type` (`local` vs `remote`) and that `command` is a single array |
| Permission rules ignored | Wildcard ordering — last match wins, so a later general rule overrode a specific one |
| Skills never fire | Expected. Skills do not auto-invoke; state the trigger in `AGENTS.md` |
| Plugin not loading | Wrong directory, or an npm package name missing from the `plugin` array |

## Post-migration checklist

1. Start opencode; confirm it launches without config errors.
2. `/connect` for each provider the old setup used.
3. `/init` if `AGENTS.md` does not exist yet.
4. Verify instructions, skills, commands, agents, and MCP tools resolve from inside a session.
5. Walk the gap list and decide, per gap, whether to implement the workaround or accept the loss.
6. Keep the Claude Code setup intact until the user says the migration is settled.
