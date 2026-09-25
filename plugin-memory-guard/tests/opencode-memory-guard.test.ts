// memory-guard opencode port Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.5.4)
// Generated: 2026-09-24 | Budget Used: 1/3 integration (memory-guard slice), 0/2 E2E
//
// Run with (HOME and the bun caches pointed outside the real home):
//
//   bun test plugin-memory-guard/tests
//
// Harness (Design Doc "Test Strategy > TS"; scripts/opencode/tests/support/scope.ts):
// - Each test builds a temp scope under os.tmpdir(): HOME=<tmp>/home, XDG_CONFIG_HOME=<tmp>/config, and
//   <tmp>/config/opencode/plugins as a symlink to <tmp>/dotfiles/plugins (the live layout, N20). The port is copied
//   to <tmp>/dotfiles/plugins/opencode-memory-guard.ts. The project dir is a temp git repo with AGENTS.md committed.
// - The payload is the real hooks/, scripts/ and config/ of plugin-memory-guard, copied to the candidate under test
//   (installPayload for the global scope).
// - Every instruction path writes session state under os.homedir()/.config/opencode/.memory-guard, and Bun caches
//   os.homedir() at startup. So the port runs only in a child bun (process.execPath) started with a fully built env:
//   HOME, XDG_*, TMPDIR, BUN_INSTALL_CACHE_DIR, PATH, PYTHONDONTWRITEBYTECODE, GIT_CONFIG_GLOBAL, GIT_CONFIG_NOSYSTEM
//   and GIT_TERMINAL_PROMPT, plus LLM_AGENT_WORKFLOW_PAYLOAD_ROOT where a test sets it. Nothing is inherited but
//   PATH, so no GIT_* from the caller reaches the port. The child imports the port through the symlinked path, so
//   Bun reports import.meta.url at the realpath (N19), as opencode does. One child is one process, so "once per
//   process" is "once per child" here.
// - The child runs a scenario (a JSON list of steps: start an instance with a recording fake client, send
//   session.created or file.edited, run experimental.chat.system.transform, or change a file) and prints
//   { systems, logs }. The port spawns only through the `$` it is given (Bun's), which reads the live process.env.
// - Every scenario result is also checked for two invariants: each system line that runs python3 starts with
//   "MEMORY_GUARD_RUNTIME=opencode python3 ", and no emitted string (instruction or log) contains AskUserQuestion.

import { afterEach, describe, expect, setDefaultTimeout, test } from "bun:test"
import { spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import { cpSync, existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { copyPort, installPayload, makeScope, REPO_ROOT, type Scope } from "../../scripts/opencode/tests/support/scope"

const PORT = "plugin-memory-guard/plugins/opencode-memory-guard.ts"
const PLUGIN_ID = "memory-guard"
const PAYLOAD = join("llm-agent-workflow", PLUGIN_ID)
const PAYLOAD_SOURCES = ["hooks", "scripts", "config"].map((dir) => join("plugin-memory-guard", dir))
// A child that hangs fails runScenario's status/stderr check, well inside the per-test timeout.
const CHILD_TIMEOUT_MS = 30_000

setDefaultTimeout(60_000)

const RUNTIME_LINE = "  MEMORY_GUARD_RUNTIME=opencode python3 "
const REINSTALL = "./setup-opencode.sh --global --plugin memory-guard"
const MISSING_LINE = `  the memory-guard payload is missing; reinstall with ${REINSTALL}`
const NO_SCRIPTS_HINT = `Instructions name no scripts. Reinstall with ${REINSTALL}`

// Every transform starts from this system prompt, so "untouched" means exactly [BASE].
const BASE = "base system prompt"

const HEADERS = {
  start: "[memory-guard] Watched files were already dirty before this session started:",
  edit: "[memory-guard] Watched file(s) just changed:",
}

const FIRST_TIME_BODY = [
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
]

type Kind = keyof typeof HEADERS
type Action = "remove" | "stash"
type ClientKind = "recorder" | "slow" | "rejecting" | "throwing"

interface Instruction {
  kind: Kind
  paths: string[]
  session: string
  repo: string
  // The payload's scripts dir the instruction must name, or null for the payload-missing line.
  scripts: string | null
}

type Step =
  | { op: "start"; name: string; directory: unknown; worktree: unknown; client: ClientKind }
  | { op: "created" | "transform"; name: string; session: string }
  | { op: "edited"; name: string; file: string }
  | { op: "write"; path: string; content: string }
  | { op: "rm"; path: string }
  | { op: "concurrent"; steps: Step[] }

interface Result {
  systems: string[][]
  logs: Record<string, unknown[]>
}

interface Roots {
  directory: unknown
  worktree: unknown
}

type EnvOverrides = Record<string, string | undefined>

// The expected first-time ask: header, paths, session and project, the fixed body, then the commands.
function firstTime(expected: Instruction): string {
  const { kind, paths, session, repo, scripts } = expected
  const commands =
    scripts === null
      ? [MISSING_LINE]
      : [
          `${RUNTIME_LINE}${scripts}/set_preference.py --repo-root "${repo}" --action <remove|stash>`,
          `${RUNTIME_LINE}${scripts}/apply_action.py --repo-root "${repo}" --action <remove|stash> ` +
            `--session-id ${session}`,
        ]
  const head = [HEADERS[kind], ...paths.map((p) => `  - ${p}`), "", `Session: ${session}`, `Project: ${repo}`, ""]
  return [...head, ...FIRST_TIME_BODY, ...commands].join("\n")
}

// The expected auto-apply text for a project whose preference is ACTION.
function autoApply(expected: Instruction, action: Action): string {
  const { kind, paths, session, repo, scripts } = expected
  const command =
    scripts === null
      ? MISSING_LINE
      : `${RUNTIME_LINE}${scripts}/apply_action.py --repo-root "${repo}" --action ${action} --session-id ${session}`
  return [
    HEADERS[kind],
    ...paths.map((p) => `  - ${p}`),
    "",
    `Session: ${session}`,
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

function warn(message: string): unknown {
  return { body: { service: PLUGIN_ID, level: "warn", message } }
}

function info(message: string, extra: Record<string, unknown>): unknown {
  return { body: { service: PLUGIN_ID, level: "info", message, extra } }
}

const startLog = (session: string) => info("injected session-start instruction", { sessionID: session })
const flagLog = (path: string, session: string) => info("flagged watched file edit", { path, sessionID: session })
const editLog = (paths: string[], session: string) =>
  info("injected post-edit instruction", { paths, sessionID: session })

function missingMessage(searched: string[]): string {
  return `[memory-guard] payload not found. Searched: ${searched.join(", ")}. ${NO_SCRIPTS_HINT}`
}

function failedMessage(reason: string): string {
  return `[memory-guard] payload resolution failed (${reason}). ${NO_SCRIPTS_HINT}`
}

function removedMessage(marker: string): string {
  return `[memory-guard] payload script not found at ${marker}. ${NO_SCRIPTS_HINT}`
}

// The text of what FN throws, as String(error) renders it; the runtime's own wording, not the port's.
function thrownText(fn: () => unknown): string {
  try {
    fn()
  } catch (error) {
    return String(error)
  }
  throw new Error("expected the call to throw")
}

// What the port's resolver throws for worktree: 42 (the same join, on its second project root).
function resolverFailure(): string {
  return thrownText(() => join(42 as unknown as string, ".opencode", PAYLOAD))
}

// --- Scope, repo and payload helpers ----------------------------------------

const scopes: Scope[] = []

afterEach(() => {
  for (const scope of scopes.splice(0)) scope.cleanup()
})

function newScope(): Scope {
  const scope = makeScope({ kind: "global", symlinkPlugins: true })
  scopes.push(scope)
  mkdirSync(join(scope.root, "tmp"))
  git(scope, "init", "-q")
  writeFileSync(join(scope.projectDir, "AGENTS.md"), "committed\n")
  git(scope, "add", "AGENTS.md")
  git(scope, "commit", "-q", "-m", "init")
  return scope
}

// The env of every child: built from scratch, with every path inside the scope. OVERRIDES of undefined drop a key.
function childEnv(scope: Scope, overrides: EnvOverrides = {}): Record<string, string> {
  const env: EnvOverrides = {
    PATH: process.env.PATH ?? "",
    HOME: scope.home,
    XDG_CONFIG_HOME: scope.configHome,
    XDG_CACHE_HOME: join(scope.root, "cache"),
    XDG_DATA_HOME: join(scope.root, "data"),
    BUN_INSTALL_CACHE_DIR: join(scope.root, "cache", "bun"),
    TMPDIR: join(scope.root, "tmp"),
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: scope.gitconfig,
    GIT_TERMINAL_PROMPT: "0",
    PYTHONDONTWRITEBYTECODE: "1",
    ...overrides,
  }
  return Object.fromEntries(Object.entries(env).filter((entry): entry is [string, string] => entry[1] !== undefined))
}

function git(scope: Scope, ...args: string[]): string {
  const result = spawnSync("git", ["-C", scope.projectDir, ...args], { env: childEnv(scope), encoding: "utf8" })
  if (result.status !== 0) throw new Error(`git ${args.join(" ")} failed: ${result.stderr}`)
  return result.stdout
}

function repoFile(scope: Scope, path: string): string {
  return join(scope.projectDir, path)
}

// Makes PATH in the project repo dirty: overwrites a committed file or creates an untracked one.
function dirty(scope: Scope, path: string): void {
  mkdirSync(join(repoFile(scope, path), ".."), { recursive: true })
  writeFileSync(repoFile(scope, path), "dirty\n")
}

function projectRoots(scope: Scope): Roots {
  return { directory: scope.projectDir, worktree: scope.projectDir }
}

function globalPayload(scope: Scope): string {
  return join(scope.configHome, "opencode", PAYLOAD)
}

function projectPayload(root: string): string {
  return join(root, ".opencode", PAYLOAD)
}

// The candidates searched with directory = worktree = the project, as the missing-payload warning lists them.
function noPayloadSearched(scope: Scope): string[] {
  return [projectPayload(scope.projectDir), globalPayload(scope), join(scope.root, "dotfiles")]
}

// Copies the real payload dirs to DEST, without bytecode; returns DEST/scripts.
function placePayload(dest: string): string {
  for (const source of PAYLOAD_SOURCES) {
    cpSync(join(REPO_ROOT, source), join(dest, source.split("/")[1]), {
      recursive: true,
      filter: (src) => !src.includes("__pycache__") && !src.endsWith(".pyc"),
    })
  }
  return join(dest, "scripts")
}

// The global payload through the shared harness; returns its scripts dir.
function installGlobal(scope: Scope): string {
  return join(installPayload(scope, PLUGIN_ID, PAYLOAD_SOURCES), "scripts")
}

// The port's (and Python's) project-prefs file for the project repo, under HOME's opencode state dir.
function prefFile(scope: Scope): string {
  const key = createHash("sha256").update(realpathSync(scope.projectDir)).digest("hex").slice(0, 16)
  return join(scope.home, ".config", "opencode", ".memory-guard", "project-prefs", `${key}.json`)
}

function sessionFile(scope: Scope, session: string): string {
  return join(scope.home, ".config", "opencode", ".memory-guard", `session_${session}.json`)
}

// --- Scenario steps -----------------------------------------------------------

function start(name: string, roots: Roots, client: ClientKind = "recorder"): Step {
  return { op: "start", name, directory: roots.directory, worktree: roots.worktree, client }
}

function created(name: string, session: string): Step {
  return { op: "created", name, session }
}

function transform(name: string, session: string): Step {
  return { op: "transform", name, session }
}

// session.created, then the first transform for it: the SessionStart equivalent.
function sessionStart(name: string, session: string): Step[] {
  return [created(name, session), transform(name, session)]
}

function edited(name: string, file: string): Step {
  return { op: "edited", name, file }
}

function write(path: string, content: string): Step {
  return { op: "write", path, content }
}

function rm(path: string): Step {
  return { op: "rm", path }
}

// Reads a scenario (argv[3]), imports the port (argv[2]), runs each step and prints { systems, logs } as JSON.
// A transform takes its systems slot when it starts, so concurrent steps keep their listed order.
const CHILD_PROBE = [
  'import { $ } from "bun"',
  'import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs"',
  'import { dirname } from "node:path"',
  "const [port, scenarioFile] = process.argv.slice(2)",
  "const factory = (await import(port)).opencodeMemoryGuard",
  "const hooks = new Map<string, any>()",
  "const logs: Record<string, unknown[]> = {}",
  "const systems: string[][] = []",
  "function client(kind: string, entries: unknown[]) {",
  "  if (kind === \"throwing\") return { app: { log: () => { throw new Error(\"log service down\") } } }",
  "  if (kind === \"rejecting\") return { app: { log: async () => { throw new Error(\"log service down\") } } }",
  "  const delay = kind === \"slow\" ? 500 : 0",
  "  return { app: { log: async (entry: unknown) => {",
  "    entries.push(entry)",
  "    if (delay) await new Promise((done) => setTimeout(done, delay))",
  "    return { data: true }",
  "  } } }",
  "}",
  "async function run(step: any): Promise<void> {",
  "  if (step.op === \"start\") {",
  "    logs[step.name] = []",
  "    const roots = { directory: step.directory, worktree: step.worktree }",
  "    const input = { client: client(step.client, logs[step.name]), ...roots, $ }",
  "    hooks.set(step.name, await factory(input))",
  "  } else if (step.op === \"created\") {",
  "    const event = { type: \"session.created\", properties: { info: { id: step.session } } }",
  "    await hooks.get(step.name).event({ event })",
  "  } else if (step.op === \"edited\") {",
  "    await hooks.get(step.name).event({ event: { type: \"file.edited\", properties: { file: step.file } } })",
  "  } else if (step.op === \"transform\") {",
  `    const output = { system: [${JSON.stringify(BASE)}] }`,
  "    systems.push(output.system)",
  "    await hooks.get(step.name)[\"experimental.chat.system.transform\"]({ sessionID: step.session }, output)",
  "  } else if (step.op === \"write\") {",
  "    mkdirSync(dirname(step.path), { recursive: true })",
  "    writeFileSync(step.path, step.content)",
  "  } else if (step.op === \"rm\") {",
  "    rmSync(step.path, { recursive: true, force: true })",
  "  } else if (step.op === \"concurrent\") {",
  "    await Promise.all(step.steps.map(run))",
  "  }",
  "}",
  "for (const step of JSON.parse(readFileSync(scenarioFile, \"utf8\"))) await run(step)",
  "console.log(JSON.stringify({ systems, logs }))",
  "",
].join("\n")

let scenarioCounter = 0

// Runs STEPS against the port at PORTPATH in a child bun with the scope env (plus OVERRIDES), from the project dir.
function runScenario(scope: Scope, portPath: string, steps: Step[], overrides: EnvOverrides = {}): Result {
  scenarioCounter += 1
  const probe = join(scope.root, "probe.ts")
  const scenario = join(scope.root, `scenario-${scenarioCounter}.json`)
  writeFileSync(probe, CHILD_PROBE)
  writeFileSync(scenario, JSON.stringify(steps))
  const result = spawnSync(process.execPath, [probe, portPath, scenario], {
    cwd: scope.projectDir, env: childEnv(scope, overrides), encoding: "utf8", timeout: CHILD_TIMEOUT_MS,
  })

  expect({ status: result.status, stderr: result.stderr }).toEqual({ status: 0, stderr: "" })
  const parsed = JSON.parse(result.stdout) as Result
  expectRuntimeInvariants(parsed)
  return parsed
}

// Each system line that runs python3 is prefixed with the runtime env, and nothing emitted names AskUserQuestion.
function expectRuntimeInvariants(result: Result): void {
  const lines = result.systems.flat().flatMap((text) => text.split("\n"))
  const unprefixed = lines.filter((line) => line.includes("python3") && !line.startsWith(RUNTIME_LINE))
  expect(unprefixed).toEqual([])
  expect(JSON.stringify(result)).not.toContain("AskUserQuestion")
}

// The first python3 command line in TEXT naming SCRIPT, without its leading spaces.
function commandLine(text: string, script: string): string {
  const line = text.split("\n").find((candidate) => candidate.startsWith(RUNTIME_LINE) && candidate.includes(script))
  if (!line) throw new Error(`no ${script} command in:\n${text}`)
  return line.trim()
}

// Runs one injected command line through sh with the scope env, as the model would.
function runCommand(scope: Scope, line: string): { status: number | null; stdout: string; stderr: string } {
  const result = spawnSync("sh", ["-c", line], { cwd: scope.projectDir, env: childEnv(scope), encoding: "utf8" })
  return { status: result.status, stdout: result.stdout, stderr: result.stderr }
}

// Design Doc F1 table, memory-guard row: marker scripts/apply_action.py; on missing, log once and the instruction
// states the payload is missing. Slice memory-guard: the `question` text and the MEMORY_GUARD_RUNTIME prefix.
describe("opencode-memory-guard: payload resolution on the installed layout", () => {
  // AC-005 (D4, [bun]), memory-guard bullet; the text check also covers AC-053's third bullet
  // AC-005 (part): "Installed into a temp scope: memory-guard's instruction shall name an existing
  //   .../llm-agent-workflow/memory-guard/scripts/apply_action.py prefixed with MEMORY_GUARD_RUNTIME=opencode"
  // AC-053 (part): "The opencode instruction text contains question and not AskUserQuestion."
  // Given: the installed layout above, and a project repo with an uncommitted change to AGENTS.md.
  // When: the port's event hook gets {type: "session.created"}, then "experimental.chat.system.transform" runs for
  //   that session.
  // Then: the injected instruction points at the installed payload script under the opencode runtime.
  // Verification items:
  //   - the pushed system text is the first-time ask naming "MEMORY_GUARD_RUNTIME=opencode python3 <path>/..."
  //   - <path> exists on disk and ends with "llm-agent-workflow/memory-guard/scripts"
  //   - the text names the `question` tool and does not contain "AskUserQuestion"
  //   - client.app.log has no payload-missing warning
  // Pass criteria: all four checks hold with the port imported through the symlink.
  // ROI: 80 (BV:8 x Freq:9 + Legal:0 + Defect:8)
  // @category: core-functionality
  // @dependency: opencode-memory-guard.ts, payload resolver, git (dirty-path detection)
  // @real-dependency: git, filesystem (symlinks)
  // @complexity: medium
  test("AC-005: the instruction names the installed apply_action.py under MEMORY_GUARD_RUNTIME=opencode", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const scripts = installGlobal(scope)
    dirty(scope, "AGENTS.md")
    expect(realpathSync(installed)).toBe(join(scope.root, "dotfiles", "plugins", "opencode-memory-guard.ts"))

    const result = runScenario(scope, installed, [start("a", projectRoots(scope)), ...sessionStart("a", "ses_1")])

    const text = firstTime({ kind: "start", paths: ["AGENTS.md"], session: "ses_1", repo: scope.projectDir, scripts })
    expect(result.systems).toEqual([[BASE, text]])
    expect(scripts.endsWith("/llm-agent-workflow/memory-guard/scripts")).toBe(true)
    expect(existsSync(join(scripts, "apply_action.py"))).toBe(true)
    expect(text).toContain("`question` tool call is mandatory")
    expect(text).not.toContain("AskUserQuestion")
    expect(result.logs).toEqual({ a: [startLog("ses_1")] })
  })

  // IP-20: the TS and the Python agree on the doc name, the state dir and the preference file.
  test("the injected commands stash AGENTS.md, resolve the pending entry and set the preference the port reads", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const scripts = installGlobal(scope)
    dirty(scope, "AGENTS.md")

    const first = runScenario(scope, installed, [start("a", projectRoots(scope)), ...sessionStart("a", "ses_1")])
    const pending = JSON.parse(readFileSync(sessionFile(scope, "ses_1"), "utf8"))
    const text = first.systems[0][1]
    const setPreference = runCommand(scope, commandLine(text, "set_preference.py").replace("<remove|stash>", "stash"))
    const apply = runCommand(scope, commandLine(text, "apply_action.py").replace("<remove|stash>", "stash"))
    dirty(scope, ".claude/settings.json")
    const second = runScenario(scope, installed, [start("b", projectRoots(scope)), ...sessionStart("b", "ses_2")])

    expect(pending.paths["AGENTS.md"]).toMatchObject({ status: "pending", action: null })
    expect(setPreference).toEqual({
      status: 0, stdout: `[memory-guard] project preference set to 'stash' for ${scope.projectDir}\n`, stderr: "",
    })
    expect(apply).toEqual({ status: 0, stdout: "[memory-guard] stashed 1 path(s):\n  - AGENTS.md\n", stderr: "" })
    expect(git(scope, "stash", "show", "--name-only", "stash@{0}")).toBe("AGENTS.md\n")
    expect(JSON.parse(readFileSync(sessionFile(scope, "ses_1"), "utf8")).paths["AGENTS.md"])
      .toMatchObject({ status: "resolved", action: "stash" })
    expect(JSON.parse(readFileSync(prefFile(scope), "utf8"))).toMatchObject({ action: "stash" })
    expect(existsSync(join(scope.home, ".claude"))).toBe(false)
    const auto = { kind: "start" as const, paths: [".claude/settings.json"], session: "ses_2", repo: scope.projectDir }
    expect(second.systems).toEqual([[BASE, autoApply({ ...auto, scripts }, "stash")]])
  })

  // A real global install keeps XDG_CONFIG_HOME unset: the global candidate then comes from os.homedir().
  test("without XDG_CONFIG_HOME, the child finds the global payload under its HOME's .config", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const scripts = placePayload(join(scope.home, ".config", "opencode", PAYLOAD))
    dirty(scope, "AGENTS.md")

    const steps = [start("a", projectRoots(scope)), ...sessionStart("a", "ses_1")]
    const result = runScenario(scope, installed, steps, { XDG_CONFIG_HOME: undefined })

    const text = firstTime({ kind: "start", paths: ["AGENTS.md"], session: "ses_1", repo: scope.projectDir, scripts })
    expect(result).toEqual({ systems: [[BASE, text]], logs: { a: [startLog("ses_1")] } })
  })
})

// Every builder the port has: the first-time ask and the preference-set text, each for SessionStart and file.edited.
describe("opencode-memory-guard: every instruction builder", () => {
  // Slice memory-guard: "injected commands prefixed with MEMORY_GUARD_RUNTIME=opencode"; line 251 -> `question`.
  test("each builder names every script under MEMORY_GUARD_RUNTIME=opencode and never AskUserQuestion", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const scripts = installGlobal(scope)
    dirty(scope, "AGENTS.md")
    const context = { repo: scope.projectDir, scripts }

    const result = runScenario(scope, installed, [
      start("a", projectRoots(scope)),
      ...sessionStart("a", "ses_1"),
      write(repoFile(scope, ".claude/notes.md"), "dirty\n"),
      edited("a", repoFile(scope, ".claude/notes.md")),
      transform("a", "ses_1"),
      write(prefFile(scope), JSON.stringify({ action: "remove" })),
      ...sessionStart("a", "ses_2"),
      write(repoFile(scope, "docs/ticket-tracking/T-1.md"), "dirty\n"),
      edited("a", repoFile(scope, "docs/ticket-tracking/T-1.md")),
      transform("a", "ses_2"),
    ])

    const startPaths = ["AGENTS.md", ".claude/notes.md"]
    const editPaths = ["docs/ticket-tracking/T-1.md"]
    expect(result.systems).toEqual([
      [BASE, firstTime({ ...context, kind: "start", paths: ["AGENTS.md"], session: "ses_1" })],
      [BASE, firstTime({ ...context, kind: "edit", paths: [".claude/notes.md"], session: "ses_1" })],
      [BASE, autoApply({ ...context, kind: "start", paths: startPaths, session: "ses_2" }, "remove")],
      [BASE, autoApply({ ...context, kind: "edit", paths: editPaths, session: "ses_2" }, "remove")],
    ])
    expect(result.systems.flat().join("\n")).not.toContain("AskUserQuestion")
    expect(result.logs).toEqual({
      a: [
        startLog("ses_1"), flagLog(".claude/notes.md", "ses_1"), editLog([".claude/notes.md"], "ses_1"),
        startLog("ses_2"), flagLog("docs/ticket-tracking/T-1.md", "ses_2"),
        editLog(["docs/ticket-tracking/T-1.md"], "ses_2"),
      ],
    })
  })

  test("each builder puts the payload-missing line in place of the commands when there is no payload", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    dirty(scope, "AGENTS.md")
    const context = { repo: scope.projectDir, scripts: null }

    const result = runScenario(scope, installed, [
      start("a", projectRoots(scope)),
      ...sessionStart("a", "ses_1"),
      write(repoFile(scope, ".claude/notes.md"), "dirty\n"),
      edited("a", repoFile(scope, ".claude/notes.md")),
      transform("a", "ses_1"),
      write(prefFile(scope), JSON.stringify({ action: "stash" })),
      ...sessionStart("a", "ses_2"),
      write(repoFile(scope, "docs/ticket-tracking/T-1.md"), "dirty\n"),
      edited("a", repoFile(scope, "docs/ticket-tracking/T-1.md")),
      transform("a", "ses_2"),
    ])

    const startPaths = ["AGENTS.md", ".claude/notes.md"]
    const editPaths = ["docs/ticket-tracking/T-1.md"]
    expect(result.systems).toEqual([
      [BASE, firstTime({ ...context, kind: "start", paths: ["AGENTS.md"], session: "ses_1" })],
      [BASE, firstTime({ ...context, kind: "edit", paths: [".claude/notes.md"], session: "ses_1" })],
      [BASE, autoApply({ ...context, kind: "start", paths: startPaths, session: "ses_2" }, "stash")],
      [BASE, autoApply({ ...context, kind: "edit", paths: editPaths, session: "ses_2" }, "stash")],
    ])
    expect(result.systems.flat().join("\n")).not.toContain("python3")
    expect(result.logs.a[0]).toEqual(warn(missingMessage(noPayloadSearched(scope))))
  })
})

// Design Doc F1: the env root (exclusive), then the project (directory, then worktree), then global, then dev.
describe("opencode-memory-guard: payload candidates (F1)", () => {
  test("the port in the repo names plugin-memory-guard/scripts through the dev-layout candidate", () => {
    const scope = newScope()
    dirty(scope, "AGENTS.md")
    const scripts = join(REPO_ROOT, "plugin-memory-guard", "scripts")

    const result = runScenario(scope, join(REPO_ROOT, PORT), [
      start("a", projectRoots(scope)), ...sessionStart("a", "ses_1"),
    ])

    const text = firstTime({ kind: "start", paths: ["AGENTS.md"], session: "ses_1", repo: scope.projectDir, scripts })
    expect(result).toEqual({ systems: [[BASE, text]], logs: { a: [startLog("ses_1")] } })
  })

  test("PluginInput.directory wins over worktree, and worktree over the global payload", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const worktree = join(scope.root, "opencode-worktree")
    const fromDirectory = placePayload(projectPayload(scope.projectDir))
    const fromWorktree = placePayload(projectPayload(worktree))
    const fromGlobal = installGlobal(scope)
    dirty(scope, "AGENTS.md")
    const roots = { directory: scope.projectDir, worktree }

    const result = runScenario(scope, installed, [
      start("a", roots), ...sessionStart("a", "ses_1"),
      rm(projectPayload(scope.projectDir)),
      start("b", roots), ...sessionStart("b", "ses_2"),
      rm(projectPayload(worktree)),
      start("c", roots), ...sessionStart("c", "ses_3"),
    ])

    const expected = (session: string, scripts: string) =>
      [BASE, firstTime({ kind: "start", paths: ["AGENTS.md"], session, repo: scope.projectDir, scripts })]
    expect(result.systems).toEqual([
      expected("ses_1", fromDirectory), expected("ses_2", fromWorktree), expected("ses_3", fromGlobal),
    ])
    expect(result.logs).toEqual({ a: [startLog("ses_1")], b: [startLog("ses_2")], c: [startLog("ses_3")] })
  })

  test("the env root is exclusive: its payload is used, and without one nothing else is searched", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    placePayload(projectPayload(scope.projectDir))
    installGlobal(scope)
    const withPayload = join(scope.root, "env-root")
    const envScripts = placePayload(join(withPayload, PLUGIN_ID))
    const empty = join(scope.root, "empty-env-root")
    mkdirSync(empty)
    dirty(scope, "AGENTS.md")
    // One session per child: a session that already flagged AGENTS.md stays silent about it (the debounce).
    const steps = (session: string) => [start("a", projectRoots(scope)), ...sessionStart("a", session)]
    const expected = (session: string, scripts: string | null) =>
      firstTime({ kind: "start", paths: ["AGENTS.md"], session, repo: scope.projectDir, scripts })

    const used = runScenario(scope, installed, steps("ses_1"), { LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: withPayload })
    const none = runScenario(scope, installed, steps("ses_2"), { LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: empty })

    expect(used).toEqual({ systems: [[BASE, expected("ses_1", envScripts)]], logs: { a: [startLog("ses_1")] } })
    expect(none).toEqual({
      systems: [[BASE, expected("ses_2", null)]],
      logs: { a: [warn(missingMessage([join(empty, PLUGIN_ID)])), startLog("ses_2")] },
    })
  })

  test.each([
    ["directory equals worktree", projectRoots, {}],
    ["worktree is empty", (scope: Scope) => ({ directory: scope.projectDir, worktree: "" }), {}],
    ["the env override is empty", projectRoots, { LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: "" }],
  ])("one project root is searched once when %s", (_name, roots: (scope: Scope) => Roots, overrides: EnvOverrides) => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    dirty(scope, "AGENTS.md")

    const result = runScenario(scope, installed, [start("a", roots(scope)), ...sessionStart("a", "ses_1")], overrides)

    expect(result.logs).toEqual({ a: [warn(missingMessage(noPayloadSearched(scope))), startLog("ses_1")] })
  })

  test("a resolver failure leaves the factory working, and the instruction says the payload is missing", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installGlobal(scope)
    dirty(scope, "AGENTS.md")

    const roots = { directory: scope.projectDir, worktree: 42 }
    const result = runScenario(scope, installed, [start("a", roots), ...sessionStart("a", "ses_1")])

    const text =
      firstTime({ kind: "start", paths: ["AGENTS.md"], session: "ses_1", repo: scope.projectDir, scripts: null })
    expect(resolverFailure()).toStartWith("TypeError [ERR_INVALID_ARG_TYPE]: ")
    expect(result).toEqual({
      systems: [[BASE, text]], logs: { a: [warn(failedMessage(resolverFailure())), startLog("ses_1")] },
    })
  })
})

// Recovery: the marker is checked, and watched-paths.json read, for every instruction or edit.
describe("opencode-memory-guard: payload files changed after start", () => {
  test("a removed apply_action.py gives the missing line once, and a re-created one names the scripts again", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const scripts = installGlobal(scope)
    const marker = join(scripts, "apply_action.py")
    dirty(scope, "AGENTS.md")
    const expected = (session: string, names: string | null) =>
      [BASE, firstTime({ kind: "start", paths: ["AGENTS.md"], session, repo: scope.projectDir, scripts: names })]

    const result = runScenario(scope, installed, [
      start("a", projectRoots(scope)),
      rm(marker), ...sessionStart("a", "ses_1"),
      ...sessionStart("a", "ses_2"),
      write(marker, "# re-created\n"), ...sessionStart("a", "ses_3"),
      rm(marker), ...sessionStart("a", "ses_4"),
    ])

    expect(result.systems).toEqual([
      expected("ses_1", null), expected("ses_2", null), expected("ses_3", scripts), expected("ses_4", null),
    ])
    expect(result.logs).toEqual({
      a: [warn(removedMessage(marker)), startLog("ses_1"), startLog("ses_2"), startLog("ses_3"), startLog("ses_4")],
    })
  })

  test("watched-paths.json is read on every edit, so an edited, invalid, removed or re-created file applies", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const scripts = installGlobal(scope)
    const config = join(globalPayload(scope), "config", "watched-paths.json")
    const edit = (file: string): Step[] => [write(repoFile(scope, file), "dirty\n"), edited("a", repoFile(scope, file))]
    const expected = (paths: string[]) =>
      [BASE, firstTime({ kind: "edit", paths, session: "ses_1", repo: scope.projectDir, scripts })]

    const result = runScenario(scope, installed, [
      write(config, JSON.stringify({ watched_dirs: [], watched_files: ["NOTES.md"] })),
      start("a", projectRoots(scope)), ...sessionStart("a", "ses_1"),
      ...edit("AGENTS.md"), ...edit("NOTES.md"), transform("a", "ses_1"),
      write(config, "["), edited("a", repoFile(scope, "AGENTS.md")), transform("a", "ses_1"),
      rm(config), ...edit(".claude/a.md"), transform("a", "ses_1"),
      write(config, JSON.stringify({ watched_dirs: ["notes"], watched_files: ["CLAUDE.md"] })),
      ...edit("docs/ticket-tracking/T-1.md"), ...edit("notes/n.md"), transform("a", "ses_1"),
    ])

    expect(result.systems).toEqual([
      [BASE], expected(["NOTES.md"]), expected(["AGENTS.md"]), expected([".claude/a.md"]), expected(["notes/n.md"]),
    ])
    expect(result.logs.a.filter((entry) => JSON.stringify(entry).includes('"warn"'))).toEqual([])
  })
})

// A resolver failure, no payload and a marker removed after start are one warning kind: whichever comes first is the
// only one logged in a process, however many instances or instructions follow.
describe("opencode-memory-guard: the payload warning is logged once per process", () => {
  type Reason = "failure" | "missing" | "removed"

  // Starts instance NAME for REASON, runs one session start, and returns its steps.
  function reasonSteps(scope: Scope, reason: Reason, name: string, session: string): Step[] {
    const marker = join(globalPayload(scope), "scripts", "apply_action.py")
    const roots = reason === "failure" ? { directory: scope.projectDir, worktree: 42 } : projectRoots(scope)
    const setup: Step[] = reason === "removed" ? [write(marker, "# installed\n")] : [rm(globalPayload(scope))]
    const after: Step[] = reason === "removed" ? [rm(marker)] : []
    return [...setup, start(name, roots), ...after, ...sessionStart(name, session)]
  }

  function reasonMessage(scope: Scope, reason: Reason): string {
    if (reason === "failure") return failedMessage(resolverFailure())
    if (reason === "missing") return missingMessage(noPayloadSearched(scope))
    return removedMessage(join(globalPayload(scope), "scripts", "apply_action.py"))
  }

  test.each([
    [["failure", "missing", "removed"]],
    [["missing", "removed", "failure"]],
    [["removed", "failure", "missing"]],
  ] as Reason[][][])("only the first of %p is logged, and later reasons and instances log nothing more", (reasons) => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    dirty(scope, "AGENTS.md")
    const names = ["a", "b", "c"]

    const result = runScenario(scope, installed,
      reasons.flatMap((reason, index) => reasonSteps(scope, reason, names[index], `ses_${index + 1}`)))

    const text = (session: string) =>
      [BASE, firstTime({ kind: "start", paths: ["AGENTS.md"], session, repo: scope.projectDir, scripts: null })]
    expect(result.systems).toEqual([text("ses_1"), text("ses_2"), text("ses_3")])
    expect(result.logs).toEqual({
      a: [warn(reasonMessage(scope, reasons[0])), startLog("ses_1")], b: [startLog("ses_2")], c: [startLog("ses_3")],
    })
  })

  test("a second instruction in the same instance logs nothing more", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    dirty(scope, "AGENTS.md")

    const result = runScenario(scope, installed, [
      start("a", projectRoots(scope)), ...sessionStart("a", "ses_1"),
      write(repoFile(scope, ".claude/notes.md"), "dirty\n"), edited("a", repoFile(scope, ".claude/notes.md")),
      transform("a", "ses_1"),
    ])

    expect(result.logs).toEqual({
      a: [
        warn(missingMessage(noPayloadSearched(scope))), startLog("ses_1"), flagLog(".claude/notes.md", "ses_1"),
        editLog([".claude/notes.md"], "ses_1"),
      ],
    })
  })

  // The slow client holds each log call for 500 ms, so a warning marked only after its log resolved would be logged
  // by both instances.
  test("concurrent instructions from two instances log the payload warning once", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    dirty(scope, "AGENTS.md")

    const result = runScenario(scope, installed, [
      start("a", projectRoots(scope), "slow"), start("b", projectRoots(scope), "slow"),
      created("a", "ses_1"), created("b", "ses_2"),
      { op: "concurrent", steps: [transform("a", "ses_1"), transform("b", "ses_2")] },
    ])

    const text = (session: string) =>
      [BASE, firstTime({ kind: "start", paths: ["AGENTS.md"], session, repo: scope.projectDir, scripts: null })]
    expect(result.systems).toEqual([text("ses_1"), text("ses_2")])
    const warnings = [...result.logs.a, ...result.logs.b].filter((entry) => JSON.stringify(entry).includes('"warn"'))
    expect(warnings).toEqual([warn(missingMessage(noPayloadSearched(scope)))])
  })

  test("a failing client.app.log never breaks the instructions", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    dirty(scope, "AGENTS.md")

    const result = runScenario(scope, installed, [
      start("rejecting", projectRoots(scope), "rejecting"), ...sessionStart("rejecting", "ses_1"),
      start("throwing", projectRoots(scope), "throwing"), ...sessionStart("throwing", "ses_2"),
    ])

    const text = (session: string) =>
      [BASE, firstTime({ kind: "start", paths: ["AGENTS.md"], session, repo: scope.projectDir, scripts: null })]
    expect(result).toEqual({ systems: [text("ses_1"), text("ses_2")], logs: { rejecting: [], throwing: [] } })
  })
})

// Design Doc k, IP-20: the doc name is replaced, not added. Under the prefix the Python watches AGENTS.md only
// (test_runtime_env.py), so the port must not flag CLAUDE.md either, with the payload's config or the defaults.
describe("opencode-memory-guard: CLAUDE.md is not watched on opencode", () => {
  test.each([
    ["the payload config", true],
    ["the built-in defaults (no payload)", false],
  ])("a dirty or edited CLAUDE.md is never flagged, with %s", (_name, withPayload: boolean) => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const scripts = withPayload ? installGlobal(scope) : null
    dirty(scope, "AGENTS.md")
    const claudeDoc = repoFile(scope, "CLAUDE.md")

    const result = runScenario(scope, installed, [
      start("a", projectRoots(scope)), ...sessionStart("a", "ses_1"),
      write(claudeDoc, "dirty\n"), edited("a", claudeDoc), transform("a", "ses_1"),
      ...sessionStart("a", "ses_2"),
    ])

    const expected = (session: string) =>
      [BASE, firstTime({ kind: "start", paths: ["AGENTS.md"], session, repo: scope.projectDir, scripts })]
    expect(result.systems).toEqual([expected("ses_1"), [BASE], expected("ses_2")])
    const warnings = withPayload ? [] : [warn(missingMessage(noPayloadSearched(scope)))]
    expect(result.logs).toEqual({ a: [...warnings, startLog("ses_1"), startLog("ses_2")] })
  })
})

describe("opencode-memory-guard: manifests", () => {
  // Design Doc "Version Bumps", memory-guard row: plugin.json and package.json 1.0.0 -> 1.1.0; AC-045 (changelog)
  test("plugin.json and package.json are at 1.1.0, and the README logs 1.1.0 under its changelog", () => {
    const read = (path: string) => readFileSync(join(REPO_ROOT, "plugin-memory-guard", path), "utf8")

    expect(JSON.parse(read(".claude-plugin/plugin.json"))).toMatchObject({ name: "memory-guard", version: "1.1.0" })
    expect(JSON.parse(read("package.json"))).toMatchObject({ name: "opencode-memory-guard", version: "1.1.0" })
    expect(read("README.md")).toMatch(/^## Changelog\n\n### 1\.1\.0\n/m)
  })

  // AC-021 (memory-guard part): the skill description fits opencode's 250-byte limit.
  test("the skill description is at most 250 UTF-8 bytes and still names the watched paths", () => {
    const skill = readFileSync(join(REPO_ROOT, "plugin-memory-guard", "skills", "memory-guard", "SKILL.md"), "utf8")
    const description = skill.match(/^description: (.*)$/m)?.[1] ?? ""

    expect(Buffer.byteLength(description, "utf8")).toBeLessThanOrEqual(250)
    for (const name of [".claude/**", "CLAUDE.md", "docs/ticket-tracking/**", "apply_action.py"]) {
      expect(description).toContain(name)
    }
  })
})
