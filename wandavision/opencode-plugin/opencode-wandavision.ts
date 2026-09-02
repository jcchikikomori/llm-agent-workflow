import type { Plugin } from "@opencode-ai/plugin"
import { isAbsolute, resolve } from "node:path"

/**
 * wandavision plugin — OpenCode port of the Claude Code PostToolUse hook
 * (hooks/wandavision_hook.py).
 *
 * Fires after any tool call, observing (never blocking):
 *
 * - Screenshot-capture tools (Playwright's browser_take_screenshot,
 *   chrome-devtools' take_screenshot, camoufox's browse_screenshot, ...) —
 *   reminder fires unconditionally, since their output is image content in the
 *   tool response rather than a guaranteed on-disk path. The wandavision skill
 *   is the primary enforcement for that case.
 * - File writes/edits touching an image under .wandavision_workspace/ — remind
 *   to run the image through the wandavision MCP server.
 * - Image files written outside the project worktree (e.g. /tmp screenshots) —
 *   warn that they must be saved under ./.wandavision_workspace/ first.
 *
 * Informational only — emits a log line via client.app.log(), never throws.
 */

const SCREENSHOT_TOOL_RE = /(_take_screenshot|browse_screenshot)$/i
const IMAGE_EXT_RE = /\.(png|jpe?g|gif|bmp|webp)$/i
const WORKSPACE_IMAGE_RE =
  /\.wandavision_workspace[/\\].*\.(png|jpe?g|gif|bmp|webp)$/i

const SCREENSHOT_TOOL_MESSAGE =
  "[wandavision] Screenshot tool just ran. Save its output image into " +
  "./.wandavision_workspace/ (create it if missing) and then call the " +
  "wandavision MCP tool (e.g. locate_objects) on it now -- never eyeball " +
  "or estimate pixel data from a screenshot."

const WORKSPACE_IMAGE_MESSAGE =
  "[wandavision] Image detected in .wandavision_workspace/. Call the " +
  "wandavision MCP tool (e.g. locate_objects) on it now instead of " +
  "reasoning about it visually."

const OUTSIDE_WORKSPACE_MESSAGE =
  "[wandavision] Image written outside the project worktree. Save it into " +
  "./.wandavision_workspace/ (create it if missing) before using it, then " +
  "call the wandavision MCP tool (e.g. locate_objects) on it -- never " +
  "eyeball or estimate pixel data from an image."

/** File-path-like args across OpenCode's file tools (edit, write, patch). */
const FILE_PATH_ARGS = ["filePath", "file_path", "path", "filepath"]

function isWorkspaceImage(p: string): boolean {
  return WORKSPACE_IMAGE_RE.test(p)
}

function isImagePath(p: string): boolean {
  return IMAGE_EXT_RE.test(p)
}

function isUnderRoot(p: string, root: string): boolean {
  const abs = isAbsolute(p) ? p : resolve(root, p)
  const prefix = root.endsWith("/") ? root : root + "/"
  return abs === root || abs.startsWith(prefix)
}

/**
 * Collect candidate paths from the tool call. For file tools this is the
 * filePath-style arg; for bash it's the command text (matches the original
 * Python hook, which scanned tool_input.command); for webfetch it's the URL.
 */
function candidatePaths(tool: string, args: Record<string, unknown> | undefined, title: string): string[] {
  const paths: string[] = []
  if (args && typeof args === "object") {
    for (const key of FILE_PATH_ARGS) {
      const v = args[key]
      if (typeof v === "string" && v) paths.push(v)
    }
    if (typeof args.command === "string" && args.command) paths.push(args.command)
    if (typeof args.url === "string" && args.url) paths.push(args.url)
  }
  // OpenCode titles for file tools look like "Edited src/foo.png" / "Wrote foo.png"
  if (title) {
    const m = title.match(/(?:Edited|Wrote|Created|Updated|Patched)\s+(.+)$/)
    if (m) paths.push(m[1])
  }
  return paths
}

export const Wandavision: Plugin = async ({ client, directory, worktree }) => {
  const projectRoot = directory || worktree
  const log = (message: string) =>
    client.app.log({
      body: {
        service: "wandavision",
        level: "info",
        message,
      },
    })

  return {
    "tool.execute.after": async (input, output) => {
      const tool = input.tool ?? ""
      const args = (input.args ?? {}) as Record<string, unknown> | undefined
      const title = output.title ?? ""

      // Screenshot-capture tools (MCP tools included, e.g.
      // mcp__playwright__browser_take_screenshot): fire unconditionally.
      if (SCREENSHOT_TOOL_RE.test(tool)) {
        await log(SCREENSHOT_TOOL_MESSAGE)
        return
      }

      // webfetch of an image URL is a screenshot-like read — same reminder.
      if (tool === "webfetch" && typeof args?.url === "string" && isImagePath(args.url)) {
        await log(SCREENSHOT_TOOL_MESSAGE)
        return
      }

      const paths = candidatePaths(tool, args, title)
      if (paths.length === 0) return

      // Image under .wandavision_workspace/ → route it through the MCP server.
      for (const p of paths) {
        if (isWorkspaceImage(p)) {
          await log(WORKSPACE_IMAGE_MESSAGE)
          return
        }
      }

      // Workspace enforcement: an image landing outside the project worktree
      // (e.g. /tmp/screenshot.png) must be moved into the workspace. Only
      // checked for file tools — bash command strings can mix in/out paths and
      // would false-positive.
      if (projectRoot && (tool === "edit" || tool === "write")) {
        for (const p of paths) {
          if (isImagePath(p) && !isUnderRoot(p, projectRoot)) {
            await log(OUTSIDE_WORKSPACE_MESSAGE)
            return
          }
        }
      }
    },
  }
}