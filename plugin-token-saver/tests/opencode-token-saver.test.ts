// token-saver opencode port Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.5.3)
// Generated: 2026-09-24 | Budget Used: 1/3 integration (token-saver slice), 0/2 E2E
//
// Run with (HOME and the bun caches pointed outside the real home):
//
//   bun test plugin-token-saver/tests
//
// Harness (Design Doc "Test Strategy > TS"; scripts/opencode/tests/support/scope.ts):
// - Each test builds a temp scope under os.tmpdir(): HOME=<tmp>/home, XDG_CONFIG_HOME=<tmp>/config, and
//   <tmp>/config/opencode/plugins as a symlink to <tmp>/dotfiles/plugins (the live layout, N20).
// - The port is copied to <tmp>/dotfiles/plugins/opencode-token-saver.ts and dynamic-imported through the symlinked
//   path with a unique query string, so Bun reports import.meta.url at the realpath (N19), as opencode does. A fresh
//   import is a fresh module, so "once per process" is "once per imported module" here.
// - A payload is <candidate>/config/vague-patterns.json. Most tests write one that lists only sentinel patterns,
//   which are not among the port's built-in defaults, so a match proves the list came from that payload.
// - The factory gets a fake client (client.app.log records calls) and directory/worktree set to a temp project. The
//   hooks run with that project as the cwd. LLM_AGENT_WORKFLOW_PAYLOAD_ROOT is cleared unless a test sets it.
// - Only "chat.message" and "experimental.chat.system.transform" are driven. The port spawns nothing, and
//   "tool.execute.after", which writes under the os.homedir() Bun cached at startup, is never called.

import { afterEach, describe, expect, test } from "bun:test"
import { spawnSync } from "node:child_process"
import { mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import {
  copyPort, type EnvVars, type FakeClient, fakeClient, importPort, makeScope, REPO_ROOT, type Scope, scopeEnv,
  withEnv,
} from "../../scripts/opencode/tests/support/scope"

const PORT = "plugin-token-saver/plugins/opencode-token-saver.ts"
const PLUGIN_ID = "token-saver"
const PAYLOAD = join("llm-agent-workflow", PLUGIN_ID)
const PATTERNS_FILE = join("config", "vague-patterns.json")

// Not a built-in default, at least 15 characters and free of ./_:() so the short-prompt rule never flags it: only a
// pattern list that holds it makes it vague.
const SENTINEL = "sentinel payload-only pattern"
// A built-in default of 16 characters: only the exact-match rule on the default list flags it.
const DEFAULT_ONLY = "handle the error"
// Flagged only through the default whitelisted prefix "refactor " (a short remainder); 19 characters.
const WHITELIST_ONLY = "refactor the widget"
const SPECIFIC = "Fix the null pointer in src/auth/login.ts line 45"
const PROMPTS = [SENTINEL, DEFAULT_ONLY, WHITELIST_ONLY, SPECIFIC]

const DEFAULTS_HINT = "Using the built-in patterns. Reinstall with ./setup-opencode.sh --global --plugin token-saver"
const SHAPE_ERROR = "TypeError: patterns and whitelisted_prefixes must be arrays of strings"

const EM_DASH = "\u2014"
const GUIDANCE =
  `[token-saver] Vague prompt detected (OpenCode has no blocking UserPromptSubmit equivalent ${EM_DASH} this is ` +
  "guidance, not a block). Ask for specifics if needed:\n\n" +
  `[token-saver] BLOCKED: This prompt is too vague ${EM_DASH} it will cost extra tokens\n` +
  "for Claude to figure out what you mean.\n\n" +
  "Please provide specifics:\n" +
  "  - File paths (e.g. src/auth/login.ts)\n" +
  "  - Line numbers (e.g. line 45)\n" +
  "  - Function names (e.g. handleSubmit)\n" +
  `  - Error messages (e.g. "TypeError: Cannot read property 'map'")\n\n` +
  'Bad:  "fix the bug"\n' +
  `Good: "Fix the null pointer in src/auth/login.ts line 45 ${EM_DASH} ` +
  'handleSubmit called before useState resolves"\n\n' +
  'Bad:  "make it better"\n' +
  `Good: "Optimize the N+1 query in app/models/user.rb ${EM_DASH} User#orders loads each order individually"\n\n` +
  'Bad:  "handle the error"\n' +
  "Good: \"Add error handling for the failed fetch in components/Dashboard.tsx " +
  `${EM_DASH} catch the network error and show a retry button"`

// Every transform starts from this system prompt, so "untouched" means exactly [BASE].
const BASE = "base system prompt"
const WITH_GUIDANCE = [BASE, GUIDANCE]
const UNTOUCHED = [BASE]
// What guidanceFor(saver, PROMPTS) returns with a payload that lists only SENTINEL, and with the built-in defaults.
const FROM_PAYLOAD = [WITH_GUIDANCE, UNTOUCHED, UNTOUCHED, UNTOUCHED]
const FROM_DEFAULTS = [UNTOUCHED, WITH_GUIDANCE, WITH_GUIDANCE, UNTOUCHED]

type Hook = (input: unknown, output: unknown) => Promise<void>

interface Saver {
  // One chat.message with a single text part.
  message(sessionID: string, messageID: string, text: string): Promise<void>
  // One experimental.chat.system.transform for SESSIONID, starting from [BASE]; returns the system array after it.
  transform(sessionID: string): Promise<string[]>
  logs: unknown[]
}

interface Roots {
  directory: unknown
  worktree: unknown
}

const scopes: Scope[] = []

afterEach(() => {
  for (const scope of scopes.splice(0)) scope.cleanup()
})

function newScope(): Scope {
  const scope = makeScope({ kind: "global", symlinkPlugins: true })
  scopes.push(scope)
  return scope
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

// Writes <payload>/config/vague-patterns.json (a string as is, anything else as JSON) and returns its path.
function writePatterns(payload: string, content: unknown): string {
  const file = join(payload, PATTERNS_FILE)
  mkdirSync(dirname(file), { recursive: true })
  writeFileSync(file, typeof content === "string" ? content : JSON.stringify(content))
  return file
}

// A valid payload listing only PATTERNS, with no whitelisted prefixes.
function listing(...patterns: string[]): unknown {
  return { patterns, whitelisted_prefixes: [] }
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

function warn(message: string): unknown {
  return { body: { service: "token-saver", level: "warn", message } }
}

function vagueLog(sessionID: string, messageID: string): unknown {
  const extra = { sessionID, messageID }
  return { body: { service: "token-saver", level: "warn", message: "vague prompt detected", extra } }
}

function guidanceLog(sessionID: string): unknown {
  const extra = { sessionID, count: 1 }
  return { body: { service: "token-saver", level: "warn", message: "injected vague-prompt guidance", extra } }
}

// The logs guidanceFor(saver, PROMPTS) adds for each flagged prompt, with a SENTINEL-only payload and with defaults.
const PAYLOAD_LOGS = [vagueLog("ses_1", "msg_1"), guidanceLog("ses_1")]
const DEFAULT_LOGS = [
  vagueLog("ses_2", "msg_2"), guidanceLog("ses_2"), vagueLog("ses_3", "msg_3"), guidanceLog("ses_3"),
]

function missingMessage(searched: string[]): string {
  return `[token-saver] payload not found. Searched: ${searched.join(", ")}. ${DEFAULTS_HINT}`
}

// The payload warning for projectRoots(scope) with no payload anywhere.
function noPayloadMessage(scope: Scope): string {
  return missingMessage([projectPayload(scope.projectDir), globalPayload(scope), join(scope.root, "dotfiles")])
}

function failedMessage(reason: string): string {
  return `[token-saver] payload resolution failed (${reason}). ${DEFAULTS_HINT}`
}

// What the port's first project candidate throws for directory: 42 (the same join), in the runtime's own words.
function resolverFailure(): string {
  return thrownText(() => join(42 as unknown as string, ".opencode", PAYLOAD))
}

function invalidMessage(file: string, reason: string): string {
  return `[token-saver] invalid patterns file ${file} (${reason}). ${DEFAULTS_HINT}`
}

// Runs fn with the scope env and the temp project as the cwd, and restores both afterwards.
async function inScope<T>(scope: Scope, overrides: EnvVars, fn: () => Promise<T>): Promise<T> {
  const previous = process.cwd()
  process.chdir(scope.projectDir)
  try {
    return await withEnv(scopeEnv(scope, overrides), fn)
  } finally {
    process.chdir(previous)
  }
}

async function startSaver(module: Record<string, unknown>, roots: Roots, custom?: FakeClient): Promise<Saver> {
  const recorder = fakeClient()
  const factory = module.TokenSaverPlugin as (input: Record<string, unknown>) => Promise<Record<string, Hook>>
  const client = custom ?? recorder.client
  const hooks = await factory({ client, directory: roots.directory, worktree: roots.worktree })
  const message = (sessionID: string, messageID: string, text: string) =>
    hooks["chat.message"]({ sessionID, messageID }, { message: {}, parts: [{ type: "text", text }] })
  const transform = async (sessionID: string) => {
    const output = { system: [BASE] }
    await hooks["experimental.chat.system.transform"]({ sessionID }, output)
    return output.system
  }
  return { message, transform, logs: recorder.logs }
}

// Sends prompt n (from 1) as the one message msg_<n> of session ses_<n>, then runs that session's transform; returns
// each transform's system array.
async function guidanceFor(saver: Saver, prompts: string[]): Promise<string[][]> {
  const systems: string[][] = []
  for (const [index, prompt] of prompts.entries()) {
    await saver.message(`ses_${index + 1}`, `msg_${index + 1}`, prompt)
    systems.push(await saver.transform(`ses_${index + 1}`))
  }
  return systems
}

// Imports the port given as argv[2], starts it on directory = worktree = argv[3], runs guidanceFor over the JSON
// prompt list in argv[4], and prints { systems, logs } as JSON.
const CHILD_PROBE = [
  "const [port, directory, prompts] = process.argv.slice(2)",
  "const logs: unknown[] = []",
  "const client = { app: { log: async (entry: unknown) => { logs.push(entry); return { data: true } } } }",
  "const hooks = await (await import(port)).TokenSaverPlugin({ client, directory, worktree: directory })",
  "const systems: string[][] = []",
  "for (const [index, text] of (JSON.parse(prompts) as string[]).entries()) {",
  "  const ids = { sessionID: `ses_${index + 1}`, messageID: `msg_${index + 1}` }",
  '  await hooks["chat.message"](ids, { message: {}, parts: [{ type: "text", text }] })',
  `  const output = { system: [${JSON.stringify(BASE)}] }`,
  '  await hooks["experimental.chat.system.transform"]({ sessionID: ids.sessionID }, output)',
  "  systems.push(output.system)",
  "}",
  "console.log(JSON.stringify({ systems, logs }))",
  "",
].join("\n")

// Design Doc F1 table, token-saver row: marker config/vague-patterns.json; on missing, log once, built-in defaults.
describe("opencode-token-saver: payload resolution on the installed layout", () => {
  // AC-005 (D4, [bun]), token-saver bullet
  // AC-005 (part): "Installed into a temp scope: ... token-saver shall match a payload-only pattern"
  // Given: the installed layout above, with a payload at <XDG_CONFIG_HOME>/opencode/llm-agent-workflow/token-saver
  //   whose vague-patterns.json lists only SENTINEL and no whitelisted prefixes.
  // When: "chat.message" gets a user message that matches only the sentinel pattern, then
  //   "experimental.chat.system.transform" runs for that session; later sessions repeat this with a built-in default,
  //   a prompt only the default whitelist flags, and a specific prompt.
  // Then: the pattern list came from the installed payload, not the built-in defaults.
  // Verification items:
  //   - the first session's transform pushes the vague-prompt guidance
  //   - every later session's transform leaves the system array untouched (the payload replaced the defaults)
  //   - client.app.log has no payload-missing warning
  // Pass criteria: guidance appears only for the sentinel message.
  // ROI: 55 (BV:6 x Freq:8 + Legal:0 + Defect:7)
  // @category: core-functionality
  // @dependency: opencode-token-saver.ts, payload resolver
  // @real-dependency: filesystem (symlinks)
  // @complexity: low
  test("AC-005: matches a pattern that exists only in the installed vague-patterns.json payload", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writePatterns(globalPayload(scope), listing(SENTINEL))
    expect(realpathSync(installed)).toBe(join(scope.root, "dotfiles", "plugins", "opencode-token-saver.ts"))

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))

      await expect(saver.message("ses_s", "msg_s", SENTINEL)).resolves.toBeUndefined()
      expect(await saver.transform("ses_s")).toEqual(WITH_GUIDANCE)
      await expect(saver.message("ses_d", "msg_d", DEFAULT_ONLY)).resolves.toBeUndefined()
      expect(await saver.transform("ses_d")).toEqual(UNTOUCHED)
      expect(await guidanceFor(saver, PROMPTS)).toEqual(FROM_PAYLOAD)

      expect(saver.logs).toEqual([vagueLog("ses_s", "msg_s"), guidanceLog("ses_s"), ...PAYLOAD_LOGS])
    })
  })

  // F1 table, token-saver row: "On missing: log once; built-in defaults"; Logging: once per process, at warn
  // Given: the same symlinked layout with no payload anywhere (the port sits outside the repo, so the dev-layout
  //   candidate has no patterns file either), and a worktree that differs from directory.
  // When: the prompts on one instance, then a built-in default on a second instance of the same module.
  // Then: the sentinel gets no guidance, the built-in defaults still apply, and one warning is logged per process.
  // Verification items:
  //   - no call throws, and nothing is logged before the first prompt
  //   - the warning lists every searched candidate and the reinstall command, and is the only warning
  //   - the second instance logs no warning
  test("without a payload, the built-in defaults apply and one warning is logged per process", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const worktree = join(scope.root, "worktree")
    mkdirSync(worktree)
    const missing = missingMessage([
      projectPayload(scope.projectDir), projectPayload(worktree), globalPayload(scope), join(scope.root, "dotfiles"),
    ])

    await inScope(scope, {}, async () => {
      const module = await importPort(installed)
      const saver = await startSaver(module, { directory: scope.projectDir, worktree })
      expect(saver.logs).toEqual([])

      await expect(saver.message("ses_s", "msg_s", SENTINEL)).resolves.toBeUndefined()
      expect(await saver.transform("ses_s")).toEqual(UNTOUCHED)
      expect(await guidanceFor(saver, PROMPTS)).toEqual(FROM_DEFAULTS)
      const again = await startSaver(module, { directory: scope.projectDir, worktree })
      await again.message("ses_2", "msg_2", DEFAULT_ONLY)
      expect(await again.transform("ses_2")).toEqual(WITH_GUIDANCE)

      expect(saver.logs).toEqual([warn(missing), ...DEFAULT_LOGS])
      expect(again.logs).toEqual([vagueLog("ses_2", "msg_2"), guidanceLog("ses_2")])
    })
  })

  // A real global install keeps XDG_CONFIG_HOME unset: the global candidate then comes from os.homedir(), which Bun
  // caches at startup, so only a child bun started with the scope's HOME can show it.
  test("without XDG_CONFIG_HOME, a child bun finds the global payload under its HOME's .config", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writePatterns(join(scope.home, ".config", "opencode", PAYLOAD), listing(SENTINEL))
    const probe = join(scope.root, "probe.ts")
    writeFileSync(probe, CHILD_PROBE)
    const env = {
      PATH: process.env.PATH ?? "",
      HOME: scope.home,
      XDG_CACHE_HOME: join(scope.root, "cache"),
      XDG_DATA_HOME: join(scope.root, "data"),
      BUN_INSTALL_CACHE_DIR: join(scope.root, "cache", "bun"),
      TMPDIR: scope.root,
    }

    // Half the test's 60 s timeout, so a hung child fails the status/stderr check below, not as a bare timeout.
    const result = spawnSync(process.execPath, [probe, installed, scope.projectDir, JSON.stringify(PROMPTS)], {
      cwd: scope.projectDir, env, encoding: "utf8", timeout: 30_000,
    })

    expect({ status: result.status, stderr: result.stderr }).toEqual({ status: 0, stderr: "" })
    expect(JSON.parse(result.stdout)).toEqual({ systems: FROM_PAYLOAD, logs: PAYLOAD_LOGS })
  }, 60_000)
})

// Design Doc F1: the env root (exclusive), then the project (directory, then worktree), then global, then dev.
describe("opencode-token-saver: payload candidates (F1)", () => {
  test("the port in the repo finds plugin-token-saver/config through the dev-layout candidate", async () => {
    const scope = newScope()

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(join(REPO_ROOT, PORT)), projectRoots(scope))

      // The shipped file holds the same lists as the built-in defaults; only the missing warning tells them apart.
      expect(await guidanceFor(saver, PROMPTS)).toEqual(FROM_DEFAULTS)
      expect(saver.logs).toEqual(DEFAULT_LOGS)
    })
  })

  test("PluginInput.directory wins over worktree, and worktree over the global payload", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const directory = join(scope.root, "opencode-directory")
    const worktree = join(scope.root, "opencode-worktree")
    const sentinels = ["sentinel from the directory", "sentinel from the worktree", "sentinel from the global"]
    writePatterns(projectPayload(directory), listing(sentinels[0]))
    writePatterns(projectPayload(worktree), listing(sentinels[1]))
    writePatterns(globalPayload(scope), listing(sentinels[2]))

    await inScope(scope, {}, async () => {
      const module = await importPort(installed)
      const run = async () => guidanceFor(await startSaver(module, { directory, worktree }), sentinels)

      const first = await run()
      rmSync(projectPayload(directory), { recursive: true })
      const second = await run()
      rmSync(projectPayload(worktree), { recursive: true })
      const third = await run()

      expect(process.cwd()).toBe(scope.projectDir)
      expect([first, second, third]).toEqual([
        [WITH_GUIDANCE, UNTOUCHED, UNTOUCHED],
        [UNTOUCHED, WITH_GUIDANCE, UNTOUCHED],
        [UNTOUCHED, UNTOUCHED, WITH_GUIDANCE],
      ])
    })
  })

  test("the env root is exclusive: its payload is used, and without one nothing else is searched", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const others = ["sentinel from the project", "sentinel from the global"]
    writePatterns(projectPayload(scope.projectDir), listing(others[0]))
    writePatterns(globalPayload(scope), listing(others[1]))
    const withPayload = join(scope.root, "env-root")
    writePatterns(join(withPayload, PLUGIN_ID), listing(SENTINEL))
    const empty = join(scope.root, "empty-env-root")
    mkdirSync(empty)
    const prompts = [SENTINEL, ...others, DEFAULT_ONLY]

    await inScope(scope, { LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: withPayload }, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))

      expect(await guidanceFor(saver, prompts)).toEqual([WITH_GUIDANCE, UNTOUCHED, UNTOUCHED, UNTOUCHED])
      expect(saver.logs).toEqual(PAYLOAD_LOGS)
    })
    await inScope(scope, { LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: empty }, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))

      expect(await guidanceFor(saver, prompts)).toEqual([UNTOUCHED, UNTOUCHED, UNTOUCHED, WITH_GUIDANCE])
      expect(saver.logs).toEqual([
        warn(missingMessage([join(empty, PLUGIN_ID)])), vagueLog("ses_4", "msg_4"), guidanceLog("ses_4"),
      ])
    })
  })

  test.each([
    ["directory equals worktree", {}, projectRoots],
    ["worktree is empty", {}, (scope: Scope) => ({ directory: scope.projectDir, worktree: "" })],
    ["the env override is empty", { LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: "" }, projectRoots],
  ])("one project root is searched once when %s", async (_name, overrides: EnvVars, roots: (s: Scope) => Roots) => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)

    await inScope(scope, overrides, async () => {
      const saver = await startSaver(await importPort(installed), roots(scope))
      await saver.message("ses_1", "msg_1", SPECIFIC)

      expect(saver.logs).toEqual([warn(noPayloadMessage(scope))])
    })
  })

  test("a resolver failure leaves the factory working, and the built-in defaults apply", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writePatterns(globalPayload(scope), listing(SENTINEL))

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(installed), { directory: 42, worktree: scope.projectDir })

      expect(await guidanceFor(saver, PROMPTS)).toEqual(FROM_DEFAULTS)
      expect(resolverFailure()).toStartWith("TypeError")
      expect(saver.logs).toEqual([warn(failedMessage(resolverFailure())), ...DEFAULT_LOGS])
    })
  })

  test("a patterns file removed after start falls back to the defaults, and the warning names it once", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const file = writePatterns(globalPayload(scope), listing(SENTINEL))

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))
      rmSync(file)

      expect(await guidanceFor(saver, PROMPTS)).toEqual(FROM_DEFAULTS)
      const removed = `[token-saver] patterns file not found at ${file}. ${DEFAULTS_HINT}`
      expect(saver.logs).toEqual([warn(removed), ...DEFAULT_LOGS])
    })
  })

  test("the patterns file is read on every prompt, so an edit applies to the next one", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const edited = "sentinel added by an edit"
    writePatterns(globalPayload(scope), listing(SENTINEL))

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))

      await saver.message("ses_1", "msg_1", SENTINEL)
      writePatterns(globalPayload(scope), listing(edited))
      await saver.message("ses_2", "msg_2", SENTINEL)
      await saver.message("ses_3", "msg_3", edited)

      expect([await saver.transform("ses_1"), await saver.transform("ses_2"), await saver.transform("ses_3")])
        .toEqual([WITH_GUIDANCE, UNTOUCHED, WITH_GUIDANCE])
      expect(saver.logs).toEqual([
        vagueLog("ses_1", "msg_1"), vagueLog("ses_3", "msg_3"), guidanceLog("ses_1"), guidanceLog("ses_3"),
      ])
    })
  })

  // Neither an invalid nor a removed file sticks: each prompt checks and reads the file again.
  test("a fixed or re-created patterns file applies to the next prompt", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const file = writePatterns(globalPayload(scope), "[")

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))

      await saver.message("ses_1", "msg_1", SENTINEL)
      writePatterns(globalPayload(scope), listing(SENTINEL))
      await saver.message("ses_2", "msg_2", SENTINEL)
      rmSync(file)
      await saver.message("ses_3", "msg_3", SENTINEL)
      writePatterns(globalPayload(scope), listing(SENTINEL))
      await saver.message("ses_4", "msg_4", SENTINEL)

      const systems = [await saver.transform("ses_1"), await saver.transform("ses_2")]
      systems.push(await saver.transform("ses_3"), await saver.transform("ses_4"))
      expect(systems).toEqual([UNTOUCHED, WITH_GUIDANCE, UNTOUCHED, WITH_GUIDANCE])
      expect(saver.logs).toEqual([
        warn(invalidMessage(file, thrownText(() => JSON.parse("[")))), vagueLog("ses_2", "msg_2"),
        warn(`[token-saver] patterns file not found at ${file}. ${DEFAULTS_HINT}`), vagueLog("ses_4", "msg_4"),
        guidanceLog("ses_2"), guidanceLog("ses_4"),
      ])
    })
  })
})

// Not named by the Design Doc: a patterns file that cannot be read or is not valid gets the same advisor treatment as
// a missing one (built-in defaults, never a throw), logged once under its own warning kind (Error Handling: "Log once
// per warning kind").
describe("opencode-token-saver: an invalid patterns file", () => {
  test("malformed JSON falls back to the defaults and is logged once with the parse error", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const malformed = `{ "patterns": ["${SENTINEL}"`
    const file = writePatterns(globalPayload(scope), malformed)

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))

      expect(await guidanceFor(saver, PROMPTS)).toEqual(FROM_DEFAULTS)
      expect(saver.logs).toEqual([warn(invalidMessage(file, thrownText(() => JSON.parse(malformed)))), ...DEFAULT_LOGS])
      expect(thrownText(() => JSON.parse(malformed))).toStartWith("SyntaxError: JSON Parse error: ")
    })
  })

  test.each([
    ["patterns is not an array", { patterns: SENTINEL, whitelisted_prefixes: [] }],
    ["patterns is missing", { whitelisted_prefixes: [] }],
    ["whitelisted_prefixes is missing", { patterns: [SENTINEL] }],
    ["whitelisted_prefixes is not an array", { patterns: [SENTINEL], whitelisted_prefixes: "fix " }],
    ["a pattern is not a string", { patterns: [SENTINEL, 5], whitelisted_prefixes: [] }],
    ["a whitelisted prefix is not a string", { patterns: [SENTINEL], whitelisted_prefixes: [null] }],
    ["the JSON is null", "null"],
    ["the JSON is an array", JSON.stringify([SENTINEL])],
    ["the JSON is a string", JSON.stringify(SENTINEL)],
  ])("a file whose %s falls back to the defaults and is logged once", async (_name, content: unknown) => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const file = writePatterns(globalPayload(scope), content)

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))

      expect(await guidanceFor(saver, PROMPTS)).toEqual(FROM_DEFAULTS)
      expect(saver.logs).toEqual([warn(invalidMessage(file, SHAPE_ERROR)), ...DEFAULT_LOGS])
    })
  })

  test("an unreadable patterns file falls back to the defaults and is logged once with the read error", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    // A directory at the marker path: the resolver's existsSync accepts it, and the read fails.
    const file = join(globalPayload(scope), PATTERNS_FILE)
    mkdirSync(file, { recursive: true })

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))

      expect(await guidanceFor(saver, PROMPTS)).toEqual(FROM_DEFAULTS)
      const reason = thrownText(() => readFileSync(file, "utf8"))
      expect(reason).toStartWith("Error: EISDIR")
      expect(saver.logs).toEqual([warn(invalidMessage(file, reason)), ...DEFAULT_LOGS])
    })
  })
})

// The two warning kinds are separate: an invalid file never hides a missing payload, nor the reverse.
describe("opencode-token-saver: each warning kind is logged once per process", () => {
  test("an invalid patterns file does not hide a missing one, and neither repeats", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const file = writePatterns(globalPayload(scope), "[")

    await inScope(scope, {}, async () => {
      const saver = await startSaver(await importPort(installed), projectRoots(scope))

      await saver.message("ses_1", "msg_1", SPECIFIC)
      rmSync(file)
      await saver.message("ses_1", "msg_2", SPECIFIC)
      writePatterns(globalPayload(scope), "[")
      await saver.message("ses_1", "msg_3", SPECIFIC)
      rmSync(file)
      await saver.message("ses_1", "msg_4", SPECIFIC)

      expect(saver.logs).toEqual([
        warn(invalidMessage(file, thrownText(() => JSON.parse("[")))),
        warn(`[token-saver] patterns file not found at ${file}. ${DEFAULTS_HINT}`),
      ])
    })
  })

  test("a missing payload does not hide an invalid file that a later instance finds", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)

    await inScope(scope, {}, async () => {
      const module = await importPort(installed)
      const first = await startSaver(module, projectRoots(scope))
      await first.message("ses_1", "msg_1", SPECIFIC)
      const file = writePatterns(globalPayload(scope), { patterns: [SENTINEL] })
      const second = await startSaver(module, projectRoots(scope))
      await second.message("ses_1", "msg_1", SPECIFIC)
      const third = await startSaver(module, projectRoots(scope))
      await third.message("ses_1", "msg_1", SPECIFIC)
      rmSync(file)
      await third.message("ses_1", "msg_2", SPECIFIC)

      expect([first.logs, second.logs, third.logs]).toEqual([
        [warn(noPayloadMessage(scope))], [warn(invalidMessage(file, SHAPE_ERROR))], [],
      ])
    })
  })

  test("concurrent prompts still log each warning kind once", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)

    await inScope(scope, {}, async () => {
      const module = await importPort(installed)
      const missing = await startSaver(module, projectRoots(scope))
      const bothMissing = Promise.all([
        missing.message("ses_1", "msg_1", SPECIFIC), missing.message("ses_1", "msg_2", SPECIFIC),
      ])
      await expect(bothMissing).resolves.toEqual([undefined, undefined])
      const file = writePatterns(globalPayload(scope), "[")
      const invalid = await startSaver(module, projectRoots(scope))
      const bothInvalid = Promise.all([
        invalid.message("ses_1", "msg_1", SPECIFIC), invalid.message("ses_1", "msg_2", SPECIFIC),
      ])
      await expect(bothInvalid).resolves.toEqual([undefined, undefined])

      expect(missing.logs).toEqual([warn(noPayloadMessage(scope))])
      expect(invalid.logs).toEqual([warn(invalidMessage(file, thrownText(() => JSON.parse("["))))])
    })
  })

  // A kind is one warning per process: not one per reason, per file or per instance.
  test("a second invalid reason or invalid file in the same process logs nothing more", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const other = join(scope.root, "other-project")
    const file = writePatterns(globalPayload(scope), "[")
    writePatterns(projectPayload(other), { patterns: [SENTINEL] })

    await inScope(scope, {}, async () => {
      const module = await importPort(installed)
      const first = await startSaver(module, projectRoots(scope))
      await first.message("ses_1", "msg_1", SPECIFIC)
      writePatterns(globalPayload(scope), { whitelisted_prefixes: [] })
      await first.message("ses_1", "msg_2", SPECIFIC)
      const second = await startSaver(module, { directory: other, worktree: other })
      await second.message("ses_1", "msg_1", SPECIFIC)

      expect([first.logs, second.logs]).toEqual([[warn(invalidMessage(file, thrownText(() => JSON.parse("["))))], []])
    })
  })

  test("a resolver failure and a missing payload are one warning kind", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)

    await inScope(scope, {}, async () => {
      const module = await importPort(installed)
      const failing = await startSaver(module, { directory: 42, worktree: scope.projectDir })
      await failing.message("ses_1", "msg_1", SPECIFIC)
      const missing = await startSaver(module, projectRoots(scope))
      await missing.message("ses_1", "msg_1", SPECIFIC)

      expect([failing.logs, missing.logs]).toEqual([[warn(failedMessage(resolverFailure()))], []])
    })
  })

  test("a failing client.app.log never breaks the prompt check or the guidance", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const rejecting: FakeClient = { app: { log: async () => Promise.reject(new Error("log service down")) } }
    const throwing: FakeClient = {
      app: {
        log: () => {
          throw new Error("log service down")
        },
      },
    }

    await inScope(scope, {}, async () => {
      // No payload: the payload warning, the vague-prompt log and the guidance log all fail to log.
      for (const client of [rejecting, throwing]) {
        const saver = await startSaver(await importPort(installed), projectRoots(scope), client)

        expect(await guidanceFor(saver, PROMPTS)).toEqual(FROM_DEFAULTS)
      }
    })
  })
})

describe("opencode-token-saver: manifests", () => {
  // Design Doc "Version Bumps", token-saver row: plugin.json and package.json 1.0.0 -> 1.1.0; AC-045 (changelog entry)
  test("plugin.json and package.json are at 1.1.0, and the README logs 1.1.0", () => {
    const read = (path: string) => readFileSync(join(REPO_ROOT, "plugin-token-saver", path), "utf8")

    expect(JSON.parse(read(".claude-plugin/plugin.json"))).toMatchObject({ name: "token-saver", version: "1.1.0" })
    expect(JSON.parse(read("package.json"))).toMatchObject({ name: "opencode-token-saver", version: "1.1.0" })
    expect(read("README.md")).toMatch(/^## Version History\n\n### 1\.1\.0\n/m)
  })
})
