# token-saver

Enforces token-saving techniques for Claude Code — plan mode guidance, proactive compaction, specific prompts, subagent delegation, CLAUDE.md size limits, and effort advice.

Based on [Bozhidar Kamenski's 12 techniques](https://medium.com/@bozhidarkamenski/how-to-save-tokens-in-claude-code-12-techniques-that-actually-work-fac05e4abf1a).

## What it does

### Hooks (automatic)

| Hook               | Type             | What it does                                   |
| ------------------ | ---------------- | ---------------------------------------------- |
| `compact-reminder` | PostToolUse      | Nudges you to run `/compact` every ~15 minutes |
| `claude-md-guard`  | SessionStart     | Warns if CLAUDE.md exceeds 3000 chars          |
| `prompt-quality`   | UserPromptSubmit | Blocks vague prompts and asks for specifics    |

### Skill (behavioral)

The `token-saver` skill provides guidance on all 6 techniques:

1. **Plan first** — Suggest planning before coding to prevent wasted exploration
2. **Compact proactively** — Suggest `/compact` after 5+ file reads or exploration phases
3. **Be specific** — Ask for file paths, line numbers, function names
4. **Delegate verbose work** — Use subagents for output-heavy tasks
5. **Keep CLAUDE.md small** — Under 3000 chars, move details to skills/memory
6. **Lower effort for mechanical tasks** — Don't use maximum effort for formatting/renaming

## Install

```bash
/plugin install token-saver@llm-agent-workflow
/reload-plugins
```

## Using the skill in your project

The hooks work automatically once installed. For the behavioral guidance to take effect, reference the skill in your project's custom instructions.

### Option A: Load via CLAUDE.md

Add this to your project's `CLAUDE.md`:

```markdown
## Token efficiency

Follow token-saver skill rules — plan before coding, compact after exploration, be specific, delegate verbose work, keep CLAUDE.md under 3000 chars, lower effort for mechanical tasks.
```

Keep it to one line. The full rules live in the skill file; this just reminds Claude to load them.

### Option B: Load via CLAUDE.local.md

For project-specific overrides that don't get committed, use `CLAUDE.local.md`:

```markdown
## Token efficiency

Always suggest /compact before switching from exploration to implementation.
```

### Option C: Reference the skill directly

If you have other skills installed, you can reference token-saver by name in your instructions:

```markdown
/load token-saver
```

### What NOT to do

Don't copy the entire SKILL.md into CLAUDE.md — that defeats the purpose (CLAUDE.md should stay under 3000 chars). One-liner references are enough; Claude loads the full skill when it sees the reference.

## Configuration

### Vague prompt patterns

Edit `config/vague-patterns.json` to customize which prompts are blocked:

```json
{
  "patterns": ["fix the bug", "make it better", "handle the error"],
  "whitelisted_prefixes": ["fix ", "add ", "create "]
}
```

- `patterns` — Exact phrases that trigger the block
- `whitelisted_prefixes` — Prefixes that allow short follow-ups (e.g. "fix login flow" is OK)

### CLAUDE.md size limit

The default limit is 3000 characters (~500 words). To change it, edit `hooks/claude_md_guard.py` and update the `MAX_CHARS` constant.

### Compact interval

The default interval is 15 minutes. To change it, edit `hooks/compact_reminder.py` and update `COMPACT_INTERVAL_SECONDS`.

## How it works

### prompt-quality hook

Intercepts every user prompt before Claude sees it. If the prompt matches a vague pattern (and isn't whitelisted), it blocks with guidance on how to be specific:

```
[token-saver] BLOCKED: This prompt is too vague — it will cost extra tokens
for Claude to figure out what you mean.

Please provide specifics:
  - File paths (e.g. src/auth/login.ts)
  - Line numbers (e.g. line 45)
  - Function names (e.g. handleSubmit)
  - Error messages (e.g. "TypeError: Cannot read property 'map'")
```

### compact-reminder hook

Tracks timestamps in `~/.claude/.token-saver/`. After ~15 minutes, prints a reminder to consider `/compact`. Always exits 0 — never blocks.

### claude-md-guard hook

Checks CLAUDE.md size at session start. Warns (never blocks) if it exceeds 3000 characters, with suggestions to move content to skills or memory files.

## Version History

### 1.0.0

- Major release for repository rename to `llm-agent-workflow`
- Updated install target to `token-saver@llm-agent-workflow`
- No hook/skill behavior changes

### 0.1.0

- Initial release
- 3 hooks: compact-reminder, claude-md-guard, prompt-quality
- Behavioral skill with all 6 techniques
- Configurable vague-patterns.json
