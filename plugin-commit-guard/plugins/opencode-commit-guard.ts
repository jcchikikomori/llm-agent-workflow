import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import type { Plugin } from "@opencode-ai/plugin"

/**
 * commit-guard — OpenCode port of plugin-commit-guard.
 *
 * Delegation flow (NOT the old approval-then-retry flow): every git command
 * that writes a commit message is handed to the user to run in their own
 * terminal, and the agent arms a bounded watcher (hooks/await_commit.sh) that
 * reports when the user finished, aborted, or ran out of time.
 *
 * This port deliberately does NOT reimplement the classification matrix. It
 * shells out to hooks/commit_guard_hook.py — the same classifier the Claude
 * Code hook uses — so the two ports cannot drift. Distinguishing
 * `git merge --continue` (delegate) from `git merge --abort` (allow) and
 * `git merge --squash` (allow, creates no commit) is ~250 lines of argv
 * parsing; maintaining it twice is how the old regex port ended up weaker than
 * the Python one.
 *
 * Environment overrides handed to the classifier:
 *   COMMIT_GUARD_TOKEN_FILE — OpenCode's config dir, not ~/.claude
 *   COMMIT_GUARD_STATE_DIR  — ledger + watcher result files
 *
 * GPG signing is fully preserved: the command is never modified, and the
 * instructions never suggest stripping -S/--gpg-sign or forcing --no-gpg-sign.
 */

const HERE = dirname(fileURLToPath(import.meta.url))
const HOOK = join(HERE, "..", "hooks", "commit_guard_hook.py")

const CONFIG_DIR = join(homedir(), ".config", "opencode")
const TOKEN_FILE = join(CONFIG_DIR, ".commit-guard-token")
const STATE_DIR = join(CONFIG_DIR, ".commit-guard")

// Fast path only. Real classification happens in the Python hook.
const GIT_WORD = /(?<![\w./-])git(?:\.exe)?(?![\w.-])/

export const opencodeCommitGuard: Plugin = async ({ client }) => {
  return {
    "tool.execute.before": async ({ tool }, output) => {
      if (tool !== "bash") return

      const command: string =
        typeof output?.args?.command === "string" ? output.args.command : ""
      if (!command || !GIT_WORD.test(command)) return

      if (!existsSync(HOOK)) {
        // Fail CLOSED. A missing classifier must never mean "allow".
        throw new Error(
          `[commit-guard] classifier not found at ${HOOK}. Blocking to be safe — ` +
            `hand the command to the user and ask them to run it.`,
        )
      }

      const python = process.env.COMMIT_GUARD_PYTHON ?? "python3"
      const result = spawnSync(python, [HOOK], {
        input: JSON.stringify({
          tool_name: "Bash",
          tool_input: { command },
          cwd: process.cwd(),
        }),
        encoding: "utf8",
        env: {
          ...process.env,
          COMMIT_GUARD_TOKEN_FILE: TOKEN_FILE,
          COMMIT_GUARD_STATE_DIR: STATE_DIR,
        },
      })

      if (result.error || result.status === null) {
        throw new Error(
          `[commit-guard] could not run the classifier (${python}): ` +
            `${result.error?.message ?? "no exit status"}. Blocking to be safe — ` +
            `hand the command to the user and ask them to run it.`,
        )
      }

      // 0 = allow (not a commit-writing command, or an approved one-time token
      // was consumed). 2 = block, with the delegate payload on stderr.
      if (result.status === 0) return

      if (result.status === 2) {
        await client.app.log({
          body: {
            service: "commit-guard",
            level: "warn",
            message: "Delegated a commit-writing git command to the user",
          },
        })
        throw new Error(result.stderr.trim())
      }

      // Any other exit code means the classifier itself failed. Fail CLOSED:
      // in Claude Code a non-0/2 exit is a *non-blocking* error, which is
      // exactly the fail-open this port must not reproduce.
      throw new Error(
        `[commit-guard] classifier exited ${result.status}. Blocking to be safe.\n` +
          `${result.stderr.trim()}`,
      )
    },
  }
}
