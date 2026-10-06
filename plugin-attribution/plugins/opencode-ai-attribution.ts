import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

/**
 * opencode-ai-attribution plugin.
 *
 * Anti-AI-slop + attribution governance for external posts:
 *   - blocks posts that lack the "🤖 Written by <Family>, reviewed by <user>"
 *     attribution line (dynamic, ANY configured MCP server)
 *   - blocks clear AI-slop filler phrases in post bodies / posting commands
 *   - blocks AI-generated trailers in `git commit` messages
 *
 * Every check runs for every model; <Family> in the suggested line comes from
 * `chat.params`, and the check accepts any family.
 *
 * OpenCode port of the Claude Code PreToolUse hook
 * (plugin-attribution/hooks/attribution_hook.py):
 *   - `tool.execute.before` for `bash`  -> was `Bash` matcher branch
 *   - `tool.execute.before` for MCP tools -> was `mcp__.*` matcher branch
 *   - `throw new Error(...)`             -> was `sys.exit(2)` block
 *   - `config` collects MCP server names: opencode names MCP tools
 *     `<server>_<tool>` (sanitized), not `mcp__<server>__<tool>`
 */

// Family named in the suggested line for a session chat.params has not mapped.
const DEFAULT_FAMILY = "AI"

// Longest <Family> accepted in an attribution line.
const MAX_FAMILY_LENGTH = 40

// Reviewer-name files: the primary under `${XDG_CONFIG_HOME:-~/.config}`, then
// the 1.x files under the home dir, opencode's first. Read, never written.
const PRIMARY_NAME_FILE = join("llm-agent-workflow", "attribution-name.txt")
const LEGACY_OPENCODE_NAME_FILE = join(".config", "opencode", "claude-attribution-name.txt")
const LEGACY_CLAUDE_CODE_NAME_FILE = join(".claude", "claude-attribution-name.txt")

// Strict UTF-8, so a name file that does not decode counts as unreadable.
const UTF8 = new TextDecoder("utf-8", { fatal: true })

// Prefix of `mcp__<server>__<tool>` names, accepted next to the configured
// `<server>_` prefixes for forward compatibility.
const MCP_TOOL_PREFIX = "mcp__"

// MCP servers that provide native AI attribution,
// so the text attribution line is not required.
// Slack adds "Sent using @Claude" natively when posting via its AI integration.
const NATIVE_ATTRIBUTION_SERVERS = [
  "slack",
]

interface ModelFamily {
  family: string
  idTerms: readonly string[]
  providerTerms: readonly string[]
}

// ADR-0002 family table, in match order. Matches are case-insensitive
// substrings. The model id is tried against every row before the providerID,
// so routed providers such as github-copilot or openrouter still resolve the
// right family. No match is DEFAULT_FAMILY.
const MODEL_FAMILIES: readonly ModelFamily[] = [
  { family: "Claude", idTerms: ["claude"], providerTerms: ["anthropic"] },
  { family: "GPT", idTerms: ["gpt", "o1", "o3", "o4", "codex"], providerTerms: ["openai"] },
  { family: "Gemini", idTerms: ["gemini", "gemma"], providerTerms: ["google", "google-vertex"] },
  { family: "DeepSeek", idTerms: ["deepseek"], providerTerms: [] },
  { family: "Mistral", idTerms: ["mistral", "codestral", "devstral"], providerTerms: [] },
  { family: "Llama", idTerms: ["llama"], providerTerms: [] },
  { family: "Qwen", idTerms: ["qwen"], providerTerms: [] },
  { family: "Grok", idTerms: ["grok"], providerTerms: ["xai"] },
  { family: "Kimi", idTerms: ["kimi"], providerTerms: ["moonshotai"] },
  { family: "GLM", idTerms: ["glm"], providerTerms: ["zhipuai", "zai"] },
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

// Filler phrases that read as AI slop. Blocked in any external post body or
// posting command. Case-insensitive.
const SLOP_PHRASES = [
  "\\bthis\\s+commit\\b",
  "\\bin\\s+this\\s+PR\\b",
  "\\bcomprehensive\\b",
  "\\brobust\\b",
  "\\bseamless\\b",
  "\\bgreat\\s+job\\b",
  "\\bthanks\\s+for\\s+this\\b",
  "\\bI\\s+think\\s+maybe\\b",
  "\\byou\\s+might\\s+consider\\b",
]

// `git commit` invocations that carry a message inline (-m/--message,
// including combined short flags like -am) or in a file (-F/--file).
const COMMIT_COMMAND_PATTERN =
  "\\bgit\\s+commit\\b.*?\\s(?:--message\\b|--file\\b|-{1,2}[\\w-]*m\\b|-{1,2}[\\w-]*F\\b)"

// AI-model terms that mark a co-author/trailer as machine-generated.
const AI_TERMS = [
  "claude",
  "anthropic",
  "copilot",
  "chatgpt",
  "gpt",
  "openai",
  "gemini",
  "bard",
  "deepseek",
  "mistral",
  "llama",
  "llm",
  "ai",
]

// AI-trailer patterns blocked in commit messages (case-insensitive):
//   - "Co-authored-by: <AI name>" lines
//   - "generated by/with <AI>", "assisted by <AI>" footer lines
const COMMIT_AI_TRAILER_PATTERNS = [
  "co-authored-by:\\s*[^\\n]*\\b(?:" + AI_TERMS.join("|") + ")\\b",
  "(?:generated|created|written|assisted)\\s+(?:by|with)\\s+[^\\n]*\\b(?:"
    + AI_TERMS.join("|")
    + ")\\b",
]

const SETUP_MESSAGE = `[ai-attribution] BLOCKED: Reviewer name not configured.

Before posting to external platforms, set up your attribution name.
Ask the user for their name and save it:

  mkdir -p "\${XDG_CONFIG_HOME:-$HOME/.config}/llm-agent-workflow"
  echo "Their Name" > "\${XDG_CONFIG_HOME:-$HOME/.config}/llm-agent-workflow/attribution-name.txt"

Then show the complete post content to the user for approval before retrying.`

const MISSING_MESSAGE = (family: string, name: string) =>
  `[ai-attribution] BLOCKED: Attribution line missing from post body.

All external posts must include this attribution line:

  🤖 Written by ${family}, reviewed by ${name}

IMPORTANT: Before retrying, you MUST:
1. Add the attribution line to the post body
2. Show the COMPLETE post content to the user
3. Ask the user to approve before posting
4. Only retry after user confirms`

const COMMIT_TRAILER_MESSAGE = `[ai-attribution] BLOCKED: AI-generated trailer detected in commit message.

Remove AI attribution trailers from the commit message:
  - "Co-authored-by:" lines naming an AI model or assistant
  - "generated by/with <AI>" or "assisted by <AI>" lines

Keep the commit message concise, factual, and free of AI attribution. Then
retry the commit.`

const SLOP_MESSAGE = (phrases: string) => `[ai-attribution] BLOCKED: AI-slop filler detected in post content.

Remove these phrases and rewrite with concrete, specific language:
  ${phrases}

Prefer plain, factual wording that states what changed and why. For review
comments, structure feedback as: location -> problem -> proposed fix.

IMPORTANT: Rewrite the content, show the COMPLETE updated post to the user,
and get approval before retrying.`

function configHome(): string {
  /** Return `${XDG_CONFIG_HOME:-~/.config}`; an empty variable counts as unset. */
  return process.env.XDG_CONFIG_HOME || join(homedir(), ".config")
}

function nameFileCandidates(): string[] {
  /** Return the reviewer-name files in read order: primary, then the legacy fallbacks. */
  const home = homedir()
  return [
    join(configHome(), PRIMARY_NAME_FILE),
    join(home, LEGACY_OPENCODE_NAME_FILE),
    join(home, LEGACY_CLAUDE_CODE_NAME_FILE),
  ]
}

function readName(path: string): string {
  /** Return the trimmed name in one file, or "" when the file holds none or cannot be read. */
  try {
    return UTF8.decode(readFileSync(path)).trim()
  } catch {
    // Missing, a directory, no permission or not UTF-8: this file is "not
    // configured", so the caller tries the next one and fails closed after the last.
    return ""
  }
}

function getReviewerName(): string | null {
  /** Return the first non-empty reviewer name, or null when none is configured. */
  for (const path of nameFileCandidates()) {
    const name = readName(path)
    if (name) return name
  }
  return null
}

function familyMatching(value: unknown, termsOf: (row: ModelFamily) => readonly string[]): string | null {
  /** Return the first family whose terms occur in value, or null. */
  if (typeof value !== "string") return null
  const text = value.toLowerCase()
  const row = MODEL_FAMILIES.find((candidate) => termsOf(candidate).some((term) => text.includes(term)))
  return row ? row.family : null
}

function modelFamily(model: { id?: unknown; providerID?: unknown } | undefined): string {
  /** Map a chat.params model to its family: the model id first, then the providerID. */
  return familyMatching(model?.id, (row) => row.idTerms)
    ?? familyMatching(model?.providerID, (row) => row.providerTerms)
    ?? DEFAULT_FAMILY
}

function sanitizeMcpName(name: string): string {
  /** opencode's MCP name rule: every character outside [a-zA-Z0-9_-] becomes "_". */
  return name.replace(/[^a-zA-Z0-9_-]/g, "_")
}

function mcpServerOf(toolName: string, servers: readonly string[]): string | null {
  /** Return the tool's MCP server (`mcp__<server>__`, else the longest configured `<server>_` prefix), or null. */
  if (toolName.startsWith(MCP_TOOL_PREFIX)) {
    return toolName.slice(MCP_TOOL_PREFIX.length).split("__")[0]
  }
  let match: string | null = null
  for (const server of servers) {
    if (toolName.startsWith(`${server}_`) && (match === null || server.length > match.length)) {
      match = server
    }
  }
  return match
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

function hasNativeAttribution(server: string): boolean {
  /** Return true if the MCP server provides native AI attribution (e.g. Slack's 'Sent using @Claude'). */
  return NATIVE_ATTRIBUTION_SERVERS.includes(server)
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

function hasAttribution(text: string, name: string): boolean {
  /**
   * One line, any family (ATTRIBUTION_LINE_TEMPLATE in the Python hook): <Family> is
   * 1-40 characters, no comma or line break; the comma before "reviewed" is optional.
   */
  const pattern = new RegExp(
    "(?:\\u{1F916}[ \\t]*)?written[ \\t]+by[ \\t]+"
      + `[^,\\s][^,\\r\\n]{0,${MAX_FAMILY_LENGTH - 1}}?`
      + "[ \\t]*,?[ \\t]*reviewed[ \\t]+by[ \\t]+"
      + escapeRegExp(name),
    "iu",
  )
  return pattern.test(text)
}

function findSlopPhrases(text: string): string[] {
  /** Return the AI-slop filler phrases found in text, in match order. */
  const found: string[] = []
  for (const pattern of SLOP_PHRASES) {
    const match = text.match(new RegExp(pattern, "i"))
    if (match) found.push(match[0].trim())
  }
  return found
}

function findCommitMessages(command: string): string[] {
  /** Extract quoted messages from `git commit -m/--message` (incl. `-am`). */
  const messages: string[] = []
  const pattern = new RegExp(
    "(?:--message\\b|(?:^|[\\s;|&])-{1,2}[\\w-]*m\\b)\\s*([\"'])(.*?)\\1",
    "gis",
  )
  for (const match of command.matchAll(pattern)) {
    messages.push(match[2])
  }
  return messages
}

function findCommitMessageFiles(command: string): string[] {
  /** Extract file paths given to `git commit -F/--file` flags. */
  const files: string[] = []
  const pattern = new RegExp(
    "(?:--file\\b|(?:^|[\\s;|&])-{1,2}[\\w-]*F\\b)\\s*(\\S+)",
    "gi",
  )
  for (const match of command.matchAll(pattern)) {
    files.push(match[1].replace(/^["']|["']$/g, ""))
  }
  return files
}

function commitMessages(command: string): string[] {
  /** Collect the effective commit message text from a `git commit` command. */
  const messages = findCommitMessages(command)
  for (const file of findCommitMessageFiles(command)) {
    try {
      messages.push(readFileSync(file, "utf8"))
    } catch {
      // file not readable yet; git itself will fail the commit
    }
  }
  return messages
}

function hasAiTrailer(messages: string[]): boolean {
  /** Return true if any commit message carries an AI-generated trailer. */
  for (const message of messages) {
    for (const pattern of COMMIT_AI_TRAILER_PATTERNS) {
      if (new RegExp(pattern, "i").test(message)) return true
    }
  }
  return false
}

export const AiAttributionPlugin: Plugin = async ({ client }) => {
  // Sanitized names of the configured MCP servers, from the config hook.
  let mcpServers: string[] = []
  // Model family per session, from chat.params; dropped on session.deleted.
  const sessionFamilies = new Map<string, string>()

  return {
    config: async (cfg) => {
      mcpServers = Object.keys(cfg.mcp ?? {}).map(sanitizeMcpName)
    },

    "chat.params": async (input) => {
      sessionFamilies.set(input.sessionID, modelFamily(input.model))
    },

    event: async ({ event }) => {
      if (event.type === "session.deleted") {
        sessionFamilies.delete(event.properties.info.id)
      }
    },

    "tool.execute.before": async (input, output) => {
      const args = (output.args ?? {}) as Record<string, unknown>
      const family = sessionFamilies.get(input.sessionID) ?? DEFAULT_FAMILY

      // --- Bash: commit-quality check, then posting-command checks ---
      if (input.tool === "bash") {
        const command = typeof args.command === "string" ? args.command : ""

        // `git commit` with an inline or file-supplied message -> AI-trailer check
        if (new RegExp(COMMIT_COMMAND_PATTERN, "i").test(command)) {
          if (hasAiTrailer(commitMessages(command))) {
            throw new Error(COMMIT_TRAILER_MESSAGE)
          }
          return
        }

        const isPosting = POSTING_BASH_PATTERNS.some((pattern) =>
          new RegExp(pattern, "i").test(command),
        )
        if (!isPosting) return

        const slop = findSlopPhrases(command)
        if (slop.length > 0) {
          throw new Error(SLOP_MESSAGE(slop.join(", ")))
        }

        const name = getReviewerName()
        if (!name) {
          throw new Error(SETUP_MESSAGE)
        }
        if (!hasAttribution(command, name)) {
          throw new Error(MISSING_MESSAGE(family, name))
        }

        await client.app.log({
          body: {
            service: "ai-attribution",
            level: "info",
            message: `Bash posting command allowed with attribution`,
          },
        })
        return
      }

      // --- MCP tools: configured servers (or mcp__*), dynamic body field detection ---
      const server = mcpServerOf(input.tool, mcpServers)
      if (server === null) return

      // Some MCP servers (e.g. Slack) provide native AI attribution.
      // Skip the text attribution check for those tools.
      if (hasNativeAttribution(server)) return

      const result = findBodyField(args)
      if (result === null) return

      const bodyText = result[1]

      const slop = findSlopPhrases(bodyText)
      if (slop.length > 0) {
        throw new Error(SLOP_MESSAGE(slop.join(", ")))
      }

      const name = getReviewerName()
      if (!name) {
        throw new Error(SETUP_MESSAGE)
      }

      if (!hasAttribution(bodyText, name)) {
        throw new Error(MISSING_MESSAGE(family, name))
      }

      await client.app.log({
        body: {
          service: "ai-attribution",
          level: "info",
          message: `MCP post allowed with attribution (${input.tool})`,
        },
      })
    },
  }
}
