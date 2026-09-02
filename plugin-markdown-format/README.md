# markdown-format

Auto-formats `.md` files after Claude writes or edits them using `markdownlint-cli2 --fix`
— silently corrects LLM-generated markdown errors without blocking any writes.

## How It Works

A `PostToolUse` hook fires after every `Write`, `Edit`, or `MultiEdit` on a `.md` file.
It runs:

```bash
markdownlint-cli2 --fix --config <bundled-config> <file>
```

The hook is non-blocking — it never prevents a write from completing. Unfixable violations
(exit code 1) are accepted silently. The hook also loads a companion skill that teaches
Claude to write markdownlint-compliant markdown proactively.

**Binary resolution order:**

1. Global `markdownlint-cli2` binary (if installed)
2. `npx markdownlint-cli2` (auto-downloads on first run, no install required)
3. If `npx` is also unavailable: prints install hint and exits cleanly

## Requirements

- Node.js + npm (for `npx`)
- Optional: `npm install -g markdownlint-cli2` for faster repeated runs (skips npx overhead)

## Install

```bash
/plugin install markdown-format@llm-agent-workflow
/reload-plugins
```

## Configuration

The plugin ships a bundled `.markdownlint.json` with sensible defaults (see table below).
To override rules for a specific project, place a `.markdownlint.json` (or
`.markdownlint.yaml`, `.markdownlint.jsonc`) in the project root. `markdownlint-cli2` picks
it up automatically via its config search and the bundled config is not applied.

## Bundled Default Rules

| Rule | Setting | Reason |
| ---- | ------- | ------ |
| MD013 | disabled | Line length — LLMs don't wrap at 80 chars; enforcing creates noisy diffs |
| MD041 | disabled | First heading — fragments and skill files legitimately lack H1 |
| MD033 | disabled | Inline HTML — Claude emits valid HTML (badges, `<details>`, tables) |
| MD024 | `siblings_only` | Duplicate headings — same-name headings allowed under different parents |
| all others | enabled | Default markdownlint ruleset |

## Version History

| Version | Changes |
| ------- | ------- |
| 1.0.0 | Major release for repository rename to `llm-agent-workflow`; updated install target to `markdown-format@llm-agent-workflow`; no behavior changes |
| 0.1.0 | Initial release — PostToolUse hook + bundled config + markdown-format skill |
