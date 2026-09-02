# opencode-migrate

Migrates a Claude Code setup into [opencode](https://opencode.ai) — a whole machine, a single repository, or a Claude Code plugin's own source — behind a plan-then-approve gate.

Translation mapping derived from [claudecode2opencode](https://lopince.github.io/claudecode2opencode/) and the current [opencode configuration docs](https://opencode.ai/docs/config/), plus the seven Claude Code → opencode plugin ports already living in this repo.

## What it does

One skill, `opencode-migrate`, with three modes. The skill routes to exactly one reference file so a migration never carries the other two domains in context.

| Mode | Source | Destination | Reference |
| ---- | ------ | ----------- | --------- |
| `global` | `~/.claude` | `~/.config/opencode` | `references/global.md` |
| `project` | `CLAUDE.md`, `.claude/`, `.mcp.json` | `AGENTS.md`, `opencode.jsonc`, `.opencode/` | `references/project.md` |
| `plugin-port` | `plugin.json`, `hooks/hooks.json`, `hooks/*.py` | `package.json`, `plugins/opencode-*.ts`, `commands/*.md` | `references/plugin-port.md` |

### Why it plans before writing

A config migration rewrites the files that decide what an agent may do without asking, and it is hard to reverse. So the flow is:

1. **Discover** — probe, count, record; read-only.
2. **Plan** — one row per artifact, each with exactly one action (`create` / `merge` / `skip` / `manual`), plus the gap list, the security exclusions, and the backup paths.
3. **Gate** — `AskUserQuestion`, with apply-all, apply-a-subset, and plan-only as real options.
4. **Apply** — timestamped backups, key-level JSON merges, comment-preserving edits for `.jsonc`, copy rather than move.
5. **Verify** — re-read what was written, diff against the approved plan, report drift, write `migration-report.md`.

### What it refuses to migrate

Credentials, conversation history, transcripts, captured shell environments, plugin caches, and local file history — in every mode, unless explicitly asked. Environment references (`${VAR}` → `{env:VAR}`) are translated, never resolved, so a token cannot land in a committed config.

### How permissions translate

Conservatively, on purpose. A `deny` always survives; anything ambiguous becomes `ask` rather than `allow`; a command-scoped allow never widens to the whole tool. opencode wildcard rules are last-match-wins, so the reference spells out the ordering that behaves as expected — getting it backwards silently grants everything and looks fine in the config.

## Install

```bash
/plugin install opencode-migrate@llm-agent-workflow
/reload-plugins
```

## Conflicts with skills-md

`skills-md:claudecode-migrate` covers the same global migration from the same source mapping. This skill supersedes it — same translation tables, plus project migration, plugin porting, and a write gate.

Step 0 of the skill checks for the skills-md copy and prints a one-line reminder when it finds one. It does not block. Disable the skills-md skill when you are ready, and the reminder stops appearing.

## Version History

### 1.0.0

- Major release for repository rename to `llm-agent-workflow`
- Updated install target to `opencode-migrate@llm-agent-workflow`
- No migration-behavior changes

### 0.1.0

- Initial release
- `opencode-migrate` skill with three modes (`global`, `project`, `plugin-port`) and a plan-then-approve gate
- `references/global.md` — settings, permissions, model IDs, instructions, skills, commands, agents, MCP, memories, gap table, troubleshooting
- `references/project.md` — `AGENTS.md`, project config precedence, path-scoped rule strategies, what to commit, Claude Code coexistence
- `references/plugin-port.md` — hook event mapping, TypeScript module skeleton, translation idioms, and a 17-item verification checklist, all cited against the repo's existing ports
- Conflict reminder for `skills-md:claudecode-migrate`

Corrections applied after the first eval round, verified against opencode 1.17.10 rather than the published mapping tables:

- **`AGENTS.md` *is* auto-discovered.** The binary collects `AGENTS.md` walking up from the working directory, and reads `~/.config/opencode/AGENTS.md` plus `~/.claude/CLAUDE.md` unless `OPENCODE_DISABLE_CLAUDE_CODE` / `OPENCODE_DISABLE_PROJECT_CONFIG` are set. The older "nothing is discovered, list everything" advice can triple-load the same guidance, so `instructions[]` is now documented as the extras list.
- **`.jsonc` preferred at both scopes.** `opencode.json` and `opencode.jsonc` are both resolved, project root included; the skill now proposes `.jsonc` so permission rules can carry their reasons in comments.
- **Unprefixed `package.json` names.** New ports use `env-guard`, not `opencode-env-guard`.
- **Backups are gitignore-aware.** `*.bak-*` added to the project `.gitignore` delta — an existing `*.bak` rule does not match a timestamped backup.
- **Path-remap rule disambiguated.** Remap a path the plugin reads or writes; leave a path it is only describing to the user.
