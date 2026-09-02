# claude-attribution

Ensures every external post made through any MCP-connected platform carries a visible AI-authorship attribution line:

> 🤖 Written by Claude, reviewed by \<name\>

## How It Works

- A **PreToolUse hook** matches all MCP tools (`mcp__.*`) and Bash commands dynamically — no hardcoded platform names.
- The hook scans `tool_input` for body-like fields (`body`, `content`, `message`, `comment`, `commentBody`, `description`, `text`).
- Posts missing the attribution line are **blocked before they are sent**.
- A companion **skill** instructs Claude to include the attribution proactively and to **show the post to the user for review before sending**.

## Supported Platforms

Works with **any** MCP-connected platform, including:

- GitHub (PRs, issues, review comments)
- JIRA (issue comments, descriptions)
- Confluence (pages, comments)
- Any future MCP server with body-like fields

Also covers CLI tools: `gh pr create`, `gh pr comment`, `gh issue comment`, `gh api`, `curl POST/PUT/PATCH`, etc.

### Native Attribution Exceptions

Some platforms provide their own built-in AI attribution, making the manual text line unnecessary. These are **exempt** from the text check:

| Platform | Native label | Hook behaviour |
|----------|-------------|----------------|
| Slack (`mcp__slack__*`) | "Sent using @Claude" added by Slack automatically | ✅ Skipped — native label is sufficient |

To add more exempt platforms, extend `NATIVE_ATTRIBUTION_TOOL_PATTERNS` in `hooks/attribution_hook.py`.

## Setup

### 1. Install the plugin

```bash
/plugin install claude-attribution@llm-agent-workflow
/reload-plugins
```

### 2. Set your name (first use)

The plugin will prompt for your name on first use. You can also set it manually:

```bash
echo "Your Name" > ~/.claude/claude-attribution-name.txt
```

## User Review Flow

Before posting to any external platform, Claude will:

1. Compose the post with the attribution line
2. Show the **complete post content** to the user
3. Ask for approval before sending
4. Only post after user confirms

## Version History

### 1.0.0

- Major release for repository rename to `llm-agent-workflow`
- Updated install target to `claude-attribution@llm-agent-workflow`
- No behavior changes to attribution enforcement

### 0.4.0

- Added OpenCode TypeScript port (`plugins/opencode-claude-attribution.ts`)
- Added `/claude-attribution` slash command (`commands/claude-attribution.md`)
- Attribution enforcement now limited to Claude LLMs only — `chat.params` hook tracks provider/model per session, `tool.execute.before` skips non-Claude models
- Added OpenCode npm package manifest (`package.json`)

### 0.3.0

- Added `NATIVE_ATTRIBUTION_TOOL_PATTERNS` — Slack (`mcp__slack__*`) tools are now exempt from the text attribution check since Slack adds "Sent using @Claude" natively.

### 0.2.0

- Expanded Bash pattern coverage: `gh pr edit`, `gh issue edit`, `gh api ... -f body=`
- Fixed false positive on `gh pr merge` (no text body, should not block)
- Hook block messages now enforce user approval flow before retry
- Added user review requirement to skill and hook messages

### 0.1.0

- Initial release
- Dynamic MCP tool detection via body-field scanning
- PreToolUse hook + behavioral skill (dual-layer enforcement)
- User name config at `~/.claude/claude-attribution-name.txt`
- Covers GitHub, JIRA, Confluence, Slack, and any MCP server
