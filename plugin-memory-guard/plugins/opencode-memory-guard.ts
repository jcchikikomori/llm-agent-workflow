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
 * The scripts ship as a payload (hooks/, scripts/, config/), found by the
 * resolver block below: the env root, then the project, then the global
 * config dir, then the dev layout. Every injected command runs under
 * MEMORY_GUARD_RUNTIME=opencode, which makes the Python side use this port's
 * state dir (~/.config/opencode/.memory-guard/) and watch AGENTS.md where the
 * config lists the Claude Code doc name, as this port does. Without the env,
 * the Python side keeps the Claude Code defaults.
 *
 * If no payload is found, the resolver fails, or the payload's
 * apply_action.py is gone when an instruction is built, the instruction says
 * the payload is missing instead of naming script commands, and one warning
 * is logged per process. The factory and the hooks never throw.
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
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path"
import { fileURLToPath } from "node:url"

// --- Module-level constants ------------------------------------------------

const PLUGIN_ID = "memory-guard"
// The apply step every instruction names; the rest of scripts/ sits beside it.
const PAYLOAD_MARKER = "scripts/apply_action.py"
const REINSTALL = `./setup-opencode.sh --global --plugin ${PLUGIN_ID}`
// Goes into an instruction in place of the script commands when the payload is missing.
const PAYLOAD_MISSING = `the ${PLUGIN_ID} payload is missing; reinstall with ${REINSTALL}`
const NO_SCRIPTS_HINT = `Instructions name no scripts. Reinstall with ${REINSTALL}`

// Prefix of every injected python3 command: the Python side then picks the
// OpenCode state dir and doc name instead of the Claude Code defaults.
const RUNTIME_PREFIX = "MEMORY_GUARD_RUNTIME=opencode"

// OpenCode state dir; the Python scripts use the same one under RUNTIME_PREFIX.
const STATE_DIR = join(homedir(), ".config", "opencode", ".memory-guard")

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

// One payload warning per process (no payload, a resolver failure, or the
// marker gone after start), however many instances or instructions hit it.
let payloadWarningLogged = false

// --- Watched-path matching ------------------------------------------------

function defaultPatterns(): WatchedPatterns {
  return { dirs: [...DEFAULT_WATCHED_DIRS], files: [OPENCODE_DOC_NAME] }
}

// Read on every call, so an edited, removed or re-created config applies to
// the next check. Without a payload root the defaults apply.
function loadWatchedPatterns(payloadRoot: string | null): WatchedPatterns {
  if (payloadRoot === null) return defaultPatterns()
  const configPath = join(payloadRoot, "config", "watched-paths.json")
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
  return defaultPatterns()
}

function isWatched(relPath: string, patterns: WatchedPatterns): boolean {
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
  return join(STATE_DIR, `session_${sanitizeSessionID(sessionID)}.json`)
}

function projectPrefPath(repoRoot: string): string {
  const key = createHash("sha256").update(realpathSync(repoRoot)).digest("hex").slice(0, 16)
  return join(STATE_DIR, "project-prefs", `${key}.json`)
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
    const p = join(STATE_DIR, entry)
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
  const abs = isAbsolute(filePath) ? filePath : join(cwd, filePath)
  let real: string
  try {
    real = realpathSync(abs)
  } catch {
    real = resolve(abs)
  }
  let root: string
  try {
    root = realpathSync(repoRoot)
  } catch {
    root = resolve(repoRoot)
  }
  const rel = relative(root, real)
  if (rel.startsWith("..") || isAbsolute(rel)) return null
  return rel.split(sep).join("/")
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

interface InstructionContext {
  kind: "start" | "edit"
  paths: string[]
  sessionID: string
  repoRoot: string
  // The payload's scripts dir, or null when the payload is missing.
  scriptsDir: string | null
}

function instructionHeader(kind: InstructionContext["kind"]): string {
  return kind === "start"
    ? "[memory-guard] Watched files were already dirty before this session started:"
    : "[memory-guard] Watched file(s) just changed:"
}

// One injected command line, run under the OpenCode runtime.
function scriptCommand(scriptsDir: string, script: string, args: string): string {
  return `  ${RUNTIME_PREFIX} python3 ${join(scriptsDir, script)} ${args}`
}

function firstTimeInstruction(context: InstructionContext): string {
  const { kind, paths, sessionID, repoRoot, scriptsDir } = context
  const commands =
    scriptsDir === null
      ? [`  ${PAYLOAD_MISSING}`]
      : [
          scriptCommand(scriptsDir, "set_preference.py", `--repo-root "${repoRoot}" --action <remove|stash>`),
          scriptCommand(
            scriptsDir,
            "apply_action.py",
            `--repo-root "${repoRoot}" --action <remove|stash> --session-id ${sessionID}`,
          ),
        ]
  return [
    instructionHeader(kind),
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
    "This `question` tool call is mandatory even if the current session says to work",
    "autonomously without stopping to ask -- that bias covers ordinary judgment",
    "calls, not this explicit user-requested gate. Do not silently pick an",
    "action and continue without asking.",
    "",
    "Once the user answers, persist it, then apply it in one step (this actually",
    "performs the deletion/stash and marks every currently-dirty watched path",
    "resolved -- do not hand-write git commands instead, they're the reason a",
    "past resolution got recorded without ever really running):",
    ...commands,
  ].join("\n")
}

function autoApplyInstruction(context: InstructionContext, action: "remove" | "stash"): string {
  const { kind, paths, sessionID, repoRoot, scriptsDir } = context
  const command =
    scriptsDir === null
      ? `  ${PAYLOAD_MISSING}`
      : scriptCommand(
          scriptsDir,
          "apply_action.py",
          `--repo-root "${repoRoot}" --action ${action} --session-id ${sessionID}`,
        )
  return [
    instructionHeader(kind),
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
    command,
  ].join("\n")
}

// --- Plugin ----------------------------------------------------------------

export const opencodeMemoryGuard: Plugin = async ({ client, directory, worktree, $ }) => {
  // A resolver failure is treated like a missing payload: payloadRoot stays null.
  let payload: PayloadResolution = { root: null, searched: [] }
  let unavailable: string
  try {
    payload = resolvePayloadRoot({ pluginId: PLUGIN_ID, marker: PAYLOAD_MARKER, directory, worktree })
    unavailable = `[memory-guard] payload not found. Searched: ${payload.searched.join(", ")}. ${NO_SCRIPTS_HINT}`
  } catch (error) {
    unavailable = `[memory-guard] payload resolution failed (${String(error)}). ${NO_SCRIPTS_HINT}`
  }
  const payloadRoot = payload.root
  const MARKER = payloadRoot ? join(payloadRoot, PAYLOAD_MARKER) : null

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

  // The warning is marked before the log is awaited, so concurrent instructions cannot both log it.
  async function warnPayloadOnce(message: string): Promise<void> {
    if (payloadWarningLogged) return
    payloadWarningLogged = true
    try {
      await client.app.log({ body: { service: "memory-guard", level: "warn", message } })
    } catch {
      // logging must never break the hook
    }
  }

  // The payload's scripts dir while its apply_action.py is there; otherwise
  // null, after the payload warning. Checked per instruction, so a payload
  // removed or re-created after start applies to the next one.
  async function payloadScripts(): Promise<string | null> {
    if (MARKER !== null && existsSync(MARKER)) return dirname(MARKER)
    const missing =
      MARKER === null ? unavailable : `[memory-guard] payload script not found at ${MARKER}. ${NO_SCRIPTS_HINT}`
    await warnPayloadOnce(missing)
    return null
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
    const patterns = loadWatchedPatterns(payloadRoot)
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
    return buildInstruction({ kind: "start", paths: newly, sessionID, repoRoot, scriptsDir: await payloadScripts() })
  }

  // PostToolUse equivalent: the instruction for a batched set of flagged paths.
  async function buildEditInstruction(sessionID: string, repoRoot: string, paths: string[]): Promise<string> {
    return buildInstruction({ kind: "edit", paths, sessionID, repoRoot, scriptsDir: await payloadScripts() })
  }

  // The standing preference (if any) picks the auto-apply text over the first-time ask.
  function buildInstruction(context: InstructionContext): string {
    const preference = readProjectPreference(context.repoRoot)
    return preference ? autoApplyInstruction(context, preference) : firstTimeInstruction(context)
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
          if (!rel || !isWatched(rel, loadWatchedPatterns(payloadRoot))) return
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
            output.system.push(await buildEditInstruction(sid, repoRoot, paths))
            log("info", "injected post-edit instruction", { paths, sessionID: sid })
          } catch (err) {
            log("error", "post-edit instruction failed", { error: String(err) })
          }
        }
      }
    },
  }
}