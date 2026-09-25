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
 *
 * The classifier ships as a payload (hooks/), found by the resolver block
 * below: the env root, then the project, then the global config dir, then the
 * dev layout. If no payload is found, or the resolver fails, every bash call
 * that mentions git is blocked with the paths searched and the reinstall
 * command. The factory itself never throws.
 */

const PLUGIN_ID = "commit-guard"
const PAYLOAD_MARKER = "hooks/commit_guard_hook.py"
const REINSTALL_HINT = `Blocking to be safe. Reinstall with ./setup-opencode.sh --global --plugin ${PLUGIN_ID}`

const CONFIG_DIR = join(homedir(), ".config", "opencode")
const TOKEN_FILE = join(CONFIG_DIR, ".commit-guard-token")
const STATE_DIR = join(CONFIG_DIR, ".commit-guard")

// Fast path only. Real classification happens in the Python hook. The
// lookbehind matches the hook's GIT_WORD: excluding `/` or `.` would let
// `/usr/bin/git commit` and `./git commit` skip the classifier entirely.
const GIT_WORD = /(?<![\w-])git(?:\.exe)?(?![\w.-])/

// <payload-resolver v2> keep byte-identical; checked by scripts/opencode/tests/test_resolver_drift.py
// requires: existsSync (node:fs), homedir (node:os), dirname + join (node:path), fileURLToPath (node:url)
const PAYLOAD_NAMESPACE = "llm-agent-workflow"
const PAYLOAD_ROOT_ENV = "LLM_AGENT_WORKFLOW_PAYLOAD_ROOT"
const PROJECT_CONFIG_DIR = ".opencode"
const GLOBAL_CONFIG_SUBDIR = "opencode"

interface PayloadQuery {
  pluginId: string
  marker: string
  directory: string
  worktree: string
}

interface PayloadResolution {
  root: string | null
  searched: string[]
}

function payloadCandidates(query: PayloadQuery): string[] {
  const override = process.env[PAYLOAD_ROOT_ENV]
  if (override) return [join(override, query.pluginId)]
  const configHome = process.env.XDG_CONFIG_HOME || join(homedir(), ".config")
  const projectRoots = [...new Set([query.directory, query.worktree].filter(Boolean))]
  return [
    ...projectRoots.map((root) => join(root, PROJECT_CONFIG_DIR, PAYLOAD_NAMESPACE, query.pluginId)),
    join(configHome, GLOBAL_CONFIG_SUBDIR, PAYLOAD_NAMESPACE, query.pluginId),
    join(dirname(fileURLToPath(import.meta.url)), ".."),
  ]
}

function resolvePayloadRoot(query: PayloadQuery): PayloadResolution {
  const searched = payloadCandidates(query)
  const root = searched.find((dir) => existsSync(join(dir, query.marker))) ?? null
  return { root, searched }
}
// </payload-resolver>

// One payload warning per process, however many instances or git calls hit it.
let payloadWarningLogged = false

export const opencodeCommitGuard: Plugin = async ({ client, directory, worktree }) => {
  // A resolver failure is treated like a missing payload: HOOK stays null.
  let payload: PayloadResolution = { root: null, searched: [] }
  let unavailable: string
  try {
    payload = resolvePayloadRoot({ pluginId: PLUGIN_ID, marker: PAYLOAD_MARKER, directory, worktree })
    unavailable = `[commit-guard] payload not found. Searched: ${payload.searched.join(", ")}. ${REINSTALL_HINT}`
  } catch (error) {
    unavailable = `[commit-guard] payload resolution failed (${String(error)}). ${REINSTALL_HINT}`
  }
  const HOOK = payload.root ? join(payload.root, PAYLOAD_MARKER) : null

  return {
    "tool.execute.before": async ({ tool }, output) => {
      if (tool !== "bash") return

      const command: string =
        typeof output?.args?.command === "string" ? output.args.command : ""
      if (!command || !GIT_WORD.test(command)) return

      if (HOOK === null) {
        // Fail CLOSED. Without the classifier no git command can be proven safe.
        if (!payloadWarningLogged) {
          payloadWarningLogged = true
          await client.app.log({ body: { service: "commit-guard", level: "warn", message: unavailable } })
        }
        throw new Error(unavailable)
      }

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
