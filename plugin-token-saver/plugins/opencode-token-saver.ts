/**
 * opencode-token-saver — OpenCode plugin port of the token-saver hooks.
 *
 * Ports all three Claude Code hooks from plugin-token-saver/hooks/ to the
 * OpenCode plugin API:
 *
 *   1. compact_reminder.py  (PostToolUse)      -> "tool.execute.after"
 *   2. claude_md_guard.py   (SessionStart)     -> "session.created" flag +
 *                                                 "experimental.chat.system.transform"
 *   3. prompt_quality.py    (UserPromptSubmit) -> "chat.message" flag +
 *                                                 "experimental.chat.system.transform"
 *
 * Hook 1 (compact reminder) keeps its state in ~/.config/opencode/.token-saver/
 * (OpenCode's config dir, replacing the Claude Code ~/.claude/.token-saver).
 * The last-compact timestamp is read from the state file's mtimeMs — no JSON
 * read on every tool call — but the file still carries the same
 * {session_id, timestamp} JSON payload the Python side writes, so both
 * runtimes can share state.
 *
 * HOOK 3 MAPPING GAP (UserPromptSubmit) — READ THIS:
 * Claude Code's UserPromptSubmit hook runs BEFORE a user prompt reaches the
 * model and can BLOCK it (exit 2) with the stderr message fed back as context.
 * OpenCode has NO equivalent blocking pre-prompt event — a freshly submitted
 * user prompt is not interceptable before it is sent to the model. The
 * best-effort mapping used here:
 *   - "chat.message" fires when a new user message is received; the prompt
 *     text is extracted from its text parts (skipping synthetic parts such as
 *     auto-continue turns).
 *   - The vagueness check (isVague below) is ported 1:1 from prompt_quality.py
 *     and still reads config/vague-patterns.json (relative to the plugin root).
 *   - The blocking exit(2) behavior is deliberately NOT ported — it is
 *     impossible in the OpenCode plugin API. Instead the block message is
 *     injected into the system prompt via "experimental.chat.system.transform"
 *     as REMINDER/GUIDANCE for the model to see (it can reply asking for
 *     specifics, rather than having the prompt silently rejected).
 *
 * All three paths are best-effort and never throw: diagnostics go through
 * client.app.log({ body: { service: "token-saver", ... } }).
 */
import type { Plugin } from "@opencode-ai/plugin"
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import path from "node:path"

// --- Module-level constants ------------------------------------------------

// Plugin root = the plugin-token-saver directory (this file lives in plugins/).
const pluginRoot = path.resolve(import.meta.dir, "..")

// OpenCode state dir replaces the Claude Code ~/.claude/.token-saver.
const STATE_DIR = path.join(homedir(), ".config", "opencode", ".token-saver")

// Vague-pattern config, same file the Python prompt_quality.py reads. Note:
// when this file is installed into ~/.config/opencode/plugins/ by
// setup-opencode.sh, this path no longer resolves and loadVaguePatterns()
// falls back to the built-in defaults.
const PATTERNS_FILE = path.join(pluginRoot, "config", "vague-patterns.json")

const COMPACT_INTERVAL_MS = 15 * 60 * 1000 // 15 minutes, matches the Python hook

const MAX_CHARS = 3000
const MAX_WORDS = 500

// OpenCode reads AGENTS.md natively; the Claude Code hook only looked at
// CLAUDE.md. The legacy name is built via concatenation so it never appears
// verbatim next to the OpenCode one (memory-guard convention).
const CLAUDE_DOC_NAME = "CLAUDE" + ".md"
const OPENCODE_DOC_NAME = "AGENTS.md"

const REMINDER_TEXT = `**💡 Token-saving tip:** It's been ~15 minutes since last compaction. Consider running \`/compact\` now to keep context lean and reduce token costs.

When to compact:
- After 5+ file reads or exploration phases
- Before switching from investigation to implementation
- When context feels cluttered with intermediate results

When NOT to compact:
- Mid-implementation (you need the context)
- Right before a critical edit (keep the plan visible)`

// Kept byte-identical to prompt_quality.py's BLOCK_MESSAGE for diffing.
const BLOCK_MESSAGE = `[token-saver] BLOCKED: This prompt is too vague — it will cost extra tokens
for Claude to figure out what you mean.

Please provide specifics:
  - File paths (e.g. src/auth/login.ts)
  - Line numbers (e.g. line 45)
  - Function names (e.g. handleSubmit)
  - Error messages (e.g. "TypeError: Cannot read property 'map'")

Bad:  "fix the bug"
Good: "Fix the null pointer in src/auth/login.ts line 45 — handleSubmit called before useState resolves"

Bad:  "make it better"
Good: "Optimize the N+1 query in app/models/user.rb — User#orders loads each order individually"

Bad:  "handle the error"
Good: "Add error handling for the failed fetch in components/Dashboard.tsx — catch the network error and show a retry button"`

// OpenCode cannot block — prepend this to BLOCK_MESSAGE so the model does not
// mistake a reminder for a rejected prompt.
const QUALITY_REMINDER_HEADER =
  "[token-saver] Vague prompt detected (OpenCode has no blocking UserPromptSubmit " +
  "equivalent — this is guidance, not a block). Ask for specifics if needed:"

// WARNING_TEMPLATE from claude_md_guard.py, with the doc name parameterized so
// the AGENTS.md fallback reads correctly.
const CLAUDE_MD_WARNING = (docName: string, charCount: number, wordCount: number): string =>
  `[token-saver] ⚠️ ${docName} is ~${charCount} chars (~${wordCount} words).
Recommended maximum: ${MAX_CHARS} chars (~${MAX_WORDS} words).

Large ${docName} files waste tokens every session because ${docName === OPENCODE_DOC_NAME ? "OpenCode reads" : "Claude reads"}
the entire file at startup. Consider moving detailed content to:
  - .claude/skills/<skill-name>/SKILL.md  (for behavioral rules)
  - .claude/memory/                       (for project knowledge)
  - docs/                                 (for reference documentation)

Keep ${docName} focused: 5 rules + 3 file pointers is the right size.`

// Fallback defaults, matching prompt_quality.py's load_patterns() fallback.
const DEFAULT_PATTERNS = [
  "fix the bug", "fix the error", "make it better",
  "handle the error", "just do it", "figure it out",
  "optimize this", "clean this up", "update this",
  "add the thing", "make it work", "solve this",
  "handle this", "do the thing", "fix this",
  "improve this", "refactor this",
]
const DEFAULT_WHITELISTED = [
  "fix ", "add ", "create ", "update ", "remove ", "change ", "refactor ",
]

interface VaguePatterns {
  patterns: string[]
  whitelisted: string[]
}

// --- Prompt-quality logic (1:1 port of prompt_quality.py) -------------------

function loadVaguePatterns(): VaguePatterns {
  try {
    const data = JSON.parse(readFileSync(PATTERNS_FILE, "utf8")) as {
      patterns?: unknown
      whitelisted_prefixes?: unknown
    }
    const patterns = data.patterns
    const whitelisted = data.whitelisted_prefixes
    if (
      Array.isArray(patterns) &&
      Array.isArray(whitelisted) &&
      patterns.every((p) => typeof p === "string") &&
      whitelisted.every((p) => typeof p === "string")
    ) {
      return { patterns, whitelisted }
    }
  } catch {
    // fall through to defaults (missing or unreadable config)
  }
  return { patterns: [...DEFAULT_PATTERNS], whitelisted: [...DEFAULT_WHITELISTED] }
}

/**
 * Mirrors prompt_quality.py's is_vague() exactly: whitelisted prefixes first,
 * then exact pattern matches, then the short-prompt heuristic.
 */
function isVague(prompt: string, patterns: string[], whitelisted: string[]): boolean {
  const promptLower = prompt.toLowerCase().trim()

  // Check whitelisted prefixes first — if the prompt starts with one, it is
  // specific enough... unless the remainder is itself a vague fragment.
  for (const prefix of whitelisted) {
    if (promptLower.startsWith(prefix)) {
      // "fix X" is okay if X is specific (more than just "the bug")
      const remainder = promptLower.slice(prefix.length).trim()
      const vagueRemainders = patterns
        .filter((p) => p.startsWith(prefix))
        .map((p) => p.slice(prefix.length).trim())
      if (vagueRemainders.includes(remainder)) return true
      // Meaningful content (file paths, line numbers, function names) — allow
      if (remainder.length > 20 || [..."./_()"].some((c) => remainder.includes(c))) return false
      // Short remainder after prefix — could be vague
      if (remainder.split(/\s+/).length <= 3) return true
      return false
    }
  }

  // Exact matches against vague patterns
  for (const pattern of patterns) {
    if (promptLower === pattern) return true
  }

  // Very short prompt lacking specificity markers (Array.from = code points,
  // matching Python's len() over UTF-16 length)
  if (Array.from(promptLower).length < 15 && ![..."./_:()"].some((c) => promptLower.includes(c))) {
    return true
  }

  return false
}

/**
 * Extract the user's prompt text from a message's parts. Only non-synthetic
 * text parts count — synthetic parts (auto-continue, compaction continue) are
 * not user prompts.
 */
function extractPromptText(
  parts: ReadonlyArray<{ type: string; text?: string; synthetic?: boolean }>,
): string {
  const texts: string[] = []
  for (const part of parts) {
    if (part.type === "text" && !part.synthetic && part.text?.trim()) {
      texts.push(part.text)
    }
  }
  return texts.join("\n").trim()
}

// --- State helpers ----------------------------------------------------------

function sanitizeSessionID(sessionID: string): string {
  const s = String(sessionID).replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 128)
  return s || "unknown"
}

/** mtimeMs of the state file, or 0 if it does not exist yet. */
function lastCompactTimestamp(statePath: string): number {
  try {
    return statSync(statePath).mtimeMs
  } catch {
    return 0
  }
}

// --- Plugin ----------------------------------------------------------------

export const TokenSaverPlugin: Plugin = async ({ client, directory }) => {
  const docRoot = directory ?? process.cwd()

  // SessionStart equivalent: set on session.created, consumed once by the
  // first experimental.chat.system.transform (mirrors memory-guard).
  let sessionFresh = false
  let currentSessionID: string | undefined

  // chat.message can fire more than once for the same message; checkedMessages
  // dedupes by messageID. pendingVague collects the flagged messageIDs for the
  // next system.transform to surface as a single reminder.
  const checkedMessages = new Map<string, Set<string>>()
  const pendingVague = new Map<string, Set<string>>()

  async function log(
    level: "debug" | "info" | "warn" | "error",
    message: string,
    extra?: Record<string, unknown>,
  ): Promise<void> {
    try {
      await client.app.log({ body: { service: "token-saver", level, message, extra } })
    } catch {
      // logging must never break a hook
    }
  }

  // claude_md_guard equivalent: prefer CLAUDE.md, fall back to AGENTS.md
  // (OpenCode's native doc file). Only the FIRST found doc is considered.
  function buildClaudeMdWarning(): string | null {
    for (const name of [CLAUDE_DOC_NAME, OPENCODE_DOC_NAME]) {
      const candidate = path.join(docRoot, name)
      let content: string
      try {
        if (!existsSync(candidate)) continue
        content = readFileSync(candidate, "utf8")
      } catch {
        continue // unreadable — try the next candidate
      }
      if (content.length > MAX_CHARS) {
        return CLAUDE_MD_WARNING(name, content.length, content.split(/\s+/).length)
      }
      // This doc exists and is within limits — nothing to warn about.
      return null
    }
    return null
  }

  return {
    event: async ({ event }) => {
      if (event.type === "session.created") {
        sessionFresh = true
        currentSessionID = event.properties.info.id
      }
    },

    // UserPromptSubmit equivalent (best effort — see header for the gap).
    "chat.message": async ({ sessionID, messageID }, { parts }) => {
      const sid = sessionID
      const mid = messageID
      if (!sid || !mid) return
      try {
        const text = extractPromptText(parts)
        if (!text) return
        const checked = checkedMessages.get(sid) ?? new Set<string>()
        checkedMessages.set(sid, checked)
        if (checked.has(mid)) return
        checked.add(mid)

        const { patterns, whitelisted } = loadVaguePatterns()
        if (isVague(text, patterns, whitelisted)) {
          const flagged = pendingVague.get(sid) ?? new Set<string>()
          flagged.add(mid)
          pendingVague.set(sid, flagged)
          log("warn", "vague prompt detected", { sessionID: sid, messageID: mid })
        }
      } catch (err) {
        log("error", "chat.message handling failed", { error: String(err) })
      }
    },

    // compact_reminder equivalent: runs after every tool call, never blocks.
    "tool.execute.after": async (input) => {
      const sid = input.sessionID
      if (!sid) return
      try {
        mkdirSync(STATE_DIR, { recursive: true })
        const statePath = path.join(STATE_DIR, `last-compact-${sanitizeSessionID(sid)}.json`)
        const last = lastCompactTimestamp(statePath)
        const now = Date.now()
        if (last === 0 || now - last >= COMPACT_INTERVAL_MS) {
          await log("info", REMINDER_TEXT)
        }
        // Always update the timestamp — each tool call resets the window.
        // JSON payload mirrors the Python side's write so the two runtimes can
        // share state; only the mtime is ever read here.
        writeFileSync(statePath, JSON.stringify({ session_id: sid, timestamp: now / 1000 }))
      } catch (err) {
        log("error", "compact-reminder failed", { error: String(err) })
      }
    },

    "experimental.chat.system.transform": async ({ sessionID }, output) => {
      const sid = sessionID ?? currentSessionID
      if (!sid) return

      // SessionStart equivalent: run the CLAUDE.md guard once per fresh session.
      if (sessionFresh) {
        sessionFresh = false
        try {
          const warning = buildClaudeMdWarning()
          if (warning) {
            output.system.push(warning)
            log("warn", "injected doc-size warning", { sessionID: sid })
          }
        } catch (err) {
          log("error", "doc-size check failed", { error: String(err) })
        }
      }

      // Surface any vague prompts flagged since the last turn as guidance.
      const flagged = pendingVague.get(sid)
      if (flagged && flagged.size > 0) {
        pendingVague.delete(sid)
        try {
          const note = flagged.size > 1 ? `\n\n(Reminder covers ${flagged.size} prompts this turn.)` : ""
          output.system.push(`${QUALITY_REMINDER_HEADER}\n\n${BLOCK_MESSAGE}${note}`)
          log("warn", "injected vague-prompt guidance", { sessionID: sid, count: flagged.size })
        } catch (err) {
          log("error", "vague-prompt guidance failed", { error: String(err) })
        }
      }
    },
  }
}