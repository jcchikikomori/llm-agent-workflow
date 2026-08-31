---
name: opencode-migrate
description: Migrate a Claude Code setup into opencode (opencode.ai) — global ~/.claude config, a project's CLAUDE.md and .claude directory, or a Claude Code plugin's hooks ported to opencode TypeScript plugins. Produces a reviewable migration plan first and writes nothing until the user approves it. Use this skill whenever the user mentions opencode, AGENTS.md, opencode.json or opencode.jsonc, "move/switch/migrate my Claude Code setup", "convert CLAUDE.md", "port this plugin to opencode", "translate .mcp.json", or asks how a Claude Code hook, skill, agent, command, or permission maps onto opencode — even if they never say the word "migrate".
---

# Claude Code → opencode Migration

Two runtimes, similar concepts, incompatible plumbing. Claude Code discovers configuration by walking directories and merging implicit defaults; opencode wants everything declared explicitly in one file. That single difference causes most migration failures — not bad translations, but silent omissions, because opencode never goes looking for what you forgot to list.

This skill exists to make the migration reviewable *before* it happens. A config migration rewrites the files that decide what an agent is allowed to do, and it is hard to reverse. So the flow is discover → plan → human gate → apply → verify. Never a straight overwrite.

## Reasoning shape (why the phases are arranged this way)

| Phase | Reasoning pattern | Why that one |
| --- | --- | --- |
| Discover | Observation only | Probing is cheap; assuming a path exists is what breaks migrations |
| Plan | Plan-and-Execute | Long-horizon and hard to reverse, so the whole strategy is settled before the first write |
| Gate | Human-in-the-loop | The blast radius is the user's tool permissions — not a call to make alone |
| Apply | Sequential, idempotent | Deterministic steps, each backed up, each safe to re-run |
| Verify | ReAct, then self-critique | Act, observe, compare against the approved plan, report drift honestly |

If you are writing files during Discover or Plan, the gate has collapsed. Stop and back up a phase.

## Step 0 — Conflict check (before anything else)

A separate skill, `claudecode-migrate` (shipped in the `skills-md` plugin), covers the same global migration derived from the same source mapping. Two overlapping skills means two sets of instructions competing for one task, and whichever loads first wins by accident rather than intent.

Check for it:

```bash
grep -l 'skills-md@' ~/.claude/plugins/installed_plugins.json 2>/dev/null
ls -d ~/.config/opencode/skills/claudecode-migrate 2>/dev/null
ls -d ~/.claude/plugins/cache/*/skills-md/*/skills/claudecode-migrate 2>/dev/null
```

If any of those hit, say so once, near the top of the first reply:

> **Reminder:** `skills-md:claudecode-migrate` is still installed and overlaps this skill. This skill supersedes it — same source mapping, plus project migration, plugin porting, and a write gate. Disable or remove the skills-md copy to avoid divergent guidance.

Then keep going. This is a reminder, not a blocker; do not refuse to work because a stale skill is installed.

## Step 1 — Route to exactly one reference

Three migrations live in this skill because they share translation rules, but each has its own destinations and failure modes. Load only the one the user asked for. The other two are dead weight in context, and worse, they tempt you into migrating things nobody asked about.

| The user is asking about | Mode | Read |
| --- | --- | --- |
| Their own machine, `~/.claude`, "my setup", "switch me over" | `global` | `references/global.md` |
| One repository — `CLAUDE.md`, `.claude/`, "this project", team config | `project` | `references/project.md` |
| A Claude Code plugin's own source — `plugin.json`, `hooks/*.py`, "port my plugin" | `plugin-port` | `references/plugin-port.md` |

If the ask spans two modes ("migrate me *and* this repo"), run them as separate passes with separate gates. One plan, one approval, one apply — mixing them produces a plan too big to review, which defeats the point of having a gate.

## Step 2 — Discover (read-only)

Probe; never assume. A path that does not exist is a finding to record, not an error to handle.

Establish the target's real shape before planning anything:

```bash
command -v opencode && opencode --version
ls ~/.config/opencode/                              # what already lives there
ls ~/.config/opencode/opencode.json* 2>/dev/null    # .json and .jsonc are both valid
```

Also check whether `OPENCODE_CONFIG` is set in the user's shell profile — it relocates the config file entirely, so a migration that ignores it writes to a file opencode never reads. Grep the profile (`~/.zshrc`, `~/.bashrc`, `~/.profile`) for the name rather than printing the environment, which keeps secrets out of the transcript.

Three things people get wrong here, so check each explicitly:

- **The config filename is not fixed.** `opencode.json` and `opencode.jsonc` are both real. Writing a fresh `opencode.json` next to an active `opencode.jsonc` leaves two configs and a merge nobody understands.
- **The target is usually not empty.** Existing skills, plugins, commands, and agents mean most rows in your plan should read `merge` or `skip`, not `create`.
- **MCP config may already be split out** — a separate `mcps.json` or a per-project file pulled in by the user's own tooling. Find where MCP servers actually live before proposing to add more.

Then inventory the source side per the mode's reference file. Record concrete artifacts with counts, not impressions: "50 skills, 8 plugins, 5 commands, 1 agent" is reviewable; "existing setup present" is not.

## Step 3 — Plan (still read-only)

Emit a plan the user can read in one pass. Every artifact gets one row and exactly one action:

| Action | Meaning |
| --- | --- |
| `create` | Destination does not exist; safe to write fresh |
| `merge` | Destination exists; add keys or files without removing what is there |
| `skip` | Already migrated, identical, or deliberately excluded |
| `manual` | No mechanical equivalent — needs a human decision, so name the decision |

Alongside the table, include:

1. **Gaps** — Claude Code features with no opencode equivalent, each with its workaround. Put these up front; they are the part the user actually has to think about.
2. **Excluded for security** — what you are deliberately not copying, and why (see Govern).
3. **Backups** — the exact backup paths you will create.

Close the plan by stating that nothing has been written yet. Users reasonably assume an agent has already acted; saying otherwise costs one sentence.

## Step 4 — Human gate

Ask for approval with `AskUserQuestion`. Offer apply-everything, apply-a-subset, and keep-the-plan-only as real options — a user who wants to hand-apply half of it is making a sound call, not being difficult.

Do not proceed on ambiguity. "Looks good" about the plan is approval; "interesting" is not. If the answer changes scope, re-plan and gate again rather than improvising mid-apply.

## Step 5 — Apply (merge, never clobber)

Back up, write, verify each write, then move to the next.

- **Back up first.** `cp <file> <file>.bak-$(date -u +%Y%m%dT%H%M%SZ)` for every file about to change. It is cheap, and it is the only undo the user has. Inside a repository, check that the backup pattern is gitignored before writing one — an existing `*.bak` rule does not match `*.bak-<timestamp>`.
- **Merge JSON by key, not by document.** Read the existing config, add or update only the keys in the plan, leave the rest byte-identical where you can.
- **Prefer `.jsonc`, and if the target already is `.jsonc`, edit in place.** Both extensions are read at both scopes, and comments are how a permission rule keeps its reason attached. Reserializing strips the comments the user wrote to explain their own config. Targeted edits keep them; a parse-and-dump round trip does not.
- **Never remove a key you did not add.** A genuine conflict is a `manual` row you should have caught while planning — stop and ask rather than picking a winner quietly.
- **Copy, do not move.** The Claude Code setup should still work afterwards. A migration that breaks the source is a one-way door nobody agreed to walk through.

## Step 6 — Verify and report

Act, then look at what actually happened rather than assuming it worked:

```bash
# Config parses at all? opencode refuses to start on a malformed config.
opencode run -p "reply with OK" 2>&1 | head -5

# Skills visible?
opencode run -p "List available skills"

# Permissions as intended?
opencode run -p "What are my current permissions?"
```

Then re-read what you wrote and diff it against the approved plan. Report drift out loud, including anything skipped or impossible — a report that hides a failed step costs far more than the step itself.

Finish by writing `migration-report.md` (working directory, or the repo root in `project` mode) covering: what moved, what was skipped and why, the gap list with workarounds, and the backup paths. That file is the handover artifact; in `project` mode it is also what the rest of the team reads.

## Govern — the parts that are not negotiable

**Translate permissions conservatively.** Permission rules decide what an agent may do without asking, so a sloppy translation is a security regression, not a cosmetic one:

- `deny` always survives translation. Denied in Claude Code means denied in opencode.
- Anything you cannot map confidently becomes `ask`, never `allow`. An extra prompt is an annoyance; a wrong `allow` is an incident.
- Never widen an `allow`. `Bash(git diff:*)` does not become `"bash": "allow"`.
- opencode wildcard rules are **last-match-wins**, so general-then-specific (`"*": "ask"`, then `"git *": "allow"`) behaves as expected. Reversing that order silently grants everything, and nothing in the config will look wrong.

**Never migrate secrets.** These stay put in every mode unless the user explicitly asks and understands the cost:

| Excluded | Why |
| --- | --- |
| `~/.claude/credentials.json`, `.credentials.json` | Live auth tokens. Re-authenticate in opencode instead; rotate if they ever left the machine |
| `~/.claude/history.jsonl`, `transcripts/`, `sessions/` | Full conversation content — large, personal, and not configuration |
| `~/.claude/session-env/`, `shell-snapshots/` | Captured shell environments, which routinely contain exported tokens |
| `~/.claude/plugins/cache/` | Thousands of files; reinstall from source instead |
| `~/.claude/file-history/`, `paste-cache/` | Local edit history — not portable, sometimes sensitive |
| `.env`, `*.pem`, `*.key`, anything gitignored for secrecy | Never in `project` mode, and never into a file that gets committed |

**Keep secrets in the environment.** Claude Code's `${VAR}` becomes opencode's `{env:VAR}`. Translate the reference; do not resolve it. Inlining a resolved token into `opencode.json` is how a secret ends up in git history.

**Say what you could not do.** Gaps are findings, not failures. Auto-memory, agent teams, OS-level sandboxing, and path-scoped rules have no opencode equivalent today. Naming the gap and its workaround is the honest move; dropping the feature quietly and letting the user discover it next week is not.

## Reference files

Read one, chosen in Step 1:

- `references/global.md` — `~/.claude` → `~/.config/opencode`: settings, permissions, model IDs, instructions, skills, commands, agents, MCP, plus the full gap table and troubleshooting
- `references/project.md` — one repository: `CLAUDE.md` → `AGENTS.md`, `.claude/` → `.opencode/`, config precedence, what to commit, path-scoped rules
- `references/plugin-port.md` — a Claude Code plugin → an opencode plugin: hook event mapping, the TypeScript module shape, `package.json`, commands, agents, and what cannot be ported
