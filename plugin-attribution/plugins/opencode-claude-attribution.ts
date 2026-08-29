import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

/**
 * opencode-claude-attribution plugin.
 *
 * Blocks external posts that lack the "🤖 Written by Claude, reviewed by <user>"
 * attribution line. Works dynamically with ANY MCP server.
 *
 * OpenCode port of the Claude Code PreToolUse hook
 * (plugin-attribution/hooks/attribution_hook.py):
 *   - `tool.execute.before` for `bash`  -> was `Bash` matcher branch
 *   - `tool.execute.before` for `mcp__*` -> was `mcp__.*` matcher branch
 *   - `throw new Error(...)`             -> was `sys.exit(2)` block
 */

const NAME_FILE = join(homedir(), ".config", "opencode", "claude-attribution-name.txt")

// MCP tool name patterns that provide native AI attribution,
// so the text attribution line is not required.
// Slack adds "Sent using @Claude" natively when posting via its AI integration.
const NATIVE_ATTRIBUTION_TOOL_PATTERNS = [
  "^mcp__slack__",
]

// Fields that typically contain postable text body.
// Checked in order — first non-empty match wins.
const BODY_FIELDS = [
  "body",
  "content",
  "message",
  "comment",
  "commentBody",
  "description",
  "text",
]

// Bash patterns that post to external platforms
const POSTING_BASH_PATTERNS = [
  "\\bgh\\s+pr\\s+(create|comment|review|edit)\\b",
  "\\bgh\\s+issue\\s+(create|comment|edit)\\b",
  "\\bgh\\s+api\\b.*(-f\\s+body=|--field\\s+body=|-F\\s+body=|--raw-field\\s+body=)",
  "\\bcurl\\s+.*-X\\s*(POST|PUT|PATCH)",
  "\\bcurl\\s+.*--data",
  "\\bjira\\s+issue\\s+(create|comment)",
]

const SETUP_MESSAGE = `[opencode-claude-attribution] BLOCKED: Reviewer name not configured.

Before posting to external platforms, set up your attribution name.
Ask the user for their name and save it:

  echo "Their Name" > ~/.config/opencode/claude-attribution-name.txt

Then show the complete post content to the user for approval before retrying.`

const MISSING_MESSAGE = (name: string) => `[opencode-claude-attribution] BLOCKED: Attribution line missing from post body.

All external posts must include this attribution line:

  🤖 Written by Claude, reviewed by ${name}

IMPORTANT: Before retrying, you MUST:
1. Add the attribution line to the post body
2. Show the COMPLETE post content to the user
3. Ask the user to approve before posting
4. Only retry after user confirms`

function getReviewerName(): string | null {
  try {
    const name = readFileSync(NAME_FILE, "utf8").trim()
    return name || null
  } catch {
    return null
  }
}

function findBodyField(args: Record<string, unknown>): [string, string] | null {
  for (const field of BODY_FIELDS) {
    const value = args[field]
    if (typeof value === "string" && value.trim()) {
      return [field, value]
    }
  }
  return null
}

function hasNativeAttribution(toolName: string): boolean {
  /** Return true if the tool provides native AI attribution (e.g. Slack's 'Sent using @Claude'). */
  return NATIVE_ATTRIBUTION_TOOL_PATTERNS.some((pattern) =>
    new RegExp(pattern).test(toolName),
  )
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

function hasAttribution(text: string, name: string): boolean {
  const pattern = new RegExp(
    "(🤖\\s*)?written\\s+by\\s+claude.*reviewed\\s+by\\s+" + escapeRegExp(name),
    "i",
  )
  return pattern.test(text)
}

// Per-session Claude-model flag. Updated by chat.params on each model call;
// checked by tool.execute.before to skip enforcement for non-Claude LLMs
// (the attribution line literally claims "Written by Claude").
const sessionIsClaude = new Map<string, boolean>()

function isClaudeSession(sessionID: string): boolean {
  return sessionIsClaude.get(sessionID) === true
}

export const ClaudeAttributionPlugin: Plugin = async ({ client }) => {
  return {
    "chat.params": async (input) => {
      // Anthropic provider + claude model family = Claude LLM
      sessionIsClaude.set(
        input.sessionID,
        input.model.providerID === "anthropic" &&
          input.model.modelID.toLowerCase().includes("claude"),
      )
    },

    "tool.execute.before": async (input, output) => {
      // Plugin is Claude-only — skip enforcement for other LLMs.
      if (!isClaudeSession(input.sessionID)) return

      const args = (output.args ?? {}) as Record<string, unknown>

      // --- Bash: only check posting CLI commands ---
      if (input.tool === "bash") {
        const command = typeof args.command === "string" ? args.command : ""
        const isPosting = POSTING_BASH_PATTERNS.some((pattern) =>
          new RegExp(pattern, "i").test(command),
        )
        if (!isPosting) return

        const name = getReviewerName()
        if (!name) {
          throw new Error(SETUP_MESSAGE)
        }
        if (!hasAttribution(command, name)) {
          throw new Error(MISSING_MESSAGE(name))
        }

        await client.app.log({
          body: {
            service: "opencode-claude-attribution",
            level: "info",
            message: `Bash posting command allowed with attribution`,
          },
        })
        return
      }

      // --- MCP tools: dynamic body field detection ---
      if (!input.tool.startsWith("mcp__")) return

      // Some MCP servers (e.g. Slack) provide native AI attribution.
      // Skip the text attribution check for those tools.
      if (hasNativeAttribution(input.tool)) return

      const result = findBodyField(args)
      if (result === null) return

      const bodyText = result[1]

      const name = getReviewerName()
      if (!name) {
        throw new Error(SETUP_MESSAGE)
      }

      if (!hasAttribution(bodyText, name)) {
        throw new Error(MISSING_MESSAGE(name))
      }

      await client.app.log({
        body: {
          service: "opencode-claude-attribution",
          level: "info",
          message: `MCP post allowed with attribution (${input.tool})`,
        },
      })
    },
  }
}