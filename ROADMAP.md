# Roadmap: opencode support

This fork is porting its plugins to [opencode](https://opencode.ai), so the same guards, advisors, agents and skills
work in both Claude Code and opencode. The work happens on the `feature/port-to-opencode` branch. Each phase lands as
its own commit, and this file is updated in each one.

Last updated: 2026-09-25.

## Status at a glance

| Phase | What it delivers | Status |
| --- | --- | --- |
| 0 | Test harness for the installer and the ports | Done |
| 1 | Installer core, and payload resolution for the existing ports | In progress (6 of 7 tasks) |
| 2 | Legacy file cleanup and the gh-issue-to-pr agent | Planned |
| 3 | `claude-attribution` becomes `ai-attribution` | Planned |
| 4 | Skill pipeline | Planned |
| 5 | Config snippets, `--merge`, and the LSP and MemPalace ports | Planned |
| 6 | dev and qa agents, recipes and commands | Planned |
| 7 | `--with-skills-md` | Planned |
| 8 | Docs | Planned |
| 9 | Full re-run of every test suite | Planned |
| 10 | Manual check in a real opencode install | Planned |
| 11 | Final quality pass | Planned |

"Done" means the work is finished and its tests pass locally. Phases 0 and 1 land together in one commit when Phase 1
ends.

## Plugin support on opencode

This is the target state. A plugin counts as supported only when its phase is done.

| Plugin | Target support | How | Phase |
| --- | --- | --- | --- |
| `commit-guard` | Full | TS plugin; runs the bundled Python, so it needs `python3` | 1 |
| `markdown-format` | Full | TS plugin running `markdownlint-cli2 --fix` after each edit | 1 |
| `token-saver` | Partial | opencode can't block a submitted prompt, so the vague-prompt check becomes a hint | 1 |
| `memory-guard` | Full | TS plugin; on opencode it watches `AGENTS.md` instead of `CLAUDE.md` | 1 |
| `gh-issue-to-pr` | Full | opencode agent and command, using opencode's `question` tool | 2 |
| `ai-attribution` | Full | Renamed from `claude-attribution`; the line names the model family in use | 3 |
| `env-guard` | Full | TS plugin | 4 |
| `wandavision` | Full | Existing TS plugin | 4 |
| `markdown-lsp` | Full | `lsp` config snippet for rumdl | 5 |
| `ruby-lsp` | Full | `lsp` config snippet, plus a TS plugin for Reek advice | 5 |
| `mempalace-docker` | Partial | MCP server, auto-mine and compaction context. The Stop and SessionEnd save hooks don't port | 5 |
| `dev` | Full | Agents converted at install time; recipes install as a skill and a command | 6 |
| `qa` | Full | Same as `dev`; `web-qa-reviewer` needs a `chrome-devtools` MCP server | 6 |
| `skills` (skills-md) | Full | Installed per skill by `setup-opencode.sh --with-skills-md` | 7 |
| `opencode-migrate` | Not a target | A Claude Code skill for migrating to opencode; only its reference docs are refreshed | 8 |
| `metronome`, `discover`, `caveman` | Out of scope | External plugins, maintained by their own authors | - |

## How the port works

- **One installer.** `setup-opencode.sh` installs everything, globally (`~/.config/opencode`) or into one project
  (`.opencode/`). Nothing is published to npm.
- **Install-time conversion.** Agent and skill files are converted from Claude Code format when you install them.
  Converted files are never committed. A small Python helper, `scripts/opencode/convert.py`, does the conversion,
  driven by `scripts/opencode/mapping.json`.
- **Payloads.** Scripts and config that a TS plugin needs are copied to `llm-agent-workflow/<plugin>/` under the
  opencode config dir. Every port finds them with the same resolver, and a test checks that each port's copy of it is
  byte-identical.
- **Guards fail closed; advisors never block.** If a guard (such as `commit-guard`) can't find its payload, it blocks.
  If an advisor (such as `token-saver`) can't, it logs one warning and carries on.
- **Tracked and reversible.** The installer records every file it writes, with a hash. `--uninstall` removes only
  the files it recorded. `--dry-run` shows every write first.
- **Config is never overwritten silently.** `lsp` and `mcp` entries are printed as snippets. `--merge` writes them
  into a plain `opencode.json`. An `opencode.jsonc` is never rewritten, so its comments survive.

## Phase details

### Phase 0: test harness

- [x] Test helpers, sandboxed homes and skeleton tests for every acceptance criterion

### Phase 1: installer core and payload resolution

- [x] 1.1 Converter CLI, `mapping.json` loader, and the repository guard
- [x] 1.2 Install tracker, version 2
- [x] 1.3 Installer unit loop, `--list`, dry-run, backups, and `--uninstall --plugin`
- [x] 1.4 `commit-guard` payload resolver, with the drift test
- [x] 1.5 `markdown-format` payload resolver and `package.json`
- [x] 1.6 `token-saver` payload resolver
- [ ] 1.7 `memory-guard` payload resolver, and one runtime setting for both tools

### Phase 2: legacy cleanup and gh-issue-to-pr

- [ ] 2.1 Remove files from older installs, and rename the gh-issue-to-pr agent

### Phase 3: ai-attribution

- [ ] 3.1 Rename `claude-attribution` to `ai-attribution`, with a model-aware attribution line

### Phase 4: skill pipeline

- [ ] 4.1 Shorter descriptions for dev, qa and env-guard, and the env-guard port
- [ ] 4.2 Shorter descriptions for the other five plugins
- [ ] 4.3 Frontmatter parser and validators
- [ ] 4.4 Skill install, with a duplicate-name check

### Phase 5: config snippets and LSP and MemPalace ports

- [ ] 5.1 Config snippets and `--merge`
- [ ] 5.2 `markdown-lsp` snippet
- [ ] 5.3 `ruby-lsp` port with Reek advice
- [ ] 5.4 `mempalace-docker` partial port

### Phase 6: dev and qa

- [ ] 6.1 Source fixes in dev and qa
- [ ] 6.2 Tool-name and vocabulary rewriter
- [ ] 6.3 Agent conversion, checked against golden files
- [ ] 6.4 Recipe commands and recipe access policy
- [ ] 6.5 qa coverage-quality port

### Phase 7: skills-md

- [ ] 7.1 `--with-skills-md`

### Phase 8: docs

- [ ] 8.1 Refresh the opencode-migrate reference docs
- [ ] 8.2 Support column in the README and `CLAUDE.md`
- [ ] 8.3 Commit message for the last phase

### Phases 9 to 11: verification

- [ ] 9.1 Re-run every test suite
- [ ] 10 Install into a scratch opencode project and try each plugin by hand
- [ ] 11 Final quality pass

## Breaking changes

### Phase 1: memory-guard state on Claude Code

memory-guard's state goes back to `~/.claude/.memory-guard`. A project whose remove-or-stash choice was saved in the
wrong place asks once more. The `memory-guard` changelog explains how to copy the old choices over.

### Phase 3: `claude-attribution` becomes `ai-attribution`

On Claude Code, reinstall the plugin under its new name:

```console
/plugin uninstall claude-attribution
/plugin install ai-attribution@llm-agent-workflow
```

Your saved attribution name is still read from its old location, so you won't be asked for it again.

### Phase 6: `testing-principles` moves to qa

`testing-principles` moves from `dev` to `qa`, so install `qa` alongside `dev`.

## Known limits

- opencode has no event that can block a prompt, so `token-saver`'s prompt check becomes a hint in the system prompt.
- opencode has no Stop or SessionEnd hook with access to the transcript, so MemPalace's save-on-stop doesn't port.
- opencode starts language servers only when the `lsp` key exists in its config, so the LSP plugins need their
  snippet.
- opencode's default MCP timeout is 5 seconds, which is too short for a Docker start. The MemPalace snippet sets 60
  seconds.

## How this work is done

The code for this port is generated by Claude (through Claude Code agents) and reviewed by the maintainer. Each task
gets its own tests, an independent test review with mutation testing (small deliberate bugs that the tests must
catch), and a quality pass before it is staged.

This fork tracks [shinpr/claude-code-workflows](https://github.com/shinpr/claude-code-workflows). The opencode port
is fork-only work.
