import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import type { Plugin } from "@opencode-ai/plugin"

/**
 * markdown-format — OpenCode port of the Claude Code PostToolUse hook
 * (hooks/markdown_format_hook.py).
 *
 * Claude Code event → OpenCode event mapping:
 *   PostToolUse (matcher: Write|Edit|MultiEdit)
 *     → "tool.execute.after", fired only for file-touching tools (write,
 *       edit, patch, and multi-edit-style patches) — never for read or bash.
 *
 * After any .md file is written or edited, this plugin runs
 * `markdownlint-cli2 --fix` against it with the payload config
 * (config/.markdownlint.json), silently correcting lint violations the model
 * introduced. It is an advisor: it never blocks and the hook never throws,
 * mirroring the original Python hook's always-exit-0 behavior. Failures are
 * logged via client.app.log() at warn level, once per process for each kind:
 * one for the payload, one for the linter.
 *
 * The config ships as a payload (config/), found by the resolver block below:
 * the env root, then the project, then the global config dir, then the dev
 * layout. Without it, or if the resolver fails, the linter runs without
 * --config.
 *
 * Binary resolution matches the Python `shutil.which` order: a global
 * `markdownlint-cli2` is preferred, falling back to `npx markdownlint-cli2`.
 * Exit codes 0 (all fixed) and 1 (unfixable violations remain) are both
 * success.
 */

const PLUGIN_ID = "markdown-format"
const PAYLOAD_MARKER = "config/.markdownlint.json"
const NO_CONFIG = `Formatting without --config. Reinstall with ./setup-opencode.sh --global --plugin ${PLUGIN_ID}`

// File-touching tools: write/edit/patch and MCP-style variants
// (mcp__server__write_file, apply_patch, multi_edit, ...). Read/bash excluded.
const FILE_TOOL_RE = /(^|[._-])(write|edit|patch|multi_?edit)(_?file)?$/i

/** File-path-like args across OpenCode's file tools (edit, write, patch). */
const FILE_PATH_ARGS = ["filePath", "file_path", "path", "filepath"]

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

type WarningKind = "payload" | "linter"

// Warnings this process has logged: one per kind, however many instances or writes hit it.
const loggedWarnings = new Set<WarningKind>()

/**
 * Collect candidate paths from the tool call. Mirrors the wandavision plugin:
 * reads the filePath-style arg, plus the "Edited X / Wrote X" output title.
 */
function candidatePaths(
  args: Record<string, unknown> | undefined,
  title: string,
): string[] {
  const paths: string[] = []
  if (args && typeof args === "object") {
    for (const key of FILE_PATH_ARGS) {
      const v = args[key]
      if (typeof v === "string" && v) paths.push(v)
    }
  }
  // OpenCode titles for file tools look like "Edited src/foo.md" / "Wrote README.md"
  if (title) {
    const m = title.match(/(?:Edited|Wrote|Created|Updated|Patched)\s+(.+)$/)
    if (m) paths.push(m[1])
  }
  return paths
}

/**
 * Cross-platform stand-in for Python's shutil.which(): POSIX `command -v`
 * first, Windows `where` as fallback.
 *
 * Every child gets `env: process.env`: without it, Bun's node:child_process
 * hands a child the env from process start, not the current one.
 */
function findExecutable(name: string): string | null {
  try {
    const res = spawnSync("sh", ["-c", `command -v ${name}`], { encoding: "utf8", env: process.env })
    if (res.status === 0 && res.stdout) {
      const out = res.stdout.trim()
      if (out) return out
    }
  } catch {
    // fall through to the Windows check
  }
  try {
    const res = spawnSync("cmd", ["/c", "where", name], { encoding: "utf8", env: process.env })
    if (res.status === 0 && res.stdout) {
      const out = res.stdout.split(/\r?\n/)[0]?.trim()
      if (out) return out
    }
  } catch {
    // not found
  }
  return null
}

export const MarkdownFormatPlugin: Plugin = async ({ client, directory, worktree }) => {
  // A resolver failure is treated like a missing payload: CONFIG stays null.
  let payload: PayloadResolution = { root: null, searched: [] }
  let unavailable: string
  try {
    payload = resolvePayloadRoot({ pluginId: PLUGIN_ID, marker: PAYLOAD_MARKER, directory, worktree })
    unavailable = `[markdown-format] payload not found. Searched: ${payload.searched.join(", ")}. ${NO_CONFIG}`
  } catch (error) {
    unavailable = `[markdown-format] payload resolution failed (${String(error)}). ${NO_CONFIG}`
  }
  const CONFIG = payload.root ? join(payload.root, PAYLOAD_MARKER) : null

  const log = async (level: "info" | "warn", message: string): Promise<void> => {
    try {
      await client.app.log({ body: { service: "markdown-format", level, message } })
    } catch {
      // logging must never break the hook
    }
  }

  const warnOnce = async (kind: WarningKind, message: string): Promise<void> => {
    if (loggedWarnings.has(kind)) return
    loggedWarnings.add(kind)
    await log("warn", message)
  }

  // The payload config if it is still there; otherwise null, after the payload warning.
  const payloadConfig = async (): Promise<string | null> => {
    if (CONFIG !== null && existsSync(CONFIG)) return CONFIG
    const missing = CONFIG === null ? unavailable : `[markdown-format] config not found at ${CONFIG}. ${NO_CONFIG}`
    await warnOnce("payload", missing)
    return null
  }

  const runFormatter = async (filePath: string): Promise<void> => {
    // Binary resolution: prefer a global markdownlint-cli2, fall back to npx.
    let cmd: string[]
    const binary = findExecutable("markdownlint-cli2")
    if (binary) {
      cmd = [binary, "--fix"]
    } else {
      const npx = findExecutable("npx")
      if (!npx) {
        await warnOnce(
          "linter",
          "[markdown-format] markdownlint-cli2 not found and npx unavailable. " +
            "Install: npm install -g markdownlint-cli2",
        )
        return
      }
      cmd = ["npx", "markdownlint-cli2", "--fix"]
    }

    const config = await payloadConfig()
    if (config) cmd.push("--config", config)

    cmd.push(filePath)

    const result = spawnSync(cmd[0], cmd.slice(1), { encoding: "utf8", env: process.env })

    if (result.error) {
      await warnOnce("linter", `[markdown-format] could not run ${cmd[0]}: ${result.error.message}`)
      return
    }

    // 0 = all fixed, 1 = unfixable violations remain — both are acceptable.
    if (result.status !== 0 && result.status !== 1) {
      await warnOnce(
        "linter",
        `[markdown-format] unexpected exit ${result.status ?? result.signal}: ${(result.stderr ?? "").trim()}`,
      )
      return
    }

    await log(
      "info",
      `[markdown-format] markdownlint-cli2 --fix ${filePath} (exit ${result.status})`,
    )
  }

  return {
    "tool.execute.after": async (input, output) => {
      // The write already happened: nothing here may throw back at it.
      try {
        const tool = input.tool ?? ""
        if (!FILE_TOOL_RE.test(tool)) return

        const args = (input.args ?? {}) as Record<string, unknown> | undefined
        const title = output?.title ?? ""
        const mdPath = candidatePaths(args, title).find((p) => p.endsWith(".md"))
        if (!mdPath) return

        await runFormatter(mdPath)
      } catch (err) {
        await warnOnce("linter", `[markdown-format] error: ${err instanceof Error ? err.message : String(err)}`)
      }
    },
  }
}
