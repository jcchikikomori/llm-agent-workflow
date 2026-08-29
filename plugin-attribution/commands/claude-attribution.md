---
description: Ensures "🤖 Written by Claude, reviewed by <user>" attribution on external posts. Use when posting to GitHub, JIRA, Confluence, Slack, or any external platform via MCP tools or CLI commands.
---

# claude-attribution

## Core Rule

Every post to an external platform MUST end with:

> 🤖 Written by Claude, reviewed by \<name\>

Where `<name>` comes from `~/.config/opencode/claude-attribution-name.txt`.

## Setup

On first use, if `~/.config/opencode/claude-attribution-name.txt` does not exist:

1. Ask the user: "What name should appear in the attribution line?"
2. Save their answer: `echo "<name>" > ~/.config/opencode/claude-attribution-name.txt`
3. Proceed with the original task

## What Counts as External Posts

ANY MCP tool call that includes a body/content/message field:

- GitHub: PR descriptions, issue comments, review comments
- JIRA: issue comments, issue descriptions
- Confluence: page content, comments
- Slack: messages
- Any other MCP-connected platform

Also CLI commands: `gh pr create`, `gh issue comment`, etc.

## How to Apply

1. Compose the post body
2. Append the attribution as the **last line** of the body:
   ```
   🤖 Written by Claude, reviewed by <name>
   ```
3. Do NOT add it to titles, labels, or metadata fields — only the body/content
4. For Bash CLI commands (`gh`, `curl`, etc.), always include the attribution **inline** in the body string — never use shell variable expansion like `${ATTR}` to inject it, because the hook inspects raw command text before shell expansion

## User Review Before Posting (Mandatory)

Before making ANY external post, you MUST:

1. Show the user the **complete post content** (including the attribution line)
2. Clearly state **where** it will be posted (platform, repo, issue number, channel, etc.)
3. Ask: "Ready to post this? (yes/no)"
4. **Only post after user confirms.** If user says no, ask what to change and revise.

This applies to ALL external posts — GitHub PRs, issue comments, JIRA comments,
Slack messages, Confluence pages, review comments, and any other platform.

Never skip this step. The attribution line says "reviewed by" — the user must
actually review it.

## When the Hook Blocks

If the plugin hook blocks your post:

1. Read the name from `~/.config/opencode/claude-attribution-name.txt`
2. Add the attribution line to the post body
3. Show the updated content to the user for review
4. Retry the tool call only after user approves