import type { Plugin } from "@opencode-ai/plugin"
import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { join, resolve } from "node:path"

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
 * `markdownlint-cli2 --fix` against it using the bundled config
 * (config/.markdownlint.json), silently correcting lint violations the model
 * introduced. Auto-fixing only — it never blocks, and every failure is logged
 * via client.app.log() and swallowed (the hook never throws), mirroring the
 * original Python hook's always-exit-0 behavior.
 *
 * Binary resolution matches the Python `shutil.which` order: a global
 * `markdownlint-cli2` is preferred, falling back to `npx markdownlint-cli2`.
 * Exit codes 0 (all fixed) and 1 (unfixable violations remain) are both
 * success; anything else is logged as a warning, never an error.
 */

// Plugin root = the plugin-markdown-format directory (this file lives in plugins/).
// import.meta.dir is the OpenCode (Bun) runtime API — same construct the
// memory-guard plugin uses. Fall back to import.meta.dirname for plain Node.
const PLUGIN_ROOT = resolve(
  (import.meta as ImportMeta & { dir?: string }).dir ?? import.meta.dirname,
  "..",
)

const BUNDLED_CONFIG = join(PLUGIN_ROOT, "config", ".markdownlint.json")

// File-touching tools: write/edit/patch and MCP-style variants
// (mcp__server__write_file, apply_patch, multi_edit, ...). Read/bash excluded.
const FILE_TOOL_RE = /(^|[._-])(write|edit|patch|multi_?edit)(_?file)?$/i

/** File-path-like args across OpenCode's file tools (edit, write, patch). */
const FILE_PATH_ARGS = ["filePath", "file_path", "path", "filepath"]

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
 */
function findExecutable(name: string): string | null {
  try {
    const res = spawnSync("sh", ["-c", `command -v ${name}`], { encoding: "utf8" })
    if (res.status === 0 && res.stdout) {
      const out = res.stdout.trim()
      if (out) return out
    }
  } catch {
    // fall through to the Windows check
  }
  try {
    const res = spawnSync("cmd", ["/c", "where", name], { encoding: "utf8" })
    if (res.status === 0 && res.stdout) {
      const out = res.stdout.split(/\r?\n/)[0]?.trim()
      if (out) return out
    }
  } catch {
    // not found
  }
  return null
}

export const MarkdownFormatPlugin: Plugin = async ({ client }) => {
  const log = async (
    level: "info" | "warn" | "error",
    message: string,
  ): Promise<void> => {
    try {
      await client.app.log({ body: { service: "markdown-format", level, message } })
    } catch {
      // logging must never break the hook
    }
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
        await log(
          "error",
          "[markdown-format] markdownlint-cli2 not found and npx unavailable. " +
            "Install: npm install -g markdownlint-cli2",
        )
        return
      }
      cmd = ["npx", "markdownlint-cli2", "--fix"]
    }

    if (existsSync(BUNDLED_CONFIG)) cmd.push("--config", BUNDLED_CONFIG)

    cmd.push(filePath)

    const result = spawnSync(cmd[0], cmd.slice(1), { encoding: "utf8" })

    // 0 = all fixed, 1 = unfixable violations remain — both are acceptable.
    if (result.status !== 0 && result.status !== 1) {
      await log(
        "warn",
        `[markdown-format] unexpected exit ${result.status}: ${(result.stderr ?? "").trim()}`,
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
      const tool = input.tool ?? ""
      if (!FILE_TOOL_RE.test(tool)) return

      const args = (input.args ?? {}) as Record<string, unknown> | undefined
      const title = output.title ?? ""
      const paths = candidatePaths(args, title)
      if (paths.length === 0) return

      const mdPath = paths.find((p) => p.endsWith(".md"))
      if (!mdPath) return

      try {
        await runFormatter(mdPath)
      } catch (err) {
        await log(
          "error",
          `[markdown-format] error: ${err instanceof Error ? err.message : String(err)}`,
        )
      }
    },
  }
}