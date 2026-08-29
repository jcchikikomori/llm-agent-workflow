import { readFileSync, unlinkSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

/**
 * commit-guard — OpenCode port of plugin-commit-guard's hooks/commit_guard_hook.py.
 *
 * Approval-based flow (NOT the hard-block approach): blocks every `git commit`
 * bash call until the user explicitly approves it. On approval the agent writes
 * a one-time token file and retries; the retry passes through without a second
 * prompt and consumes the token.
 *
 * GPG signing is fully preserved: the command is never modified, and the
 * instructions never suggest stripping -S/--gpg-sign or forcing --no-gpg-sign.
 */

// Token config path lives under the OpenCode config dir (home + .config/opencode).
const TOKEN_FILE = join(homedir(), ".config", "opencode", ".commit-guard-token")

// Matches `git commit` (word-boundary, case-insensitive).
const GIT_COMMIT_PATTERN = /\bgit\s+commit\b/i

// Matches single- and double-quoted strings — used to strip quoted content
// before pattern matching so `git commit` inside a string arg (e.g. the
// approval token-write command) doesn't falsely trigger the hook.
const QUOTED_STRING_PATTERN = /"[^"]*"|'[^']*'/g

const BLOCKED_MESSAGE = `[commit-guard] BLOCKED: git commit requires user approval.

Before running this commit, you MUST:
1. Run: git diff --cached --stat
2. Run: git diff --cached --name-only
3. Extract the commit message from the command (the -m "..." value), or note that it uses an editor/template
4. Show the user:
   - Staged files (from step 1 and 2)
   - Commit message (from step 3)
   - The exact command you are about to run
5. Ask the user: "Proceed with this commit?"
6. If the user says YES:
   - Write the one-time approval token (SHA256 of exact command + cwd + timestamp):
     python3 -c "import hashlib,sys,pathlib,time; cmd=sys.argv[1]; cwd=sys.argv[2]; p=pathlib.Path.home()/'.config'/'opencode'/'.commit-guard-token'; p.parent.mkdir(exist_ok=True); p.write_text(hashlib.sha256((cmd+'|'+cwd+'|'+str(time.time())).encode()).hexdigest())" "<exact-command>" "<cwd>"
   - Then retry the EXACT same command unchanged
7. If the user says NO: abort. Do NOT retry.

IMPORTANT — GPG signing policy:
  - Never add --no-gpg-sign or -c commit.gpgsign=false
  - Never strip -S or --gpg-sign from the command
  - If the repo requires signed commits, git will invoke gpg-agent/pinentry after approval
  - The user will enter their passphrase through the normal pinentry dialog (TTY/pinentry-aware)`

export const opencodeCommitGuard: Plugin = async ({ client }) => {
  return {
    "tool.execute.before": async ({ tool }, output) => {
      if (tool !== "bash") return

      const command: string =
        typeof output?.args?.command === "string" ? output.args.command : ""

      const unquoted = command.replace(QUOTED_STRING_PATTERN, "")
      if (!GIT_COMMIT_PATTERN.test(unquoted)) return

      // Token present → this is the approved retry. Consume it (single-use) and allow.
      if (readToken() !== null) {
        consumeToken()
        await client.app.log({
          body: {
            service: "commit-guard",
            level: "info",
            message: "Approved git commit (one-time token consumed)",
          },
        })
        return
      }

      await client.app.log({
        body: {
          service: "commit-guard",
          level: "warn",
          message: "Blocked git commit — awaiting user approval",
        },
      })
      throw new Error(BLOCKED_MESSAGE)
    },
  }
}

/**
 * Token presence = approval. Content is a SHA256 of command + cwd + timestamp,
 * which cannot be recomputed at hook time (timestamp is write-time only), so
 * validity is presence-based. The file is single-use — consumed on the next
 * `git commit`, after which the hook blocks again.
 */
function readToken(): string | null {
  try {
    const token = readFileSync(TOKEN_FILE, "utf8").trim()
    return token.length > 0 ? token : null
  } catch {
    return null
  }
}

function consumeToken(): void {
  try {
    unlinkSync(TOKEN_FILE)
  } catch {
    // File already gone — nothing to clean up.
  }
}