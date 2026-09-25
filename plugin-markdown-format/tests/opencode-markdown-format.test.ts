// markdown-format opencode port Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.5.2)
// Generated: 2026-09-24 | Budget Used: 1/3 integration (markdown-format slice), 0/2 E2E
//
// Run with (HOME and the bun caches pointed outside the real home):
//
//   bun test plugin-markdown-format/tests
//
// Harness (Design Doc "Test Strategy > TS"; scripts/opencode/tests/support/scope.ts):
// - Each test builds a temp scope under os.tmpdir(): HOME=<tmp>/home, XDG_CONFIG_HOME=<tmp>/config, and
//   <tmp>/config/opencode/plugins as a symlink to <tmp>/dotfiles/plugins (the live layout, N20).
// - The port is copied to <tmp>/dotfiles/plugins/opencode-markdown-format.ts and dynamic-imported through the
//   symlinked path with a unique query string, so Bun reports import.meta.url at the realpath (N19), as opencode
//   does. A fresh import is a fresh module, so "once per process" is "once per imported module" here.
// - The payload is the real plugin-markdown-format/config/ tree, copied to the candidate under test.
// - markdownlint-cli2 (and npx, where a test needs it) is a stub first on PATH that appends its name and argv to
//   STUB_LOG (Mock Boundary: markdownlint-cli2 "Yes"). The port hands process.env to its children, because Bun's
//   node:child_process otherwise gives a child the env from process start, and PATH set here would not reach it.
// - The factory gets a fake client (client.app.log records calls) and directory/worktree set to a temp project.
//   The hooks run with that project as the cwd. LLM_AGENT_WORKFLOW_PAYLOAD_ROOT is cleared unless a test sets it.
// - Every scope's bin dir starts with a tripwire npx that logs its call and exits 97, so a test that reaches npx by
//   accident fails fast, with no network and no .npm writes. A test that means to reach npx writes its own stub,
//   and runs the port in a child bun whose startup PATH holds only the stubs and `sh`: even a port that spawned
//   npx without the current env would find the stub there.

import { afterEach, describe, expect, test } from "bun:test"
import { spawnSync } from "node:child_process"
import { chmodSync, cpSync, existsSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync }
  from "node:fs"
import { join } from "node:path"
import {
  copyPort, type EnvVars, type FakeClient, fakeClient, importPort, installPayload, makeScope, REPO_ROOT, type Scope,
  scopeEnv, withEnv,
} from "../../scripts/opencode/tests/support/scope"

const PORT = "plugin-markdown-format/plugins/opencode-markdown-format.ts"
const CONFIG_SOURCE = "plugin-markdown-format/config"
const PLUGIN_ID = "markdown-format"
const PAYLOAD = join("llm-agent-workflow", PLUGIN_ID)
const CONFIG = join("config", ".markdownlint.json")

const NO_CONFIG = "Formatting without --config. Reinstall with ./setup-opencode.sh --global --plugin markdown-format"
const NOT_FOUND =
  "[markdown-format] markdownlint-cli2 not found and npx unavailable. Install: npm install -g markdownlint-cli2"

// Exit status of the tripwire npx in every scope's bin dir.
const TRIPWIRE_EXIT = 97

// Appends one record per call to STUB_LOG: the stub's own name, then each argument, separated by \x1f.
const LOG_ARGV =
  '{ printf "%s" "${0##*/}"; for arg in "$@"; do printf "\\037%s" "$arg"; done; printf "\\n"; } >> "$STUB_LOG"'

type AfterHook = (input: unknown, output: unknown) => Promise<void>

interface Formatter {
  // One tool.execute.after call; the output title defaults to "".
  after(tool: string, args: Record<string, unknown>, title?: string): Promise<void>
  hook: AfterHook
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
  writeStub(scope, "npx", `exit ${TRIPWIRE_EXIT}`)
  return scope
}

function projectRoots(scope: Scope): Roots {
  return { directory: scope.projectDir, worktree: scope.projectDir }
}

function globalConfig(scope: Scope): string {
  return join(scope.configHome, "opencode", PAYLOAD, CONFIG)
}

// Copies the real config/ tree to <dir>/config and returns the config file's path.
function placeConfig(dir: string): string {
  cpSync(join(REPO_ROOT, CONFIG_SOURCE), join(dir, "config"), { recursive: true })
  return join(dir, CONFIG)
}

function projectPayload(root: string): string {
  return join(root, ".opencode", PAYLOAD)
}

function binDir(scope: Scope): string {
  const dir = join(scope.root, "bin")
  mkdirSync(dir, { recursive: true })
  return dir
}

function stubLog(scope: Scope): string {
  return join(scope.root, "stub.log")
}

// Writes an executable stub named NAME into DIR: it logs its argv, then runs TAIL.
function writeStubAt(dir: string, name: string, tail = "exit 0"): string {
  mkdirSync(dir, { recursive: true })
  const path = join(dir, name)
  writeFileSync(path, `#!/bin/sh\n${LOG_ARGV}\n${tail}\n`)
  chmodSync(path, 0o755)
  return path
}

// The same, in the scope's bin dir, which inScope puts first on PATH.
function writeStub(scope: Scope, name: string, tail = "exit 0"): string {
  return writeStubAt(binDir(scope), name, tail)
}

// A dir holding only `sh`, which findExecutable needs, so PATH can leave out every real linter and npx.
function shOnlyDir(scope: Scope): string {
  const dir = join(scope.root, "sh-only")
  mkdirSync(dir)
  symlinkSync("/bin/sh", join(dir, "sh"))
  return dir
}

// Each call the stubs logged, as [name, ...argv]; [] when nothing ran.
function stubCalls(scope: Scope): string[][] {
  if (!existsSync(stubLog(scope))) return []
  return readFileSync(stubLog(scope), "utf8").split("\n").filter(Boolean).map((line) => line.split("\x1f"))
}

function info(file: string, exit: number): unknown {
  const message = `[markdown-format] markdownlint-cli2 --fix ${file} (exit ${exit})`
  return { body: { service: "markdown-format", level: "info", message } }
}

function warn(message: string): unknown {
  return { body: { service: "markdown-format", level: "warn", message } }
}

function missingMessage(searched: string[]): string {
  return `[markdown-format] payload not found. Searched: ${searched.join(", ")}. ${NO_CONFIG}`
}

// The payload warning for projectRoots(scope) with no payload anywhere.
function noPayloadMessage(scope: Scope): string {
  return missingMessage([
    projectPayload(scope.projectDir), join(scope.configHome, "opencode", PAYLOAD), join(scope.root, "dotfiles"),
  ])
}

function markdownFile(scope: Scope, name: string): string {
  const path = join(scope.projectDir, name)
  writeFileSync(path, "# Title\n")
  return path
}

// Runs fn with the scope env, the stub bin dir first on PATH (unless PATH is overridden), and the temp project as
// the cwd, and restores all of them afterwards.
async function inScope<T>(scope: Scope, overrides: EnvVars, fn: () => Promise<T>): Promise<T> {
  const path = `${binDir(scope)}:${process.env.PATH ?? ""}`
  const env = scopeEnv(scope, { PATH: path, STUB_LOG: stubLog(scope), ...overrides })
  const previous = process.cwd()
  process.chdir(scope.projectDir)
  try {
    return await withEnv(env, fn)
  } finally {
    process.chdir(previous)
  }
}

async function startFormatter(module: Record<string, unknown>, roots: Roots, custom?: FakeClient): Promise<Formatter> {
  const recorder = fakeClient()
  const client = custom ?? recorder.client
  const factory = module.MarkdownFormatPlugin as (input: Record<string, unknown>) => Promise<Record<string, AfterHook>>
  const hooks = await factory({ client, directory: roots.directory, worktree: roots.worktree })
  const hook = hooks["tool.execute.after"]
  const after = (tool: string, args: Record<string, unknown>, title = "") =>
    hook({ tool, sessionID: "ses_test", callID: "call_test", args }, { title, output: "", metadata: {} })
  return { after, hook, logs: recorder.logs }
}

// Imports the port given as argv[2], starts it on directory = worktree = argv[3], sends one edit of argv[4], and prints
// the client.app.log entries as JSON.
const CHILD_PROBE = [
  "const [port, directory, filePath] = process.argv.slice(2)",
  "const logs: unknown[] = []",
  "const client = { app: { log: async (entry: unknown) => { logs.push(entry); return { data: true } } } }",
  "const hooks = await (await import(port)).MarkdownFormatPlugin({ client, directory, worktree: directory })",
  'const input = { tool: "edit", sessionID: "s", callID: "c", args: { filePath } }',
  'await hooks["tool.execute.after"](input, { title: "", output: "", metadata: {} })',
  "console.log(JSON.stringify(logs))",
  "",
].join("\n")

// Runs CHILD_PROBE in a child bun whose whole env is built here, with PATH as its startup PATH, and returns the logs.
function runChild(scope: Scope, installed: string, file: string, path: string): unknown[] {
  const probe = join(scope.root, "probe.ts")
  writeFileSync(probe, CHILD_PROBE)
  const env = {
    PATH: path,
    HOME: scope.home,
    XDG_CONFIG_HOME: scope.configHome,
    XDG_CACHE_HOME: join(scope.root, "cache"),
    XDG_DATA_HOME: join(scope.root, "data"),
    BUN_INSTALL_CACHE_DIR: join(scope.root, "cache", "bun"),
    TMPDIR: scope.root,
    STUB_LOG: stubLog(scope),
  }
  // Half the calling test's 60 s timeout, so a hung child fails the status/stderr check below, not as a bare timeout.
  const result = spawnSync(process.execPath, [probe, installed, scope.projectDir, file], {
    cwd: scope.projectDir, env, encoding: "utf8", timeout: 30_000,
  })
  expect({ status: result.status, stderr: result.stderr }).toEqual({ status: 0, stderr: "" })
  return JSON.parse(result.stdout) as unknown[]
}

// Design Doc F1 table, markdown-format row: marker config/.markdownlint.json; on missing, log once and no --config.
describe("opencode-markdown-format: payload resolution on the installed layout", () => {
  // AC-005 (D4, [bun]), markdown-format bullet
  // AC-005 (part): "Installed into a temp scope: ... markdown-format shall pass --config
  //   .../llm-agent-workflow/markdown-format/config/.markdownlint.json"
  // Given: the installed layout above and a .md file in the temp project.
  // When: tool.execute.after for an edit of that .md file.
  // Then: the linter gets the payload config.
  // Verification items:
  //   - STUB_LOG has one call whose argv holds "--config <XDG_CONFIG_HOME>/opencode/llm-agent-workflow/
  //     markdown-format/config/.markdownlint.json", and that path exists
  //   - the same argv names the edited file
  //   - client.app.log has no payload-missing warning
  // Pass criteria: exactly one stub call with those arguments.
  // ROI: 61 (BV:6 x Freq:9 + Legal:0 + Defect:7)
  // @category: core-functionality
  // @dependency: opencode-markdown-format.ts, payload resolver, markdownlint-cli2 (stub)
  // @real-dependency: filesystem (symlinks)
  // @complexity: low
  test("AC-005: passes --config pointing at the installed .markdownlint.json payload", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "markdownlint-cli2")
    const file = markdownFile(scope, "notes.md")
    expect(realpathSync(installed)).toBe(join(scope.root, "dotfiles", "plugins", "opencode-markdown-format.ts"))

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()

      expect(stubCalls(scope)).toEqual([["markdownlint-cli2", "--fix", "--config", globalConfig(scope), file]])
      const source = join(REPO_ROOT, CONFIG_SOURCE, ".markdownlint.json")
      expect(readFileSync(globalConfig(scope), "utf8")).toBe(readFileSync(source, "utf8"))
      expect(formatter.logs).toEqual([info(file, 0)])
    })
  })

  // F1 table, markdown-format row: "On missing: log once; no --config"; advisors always exit 0
  // Given: the same symlinked layout with no payload anywhere (the port sits outside the repo, so the dev-layout
  //   candidate has no config either), and a worktree that differs from directory.
  // When: two .md writes on one instance, then a third on a second instance of the same module.
  // Then: every write is formatted without --config, and the payload warning is logged once per process.
  // Verification items:
  //   - no call throws
  //   - each stub call is exactly `--fix <file>`
  //   - the warning lists every searched candidate and the reinstall command, and is the only warning
  //   - the second instance logs no warning
  test("without a payload, the linter runs without --config and one warning is logged per process", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writeStub(scope, "markdownlint-cli2")
    const worktree = join(scope.root, "worktree")
    mkdirSync(worktree)
    const [first, second, third] = ["a.md", "b.md", "c.md"].map((name) => markdownFile(scope, name))
    const missing = missingMessage([
      projectPayload(scope.projectDir),
      projectPayload(worktree),
      join(scope.configHome, "opencode", PAYLOAD),
      join(scope.root, "dotfiles"),
    ])

    await inScope(scope, {}, async () => {
      const module = await importPort(installed)
      const formatter = await startFormatter(module, { directory: scope.projectDir, worktree })

      await expect(formatter.after("edit", { filePath: first })).resolves.toBeUndefined()
      await expect(formatter.after("write", { filePath: second })).resolves.toBeUndefined()
      const again = await startFormatter(module, { directory: scope.projectDir, worktree })
      await expect(again.after("edit", { filePath: third })).resolves.toBeUndefined()

      expect(stubCalls(scope)).toEqual([
        ["markdownlint-cli2", "--fix", first],
        ["markdownlint-cli2", "--fix", second],
        ["markdownlint-cli2", "--fix", third],
      ])
      expect(formatter.logs).toEqual([warn(missing), info(first, 0), info(second, 0)])
      expect(again.logs).toEqual([info(third, 0)])
    })
  })
})

// Design Doc F1: the env root (exclusive), then the project (directory, then worktree), then global, then dev.
describe("opencode-markdown-format: payload candidates (F1)", () => {
  test("the port in the repo finds plugin-markdown-format/config through the dev-layout candidate", async () => {
    const scope = newScope()
    writeStub(scope, "markdownlint-cli2")
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(join(REPO_ROOT, PORT)), projectRoots(scope))
      await formatter.after("edit", { filePath: file })

      const devConfig = join(REPO_ROOT, "plugin-markdown-format", CONFIG)
      expect(stubCalls(scope)).toEqual([["markdownlint-cli2", "--fix", "--config", devConfig, file]])
      expect(formatter.logs).toEqual([info(file, 0)])
    })
  })

  test("PluginInput.directory wins over worktree, and worktree over the global payload", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writeStub(scope, "markdownlint-cli2")
    const directory = join(scope.root, "opencode-directory")
    const worktree = join(scope.root, "opencode-worktree")
    const directoryConfig = placeConfig(projectPayload(directory))
    const worktreeConfig = placeConfig(projectPayload(worktree))
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const module = await importPort(installed)
      const run = async () => (await startFormatter(module, { directory, worktree })).after("edit", { filePath: file })

      await run()
      rmSync(projectPayload(directory), { recursive: true })
      await run()
      rmSync(projectPayload(worktree), { recursive: true })
      await run()

      expect(process.cwd()).toBe(scope.projectDir)
      expect(stubCalls(scope).map((call) => call[3])).toEqual([directoryConfig, worktreeConfig, globalConfig(scope)])
    })
  })

  test("the env root is exclusive: its payload is used, and without one nothing else is searched", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writeStub(scope, "markdownlint-cli2")
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    placeConfig(projectPayload(scope.projectDir))
    const withPayload = join(scope.root, "env-root")
    const envConfig = placeConfig(join(withPayload, PLUGIN_ID))
    const empty = join(scope.root, "empty-env-root")
    mkdirSync(empty)
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, { LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: withPayload }, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))
      await formatter.after("edit", { filePath: file })

      expect(formatter.logs).toEqual([info(file, 0)])
    })
    await inScope(scope, { LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: empty }, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))
      await formatter.after("edit", { filePath: file })

      expect(formatter.logs).toEqual([warn(missingMessage([join(empty, PLUGIN_ID)])), info(file, 0)])
    })
    expect(stubCalls(scope)).toEqual([
      ["markdownlint-cli2", "--fix", "--config", envConfig, file],
      ["markdownlint-cli2", "--fix", file],
    ])
  })

  test("a resolver failure leaves the factory working, and the linter runs without --config", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "markdownlint-cli2")
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), { directory: 42, worktree: scope.projectDir })

      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()
      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()

      expect(stubCalls(scope)).toEqual([["markdownlint-cli2", "--fix", file], ["markdownlint-cli2", "--fix", file]])
      expect(formatter.logs).toHaveLength(3)
      const [warning] = formatter.logs as { body: { service: string; level: string; message: string } }[]
      expect(warning.body.service).toBe("markdown-format")
      expect(warning.body.level).toBe("warn")
      expect(warning.body.message).toStartWith("[markdown-format] payload resolution failed (TypeError")
      expect(warning.body.message).toEndWith(`). ${NO_CONFIG}`)
      expect(formatter.logs.slice(1)).toEqual([info(file, 0), info(file, 0)])
    })
  })

  test("a config removed after start is not passed, and the warning names it once", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "markdownlint-cli2")
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))
      rmSync(globalConfig(scope))

      await formatter.after("edit", { filePath: file })
      await formatter.after("edit", { filePath: file })

      expect(stubCalls(scope)).toEqual([["markdownlint-cli2", "--fix", file], ["markdownlint-cli2", "--fix", file]])
      const removed = `[markdown-format] config not found at ${globalConfig(scope)}. ${NO_CONFIG}`
      expect(formatter.logs).toEqual([warn(removed), info(file, 0), info(file, 0)])
    })
  })
})

// hooks/hooks.json matcher Write|Edit|MultiEdit, as FILE_TOOL_RE; markdown_format_hook.py formats .md paths only.
describe("opencode-markdown-format: which tool calls reach the linter", () => {
  test("non-.md files and tools that do not write files are untouched", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "markdownlint-cli2")
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await formatter.after("bash", { command: `cat ${file}`, filePath: file })
      await formatter.after("read", { filePath: file })
      await formatter.after("rewrite", { filePath: file })
      await formatter.after("mcp__notes__edit_history", { filePath: file })
      await formatter.after("write", { filePath: join(scope.projectDir, "notes.txt") })
      await formatter.after("edit", { filePath: join(scope.projectDir, "notes.md.bak") })
      await formatter.after("edit", { filePath: join(scope.projectDir, "NOTES.MD") })
      await formatter.after("edit", {}, "Edited notes.txt")
      await formatter.after("edit", { filePath: 42 }, "Read notes.md")

      expect(stubCalls(scope)).toEqual([])
      expect(formatter.logs).toEqual([])
    })
  })

  test("every file tool and path argument that names a .md file is formatted", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "markdownlint-cli2")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await formatter.after("write", { filePath: "a.md" })
      await formatter.after("edit", { file_path: "b.md" })
      await formatter.after("mcp__filesystem__write_file", { path: "c.md" })
      await formatter.after("multiedit", { filepath: "d.md" })
      await formatter.after("apply_patch", {}, "Patched docs/e.md")
      await formatter.after("patch", { filePath: "f.txt", path: "g.md" })
      await formatter.after("Edit", { filePath: "h.md" })
      await formatter.after("apply_patch", {}, "Edited docs/i.md")
      await formatter.after("apply_patch", {}, "Wrote docs/j.md")
      await formatter.after("apply_patch", {}, "Created docs/k.md")
      await formatter.after("apply_patch", {}, "Updated docs/l.md")
      await formatter.after("edit", { filePath: "m.md" }, "Edited n.md")

      const files = [
        "a.md", "b.md", "c.md", "d.md", "docs/e.md", "g.md", "h.md", "docs/i.md", "docs/j.md", "docs/k.md", "docs/l.md",
        "m.md",
      ]
      expect(stubCalls(scope).map((call) => call.slice(1))).toEqual(
        files.map((file) => ["--fix", "--config", globalConfig(scope), file]),
      )
    })
  })
})

// Design Doc Error Handling, advisor row (log once) and Log levels (warn); Applicable Standards: advisors exit 0.
describe("opencode-markdown-format: an advisor never breaks the write", () => {
  test("a linter exit other than 0 or 1 is logged once, apart from the payload warning", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writeStub(scope, "markdownlint-cli2", "printf 'config broke\\n' >&2\nexit 2")
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()
      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()

      expect(stubCalls(scope)).toEqual([["markdownlint-cli2", "--fix", file], ["markdownlint-cli2", "--fix", file]])
      expect(formatter.logs).toEqual([
        warn(noPayloadMessage(scope)),
        warn("[markdown-format] unexpected exit 2: config broke"),
      ])
    })
  })

  test("a linter exit 1 (violations left) is a normal run", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "markdownlint-cli2", "printf 'MD001 left\\n' >&2\nexit 1")
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))
      await formatter.after("edit", { filePath: file })

      expect(formatter.logs).toEqual([info(file, 1)])
    })
  })

  test("a linter killed by a signal is logged with the signal", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "markdownlint-cli2", "kill -KILL $$")
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()

      expect(stubCalls(scope)).toHaveLength(1)
      expect(formatter.logs).toEqual([warn("[markdown-format] unexpected exit SIGKILL: ")])
    })
  })

  test("a linter that cannot start is logged once with the spawn error", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    const linter = join(binDir(scope), "markdownlint-cli2")
    writeFileSync(linter, "#!/no/such/interpreter\nexit 0\n")
    chmodSync(linter, 0o755)
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()
      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()

      expect(formatter.logs).toHaveLength(1)
      const [{ body }] = formatter.logs as { body: { service: string; level: string; message: string } }[]
      expect(body.service).toBe("markdown-format")
      expect(body.level).toBe("warn")
      expect(body.message).toStartWith(`[markdown-format] could not run ${linter}: ENOENT`)
    })
  })

  // The two warning kinds are separate: a payload warning never hides a linter failure, nor the reverse.
  test("a missing payload does not hide a linter that cannot start, and neither repeats", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const linter = join(binDir(scope), "markdownlint-cli2")
    writeFileSync(linter, "#!/no/such/interpreter\nexit 0\n")
    chmodSync(linter, 0o755)
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()
      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()

      expect(formatter.logs).toHaveLength(2)
      expect(formatter.logs[0]).toEqual(warn(noPayloadMessage(scope)))
      const { body } = formatter.logs[1] as { body: { service: string; level: string; message: string } }
      expect(body.level).toBe("warn")
      expect(body.message).toStartWith(`[markdown-format] could not run ${linter}: ENOENT`)
    })
  })

  test("a missing payload does not hide an error while formatting", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writeStub(scope, "markdownlint-cli2")
    const file = join(scope.projectDir, "bad\u0000name.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()

      expect(stubCalls(scope)).toEqual([])
      expect(formatter.logs).toHaveLength(2)
      expect(formatter.logs[0]).toEqual(warn(noPayloadMessage(scope)))
      const { body } = formatter.logs[1] as { body: { service: string; level: string; message: string } }
      expect(body.level).toBe("warn")
      expect(body.message).toStartWith(
        "[markdown-format] error: The argument 'args[1]' must be a string without null bytes.",
      )
    })
  })

  test("a missing linter does not hide the payload warning once the linter appears", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const shOnly = shOnlyDir(scope)
    const file = markdownFile(scope, "notes.md")

    const formatter = await inScope(scope, { PATH: shOnly }, async () => {
      const started = await startFormatter(await importPort(installed), projectRoots(scope))
      await started.after("edit", { filePath: file })
      return started
    })
    writeStub(scope, "markdownlint-cli2")
    await inScope(scope, {}, () => formatter.after("edit", { filePath: file }))

    expect(stubCalls(scope)).toEqual([["markdownlint-cli2", "--fix", file]])
    expect(formatter.logs).toEqual([warn(NOT_FOUND), warn(noPayloadMessage(scope)), info(file, 0)])
  })

  test("concurrent writes still log each warning kind once", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writeStub(scope, "markdownlint-cli2", "printf 'config broke\\n' >&2\nexit 2")
    const [first, second] = ["a.md", "b.md"].map((name) => markdownFile(scope, name))

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      const both = Promise.all([
        formatter.after("edit", { filePath: first }), formatter.after("edit", { filePath: second }),
      ])
      await expect(both).resolves.toEqual([undefined, undefined])

      expect(stubCalls(scope)).toHaveLength(2)
      expect(stubCalls(scope)).toContainEqual(["markdownlint-cli2", "--fix", first])
      expect(stubCalls(scope)).toContainEqual(["markdownlint-cli2", "--fix", second])
      expect(formatter.logs).toEqual([
        warn(noPayloadMessage(scope)), warn("[markdown-format] unexpected exit 2: config broke"),
      ])
    })
  })

  test("without markdownlint-cli2 and npx, one install hint is logged and nothing runs", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, { PATH: shOnlyDir(scope) }, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()
      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()

      expect(formatter.logs).toEqual([warn(NOT_FOUND)])
    })
  })

  test("without markdownlint-cli2, npx runs it with the payload config", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "npx")
    const file = markdownFile(scope, "notes.md")

    const result = runChild(scope, installed, file, `${binDir(scope)}:${shOnlyDir(scope)}`)

    expect(stubCalls(scope)).toEqual([["npx", "markdownlint-cli2", "--fix", "--config", globalConfig(scope), file]])
    expect(result).toEqual([info(file, 0)])
  }, 60_000)

  test("the Windows `where` fallback finds the linter when `command -v` does not", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    // Off PATH, so only `where` can report it; the stub `cmd` prints it first, CRLF-terminated, as where does.
    const linter = writeStubAt(join(scope.root, "windows-bin"), "markdownlint-cli2")
    writeStub(scope, "cmd", `printf '%s\\r\\n%s\\r\\n' '${linter}' '/second/match'`)
    const file = markdownFile(scope, "notes.md")

    await inScope(scope, { PATH: `${binDir(scope)}:${shOnlyDir(scope)}` }, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))
      await formatter.after("edit", { filePath: file })

      expect(stubCalls(scope)).toEqual([
        ["cmd", "/c", "where", "markdownlint-cli2"],
        ["markdownlint-cli2", "--fix", "--config", globalConfig(scope), file],
      ])
      expect(formatter.logs).toEqual([info(file, 0)])
    })
  })

  test("an error while formatting is swallowed and logged once", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "markdownlint-cli2")
    // spawnSync throws a TypeError for a NUL byte in an argument.
    const file = join(scope.projectDir, "bad\u0000name.md")

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()
      await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()

      expect(stubCalls(scope)).toEqual([])
      expect(formatter.logs).toHaveLength(1)
      const [{ body }] = formatter.logs as { body: { service: string; level: string; message: string } }[]
      expect(body.level).toBe("warn")
      expect(body.message).toStartWith(
        "[markdown-format] error: The argument 'args[3]' must be a string without null bytes.",
      )
    })
  })

  test("a malformed hook call never throws, and a thrown non-Error is logged as text", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [CONFIG_SOURCE])
    writeStub(scope, "markdownlint-cli2")
    const file = markdownFile(scope, "notes.md")
    // A caller that throws a bare string, to reach the catch-all's non-Error branch.
    const throwing = {
      get tool(): string {
        throw "tool unreadable"
      },
    }

    await inScope(scope, {}, async () => {
      const formatter = await startFormatter(await importPort(installed), projectRoots(scope))

      await expect(formatter.hook({ tool: "edit", args: { filePath: file } }, undefined)).resolves.toBeUndefined()
      await expect(formatter.hook(throwing, {})).resolves.toBeUndefined()
      await expect(formatter.hook(null, {})).resolves.toBeUndefined()

      expect(stubCalls(scope)).toEqual([["markdownlint-cli2", "--fix", "--config", globalConfig(scope), file]])
      expect(formatter.logs).toEqual([info(file, 0), warn("[markdown-format] error: tool unreadable")])
    })
  })

  test("a failing client.app.log never breaks the hook or the formatting", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    writeStub(scope, "markdownlint-cli2", "exit 2")
    const file = markdownFile(scope, "notes.md")
    const rejecting: FakeClient = { app: { log: async () => Promise.reject(new Error("log service down")) } }
    const throwing: FakeClient = {
      app: {
        log: () => {
          throw new Error("log service down")
        },
      },
    }

    await inScope(scope, {}, async () => {
      for (const client of [rejecting, throwing]) {
        // No payload and an exit 2: both warnings fail to log, and the linter still ran.
        const formatter = await startFormatter(await importPort(installed), projectRoots(scope), client)
        await expect(formatter.after("edit", { filePath: file })).resolves.toBeUndefined()
        // A fresh module, so the first warning comes from the catch-all, and it fails to log too.
        const fresh = await startFormatter(await importPort(installed), projectRoots(scope), client)
        await expect(fresh.hook(null, {})).resolves.toBeUndefined()
      }

      expect(stubCalls(scope)).toEqual([["markdownlint-cli2", "--fix", file], ["markdownlint-cli2", "--fix", file]])
    })
  })
})

// Outside the "opencode-markdown-format: " describes: it checks the harness, not the port.
describe("markdown-format harness: the npx tripwire", () => {
  test("every scope's bin dir starts with an npx that logs its call and exits 97", () => {
    const scope = newScope()

    const result = spawnSync(join(binDir(scope), "npx"), ["markdownlint-cli2", "--fix"], {
      env: { PATH: "/usr/bin:/bin", STUB_LOG: stubLog(scope) }, encoding: "utf8",
    })

    expect({ status: result.status, error: result.error }).toEqual({ status: 97, error: undefined })
    expect(stubCalls(scope)).toEqual([["npx", "markdownlint-cli2", "--fix"]])
  })
})

describe("opencode-markdown-format: manifests", () => {
  // AC-044 (D, [unit]), markdown-format; AC-045 (every changed plugin has a changelog entry for its new version)
  test("package.json has exactly the 7 keys at the plugin.json version, and the README logs 1.1.0", () => {
    const read = (path: string) => readFileSync(join(REPO_ROOT, "plugin-markdown-format", path), "utf8")
    const manifest = JSON.parse(read("package.json"))

    expect(manifest).toEqual({
      name: "opencode-markdown-format",
      version: "1.1.0",
      description: expect.any(String),
      type: "module",
      author: { name: "John Cyrill Corsanes", url: "https://github.com/jcchikikomori" },
      homepage: "https://github.com/jcchikikomori/llm-agent-workflow",
      license: "MIT",
    })
    expect(Object.keys(manifest).sort()).toEqual(
      ["author", "description", "homepage", "license", "name", "type", "version"],
    )
    expect(manifest.description.length).toBeGreaterThan(0)
    expect(JSON.parse(read(".claude-plugin/plugin.json")).version).toBe("1.1.0")
    expect(read("README.md")).toMatch(/^## Version History\n\n### 1\.1\.0\n/m)
  })
})
