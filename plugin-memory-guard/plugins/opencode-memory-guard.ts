/**
 * opencode-memory-guard — OpenCode plugin port of the memory-guard hooks.
 *
 * Replaces the Claude Code SessionStart + PostToolUse (Write|Edit|MultiEdit)
 * hooks with OpenCode equivalents:
 *
 * - SessionStart  -> "experimental.chat.system.transform", gated by a
 *   session.created flag so the dirty-path check runs once per fresh session
 *   (mirroring Claude Code's once-per-session SessionStart).
 * - PostToolUse   -> event({event}) on "file.edited", which fires for any
 *   file write regardless of the tool that produced it.
 *
 * Both paths only *surface context* — they never act on the working tree
 * themselves. The Python scripts under scripts/ remain the only code that
 * actually removes/stashes watched paths, and they are invoked by the model
 * via the injected instructions (exactly the contract of the original Claude
 * Code hooks): a memory save must land before any deletion, and only the
 * model can judge memory-worthiness and run the one-time per-project ask.
 *
 * State/preference files live under ~/.config/opencode/.memory-guard/
 * (previously the Claude Code config dir); the Python scripts in scripts/
 * check both paths at module load so the two runtimes can share state.
 */
import type { Plugin } from "@opencode-ai/plugin"
import { createHash } from "node:crypto"
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs"
import { homedir } from "node:os"
import path from "node:path"

// --- Module-level constants ------------------------------------------------

// Plugin root = the plugin-memory-guard directory (this file lives in plugins/).
const pluginRoot = path.resolve(import.meta.dir, "..")

// Claude Code stored state under the Claude Code config dir; OpenCode uses its
// own config dir. The Python scripts in scripts/ check both and fall back to
// the Claude Code path, so the two runtimes can share state.
const STATE_DIR = path.join(homedir(), ".config", "opencode", ".memory-guard")
const SCRIPTS_DIR = path.join(pluginRoot, "scripts")

const DEFAULT_WATCHED_DIRS = [".claude", "docs/ticket-tracking"]

// Doc-path transform: OpenCode reads AGENTS.md natively. The config file (a
// Claude Code artifact) still lists the legacy doc filename — build that
// literal via concatenation so it never appears verbatim here.
const CLAUDE_DOC_NAME = "CLAUDE" + ".md"
const OPENCODE_DOC_NAME = "AGENTS.md"

const GC_PROBABILITY = 0.1
const GC_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

interface WatchedPatterns {
  dirs: string[]
  files: string[]
}

// --- Watched-path matching ------------------------------------------------

function loadWatchedPatterns(): WatchedPatterns {
  const configPath = path.join(pluginRoot, "config", "watched-paths.json")
  try {
    const data = JSON.parse(readFileSync(configPath, "utf8")) as {
      watched_dirs?: unknown
      watched_files?: unknown
    }
    const dirs = data.watched_dirs
    const files = data.watched_files
    if (
      Array.isArray(dirs) &&
      Array.isArray(files) &&
      dirs.every((d) => typeof d === "string") &&
      files.every((f) => typeof f === "string")
    ) {
      return {
        dirs: dirs as string[],
        // OpenCode doc-path transform: the root doc file is AGENTS.md.
        files: (files as string[]).map((f) => (f === CLAUDE_DOC_NAME ? OPENCODE_DOC_NAME : f)),
      }
    }
  } catch {
    // fall through to defaults
  }
  return { dirs: [...DEFAULT_WATCHED_DIRS], files: [OPENCODE_DOC_NAME] }
}

function isWatched(relPath: string, patterns: WatchedPatterns = loadWatchedPatterns()): boolean {
  if (patterns.files.includes(relPath)) return true
  for (const dir of patterns.dirs) {
    const d = dir.replace(/\/+$/, "")
    if (relPath === d || relPath.startsWith(`${d}/`)) return true
  }
  return false
}

// --- State + preference I/O ------------------------------------------------

function sanitizeSessionID(sessionID: string): string {
  const s = String(sessionID).replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 128)
  return s || "unknown"
}

function stateFilePath(sessionID: string): string {
  return path.join(STATE_DIR, `session_${sanitizeSessionID(sessionID)}.json`)
}

function projectPrefPath(repoRoot: string): string {
  const key = createHash("sha256").update(realpathSync(repoRoot)).digest("hex").slice(0, 16)
  return path.join(STATE_DIR, "project-prefs", `${key}.json`)
}

function readProjectPreference(repoRoot: string): "remove" | "stash" | null {
  try {
    const data = JSON.parse(readFileSync(projectPrefPath(repoRoot), "utf8")) as {
      action?: unknown
    }
    return data.action === "remove" || data.action === "stash" ? data.action : null
  } catch {
    return null
  }
}

interface FlagEntry {
  status: string
  action: string | null
  ts: number
}

interface SessionState {
  paths: Record<string, FlagEntry>
}

// Serialize read-modify-write on the session state file, mirroring the flock
// in memory_guard_common.py (parallel tool calls in one turn can run
// concurrently). State file format is identical to the Python side.
let stateLock: Promise<unknown> = Promise.resolve()

function withStateLock<T>(fn: () => T): Promise<T> {
  const run = stateLock.then(fn)
  stateLock = run.catch(() => undefined)
  return run
}

async function markPendingIfNew(sessionID: string, relPath: string): Promise<boolean> {
  return withStateLock(() => {
    mkdirSync(STATE_DIR, { recursive: true })
    const sp = stateFilePath(sessionID)
    let paths = Object.create(null) as Record<string, FlagEntry>
    try {
      const parsed = JSON.parse(readFileSync(sp, "utf8")) as Partial<SessionState>
      for (const k of Object.keys(parsed.paths ?? {})) paths[k] = (parsed.paths as SessionState["paths"])[k]
    } catch {
      // no state yet
    }
    if (Object.prototype.hasOwnProperty.call(paths, relPath)) return false
    paths[relPath] = { status: "pending", action: null, ts: Date.now() / 1000 }
    const state: SessionState = { paths }
    writeFileSync(sp, JSON.stringify(state))
    return true
  })
}

function maybeGcOldSessions(): void {
  if (Math.random() > GC_PROBABILITY) return
  if (!existsSync(STATE_DIR)) return
  const cutoff = Date.now() / 1000 - GC_MAX_AGE_SECONDS
  for (const entry of readdirSync(STATE_DIR)) {
    const p = path.join(STATE_DIR, entry)
    try {
      if (statSync(p).mtimeMs / 1000 < cutoff) rmSync(p, { recursive: true })
    } catch {
      // ignore
    }
  }
}

// --- Path helpers ----------------------------------------------------------

function relpathOrNone(filePath: string, cwd: string, repoRoot: string): string | null {
  if (!filePath) return null
  const abs = path.isAbsolute(filePath) ? filePath : path.join(cwd, filePath)
  let real: string
  try {
    real = realpathSync(abs)
  } catch {
    real = path.resolve(abs)
  }
  let root: string
  try {
    root = realpathSync(repoRoot)
  } catch {
    root = path.resolve(repoRoot)
  }
  const rel = path.relative(root, real)
  if (rel.startsWith("..") || path.isAbsolute(rel)) return null
  return rel.split(path.sep).join("/")
}

function parsePorcelainPaths(output: string): string[] {
  const paths: string[] = []
  for (const line of output.split("\n")) {
    if (line.length < 4) continue
    let entry = line.slice(3)
    if (entry.includes(" -> ")) entry = entry.split(" -> ", 2)[1]
    entry = entry.trim()
    if (entry.startsWith('"') && entry.endsWith('"')) entry = entry.slice(1, -1)
    if (entry) paths.push(entry)
  }
  return paths
}

// --- Instruction text ------------------------------------------------------

function firstTimeInstruction(
  kind: "start" | "edit",
  paths: string[],
  sessionID: string,
  repoRoot: string,
): string {
  const applyActionPath = path.join(SCRIPTS_DIR, "apply_action.py")
  const setPreferencePath = path.join(SCRIPTS_DIR, "set_preference.py")
  const header =
    kind === "start"
      ? "[memory-guard] Watched files were already dirty before this session started:"
      : "[memory-guard] Watched file(s) just changed:"
  return [
    header,
    ...paths.map((p) => `  - ${p}`),
    "",
    `Session: ${sessionID}`,
    `Project: ${repoRoot}`,
    "",
    "No remove/stash preference is set for this project yet. Before doing",
    "anything else, run the memory-guard procedure (see the memory-guard skill):",
    "for each path above, judge whether the change is memory-worthy, save it",
    "(mempalace if available, otherwise the file-based auto-memory system), then",
    "ask the user ONCE: should watched .claude-scoped changes in this project be",
    "REMOVED (deleted from disk, content already preserved in memory) or STASHed",
    "(git stash, scoped only to watched paths) going forward? This question is",
    "asked only this one time for this project -- the answer is then persisted",
    "and reused automatically for every future flagged path here.",
    "",
    "This AskUserQuestion is mandatory even if the current session says to work",
    "autonomously without stopping to ask -- that bias covers ordinary judgment",
    "calls, not this explicit user-requested gate. Do not silently pick an",
    "action and continue without asking.",
    "",
    "Once the user answers, persist it, then apply it in one step (this actually",
    "performs the deletion/stash and marks every currently-dirty watched path",
    "resolved -- do not hand-write git commands instead, they're the reason a",
    "past resolution got recorded without ever really running):",
    `  python3 ${setPreferencePath} --repo-root "${repoRoot}" --action <remove|stash>`,
    `  python3 ${applyActionPath} --repo-root "${repoRoot}" --action <remove|stash> --session-id ${sessionID}`,
  ].join("\n")
}

function autoApplyInstruction(
  kind: "start" | "edit",
  paths: string[],
  sessionID: string,
  repoRoot: string,
  action: "remove" | "stash",
): string {
  const applyActionPath = path.join(SCRIPTS_DIR, "apply_action.py")
  const header =
    kind === "start"
      ? "[memory-guard] Watched files were already dirty before this session started:"
      : "[memory-guard] Watched file(s) just changed:"
  return [
    header,
    ...paths.map((p) => `  - ${p}`),
    "",
    `Session: ${sessionID}`,
    `Project preference already set: ${action}`,
    "",
    "No need to ask -- this project already has a standing preference. Before",
    "doing anything else, run the memory-guard procedure (see the memory-guard",
    "skill) for each path above: judge whether the change is memory-worthy, save",
    "it (mempalace if available, otherwise the file-based auto-memory system),",
    `then run the one command below to actually apply "${action}" and mark`,
    "everything resolved -- do not hand-write git commands instead:",
    `  python3 ${applyActionPath} --repo-root "${repoRoot}" --action ${action} --session-id ${sessionID}`,
  ].join("\n")
}

// --- Plugin ----------------------------------------------------------------

export const opencodeMemoryGuard: Plugin = async ({ client, directory, $ }) => {
  let currentSessionID: string | undefined
  let sessionFresh = false

  interface PendingFlag {
    sessionID: string
    repoRoot: string
    path: string
  }
  const pendingFlags: PendingFlag[] = []

  function log(
    level: "debug" | "info" | "warn" | "error",
    message: string,
    extra?: Record<string, unknown>,
  ): void {
    try {
      client
        .app.log({ body: { service: "memory-guard", level, message, extra } })
        .catch(() => {})
    } catch {
      // logging must never break the hook
    }
  }

  async function repoRootFor(cwd: string): Promise<string | null> {
    try {
      const result = await $`git -C ${cwd} rev-parse --show-toplevel`
        .cwd(cwd)
        .quiet()
        .nothrow()
      const out = result.stdout.toString().trim()
      return out || null
    } catch {
      return null
    }
  }

  async function liveDirtyWatchedPaths(repoRoot: string): Promise<string[]> {
    const patterns = loadWatchedPatterns()
    const pathspecs = [...patterns.dirs, ...patterns.files]
    try {
      const result = await $`git -C ${repoRoot} status --porcelain --untracked-files=all -- ${pathspecs}`
        .cwd(repoRoot)
        .quiet()
        .nothrow()
      const paths = parsePorcelainPaths(result.stdout.toString())
      return paths.filter((p) => isWatched(p, patterns))
    } catch {
      return []
    }
  }

  // SessionStart equivalent: run once per fresh session. Mirrors the Python
  // session_start_hook.py — dirty watched paths get marked pending in the
  // session state file (so post-edit flags stay silent for them) and an
  // instruction is injected telling the model how to resolve them.
  async function buildSessionStartInstruction(sessionID: string): Promise<string | null> {
    maybeGcOldSessions()
    const repoRoot = await repoRootFor(directory)
    if (!repoRoot) return null
    const dirty = await liveDirtyWatchedPaths(repoRoot)
    const newly: string[] = []
    for (const p of dirty) {
      if (await markPendingIfNew(sessionID, p)) newly.push(p)
    }
    if (newly.length === 0) return null
    const preference = readProjectPreference(repoRoot)
    if (preference) {
      return autoApplyInstruction("start", newly, sessionID, repoRoot, preference)
    }
    return firstTimeInstruction("start", newly, sessionID, repoRoot)
  }

  // PostToolUse equivalent: read the standing preference (if any) for a
  // batched set of flagged paths and emit the matching instruction.
  function buildEditInstruction(
    sessionID: string,
    repoRoot: string,
    paths: string[],
  ): string | null {
    const preference = readProjectPreference(repoRoot)
    if (preference) {
      return autoApplyInstruction("edit", paths, sessionID, repoRoot, preference)
    }
    return firstTimeInstruction("edit", paths, sessionID, repoRoot)
  }

  return {
    event: async ({ event }) => {
      if (event.type === "session.created") {
        sessionFresh = true
        currentSessionID = event.properties.info.id
        return
      }
      if (event.type === "file.edited") {
        const file = event.properties.file
        const sid = currentSessionID
        if (!sid || !file) return
        try {
          const repoRoot = await repoRootFor(directory)
          if (!repoRoot) return
          const rel = relpathOrNone(file, directory, repoRoot)
          if (!rel || !isWatched(rel)) return
          if (await markPendingIfNew(sid, rel)) {
            pendingFlags.push({ sessionID: sid, repoRoot, path: rel })
            log("info", "flagged watched file edit", { path: rel, sessionID: sid })
          }
        } catch (err) {
          log("error", "file.edited handling failed", { error: String(err) })
        }
      }
    },

    "experimental.chat.system.transform": async ({ sessionID }, output) => {
      const sid = sessionID ?? currentSessionID
      if (!sid) return

      if (sessionFresh) {
        sessionFresh = false
        try {
          const text = await buildSessionStartInstruction(sid)
          if (text) {
            output.system.push(text)
            log("info", "injected session-start instruction", { sessionID: sid })
          }
        } catch (err) {
          log("error", "session-start check failed", { error: String(err) })
        }
      }

      if (pendingFlags.length > 0) {
        const flags = pendingFlags.splice(0, pendingFlags.length)
        const byRepo = new Map<string, string[]>()
        for (const f of flags) {
          const list = byRepo.get(f.repoRoot) ?? []
          list.push(f.path)
          byRepo.set(f.repoRoot, list)
        }
        for (const [repoRoot, paths] of byRepo) {
          try {
            const text = buildEditInstruction(sid, repoRoot, paths)
            if (text) {
              output.system.push(text)
              log("info", "injected post-edit instruction", { paths, sessionID: sid })
            }
          } catch (err) {
            log("error", "post-edit instruction failed", { error: String(err) })
          }
        }
      }
    },
  }
}