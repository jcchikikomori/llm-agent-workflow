// commit-guard opencode port Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.5.2)
// Generated: 2026-09-24 | Budget Used: 3/3 integration (commit-guard slice), 0/2 E2E
//
// Run with (HOME and the bun caches pointed outside the real home):
//
//   bun test plugin-commit-guard/tests
//
// Harness (Design Doc "Test Strategy > TS"; scripts/opencode/tests/support/scope.ts):
// - Each test builds a temp scope under os.tmpdir(): HOME=<tmp>/home, XDG_CONFIG_HOME=<tmp>/config, and
//   <tmp>/config/opencode/plugins as a symlink to <tmp>/dotfiles/plugins (the live layout, N20).
// - The port is copied to <tmp>/dotfiles/plugins/opencode-commit-guard.ts and dynamic-imported through the symlinked
//   path with a unique query string, so Bun reports import.meta.url at the realpath (N19), as opencode does.
// - The payload is the real plugin-commit-guard/hooks/ tree, copied to the candidate under test. The hook is not
//   mocked (Mock Boundary: commit_guard_hook.py "No").
// - The factory gets a fake client (client.app.log records calls) and directory/worktree set to a temp project.
// - LLM_AGENT_WORKFLOW_PAYLOAD_ROOT is cleared unless a test sets it, and COMMIT_GUARD_MODE is "delegate".
// - The hooks run with the temp project as the cwd. It is not a git repo (GIT_CEILING_DIRECTORIES stops the search
//   at the temp root, and scopeEnv drops every inherited GIT_* variable, GIT_DIR included), so the classifier
//   delegates without a repo snapshot and writes no ledger. That matters because the port's state dir hangs off
//   os.homedir(), which Bun caches at startup: HOME set here cannot move it.
// - The two tests that need a real repo, a token or a ledger run the port in a child bun whose HOME is inside the
//   scope, so its cached os.homedir() is too. The last test re-runs this file in such a child with GIT_DIR and a
//   config injection inherited, and checks that nothing changes and nothing lands under the child's HOME.

import { afterEach, describe, expect, test } from "bun:test"
import { spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import { chmodSync, cpSync, existsSync, mkdirSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs"
import { basename, join } from "node:path"
import {
  copyPort, type EnvVars, fakeClient, importPort, installPayload, makeScope, REPO_ROOT, type Scope, scopeEnv, withEnv,
} from "../../scripts/opencode/tests/support/scope"

const PORT = "plugin-commit-guard/plugins/opencode-commit-guard.ts"
const HOOKS = "plugin-commit-guard/hooks"
const PLUGIN_ID = "commit-guard"
const PAYLOAD = join("llm-agent-workflow", PLUGIN_ID)
const HOOK = join("hooks", "commit_guard_hook.py")

const MISSING_TAIL = "Blocking to be safe. Reinstall with ./setup-opencode.sh --global --plugin commit-guard"
const HAND_OVER = "Blocking to be safe \u2014 hand the command to the user and ask them to run it."
const DELEGATED =
  "[commit-guard] DELEGATED: this command writes a commit message and must be run\n" +
  "by the user, in their own terminal.\n"
const NO_SNAPSHOT = "(repo not readable -- ask the user to run it and tell you when done)"
const DELEGATED_LOG = {
  body: { service: "commit-guard", level: "warn", message: "Delegated a commit-writing git command to the user" },
}

type ToolHook = (
  input: { tool: string; sessionID: string; callID: string },
  output: { args: Record<string, unknown> },
) => Promise<void>

interface Guard {
  bash(command: string): Promise<void>
  tool(name: string, args: Record<string, unknown>): Promise<void>
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

function projectPayload(scope: Scope): string {
  return join(scope.projectDir, ".opencode", PAYLOAD)
}

function globalPayload(scope: Scope): string {
  return join(scope.configHome, "opencode", PAYLOAD)
}

function missingMessage(searched: string[]): string {
  return `[commit-guard] payload not found. Searched: ${searched.join(", ")}. ${MISSING_TAIL}`
}

function warnLog(message: string): unknown {
  return { body: { service: "commit-guard", level: "warn", message } }
}

// Copies the real hooks/ tree to <dir>/hooks, without bytecode, like installPayload does for the scope root.
function placeHooks(dir: string): string {
  cpSync(join(REPO_ROOT, HOOKS), join(dir, "hooks"), {
    recursive: true,
    filter: (src) => basename(src) !== "__pycache__" && !src.endsWith(".pyc"),
  })
  return dir
}

function writeExecutable(path: string, text: string): string {
  writeFileSync(path, text)
  chmodSync(path, 0o755)
  return path
}

// Runs fn with the scope env and the temp project as the cwd, and restores both afterwards.
async function inScope<T>(scope: Scope, overrides: EnvVars, fn: () => Promise<T>): Promise<T> {
  const env = scopeEnv(scope, {
    COMMIT_GUARD_MODE: "delegate",
    COMMIT_GUARD_PYTHON: undefined,
    GIT_CEILING_DIRECTORIES: scope.root,
    ...overrides,
  })
  const previous = process.cwd()
  process.chdir(scope.projectDir)
  try {
    return await withEnv(env, fn)
  } finally {
    process.chdir(previous)
  }
}

async function startGuard(module: Record<string, unknown>, roots: Roots): Promise<Guard> {
  const { client, logs } = fakeClient()
  const factory = module.opencodeCommitGuard as (input: Record<string, unknown>) => Promise<Record<string, ToolHook>>
  const hooks = await factory({ client, directory: roots.directory, worktree: roots.worktree })
  const before = hooks["tool.execute.before"]
  const tool = (name: string, args: Record<string, unknown>) =>
    before({ tool: name, sessionID: "ses_test", callID: "call_test" }, { args })
  return { bash: (command) => tool("bash", { command }), tool, logs }
}

// Real git with only the scope's config, for fixture repos.
function scopeGit(scope: Scope, args: string[]): void {
  const result = spawnSync("git", args, {
    encoding: "utf8",
    env: {
      PATH: process.env.PATH ?? "", HOME: scope.home, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: scope.gitconfig,
    },
  })
  expect({ status: result.status, stderr: result.stderr }).toEqual({ status: 0, stderr: "" })
}

// The env for a child bun: this process's env without GIT_*, COMMIT_GUARD_* or the payload override, plus OVERRIDES.
// A child gets its own cached os.homedir(), so HOME set here (unlike in withEnv) moves the port's config dir.
function childEnv(overrides: Record<string, string>): Record<string, string> {
  const env: Record<string, string> = {}
  for (const [key, value] of Object.entries(process.env)) {
    const dropped =
      key.startsWith("GIT_") || key.startsWith("COMMIT_GUARD_") || key === "LLM_AGENT_WORKFLOW_PAYLOAD_ROOT"
    if (value !== undefined && !dropped) env[key] = value
  }
  return { ...env, ...overrides }
}

async function rejection(call: Promise<unknown>): Promise<Error> {
  try {
    await call
  } catch (error) {
    if (error instanceof Error) return error
    throw new Error(`the call rejected with a non-Error: ${String(error)}`)
  }
  throw new Error("expected the call to throw, but it returned")
}

describe("opencode-commit-guard: payload resolution on the installed layout", () => {
  // AC-003 (D4, [bun]); the Design Doc's Early Verification Point and the latent commit-guard bug
  // AC: "Given commit-guard installed into a temp global scope whose plugins/ is a symlink to another directory, the
  //   installed port shall allow git status and delegate git commit -m "x". This proves resolution survives
  //   realpaths."
  // Given: the port behind the symlinked plugins/; the payload only at
  //   <XDG_CONFIG_HOME>/opencode/llm-agent-workflow/commit-guard/hooks/commit_guard_hook.py; a project without a
  //   payload.
  // When: tool.execute.before for tool "bash" with `git status`, then with `git commit -m "x"`.
  // Then: the global payload is found even though the port's realpath is in the dotfiles dir.
  // Verification items:
  //   - `git status` resolves; the hook allowed it
  //   - `git commit -m "x"` throws commit_guard_hook.py's delegation message (the exit 2 path), not the
  //     payload-missing message
  //   - client.app.log has no payload-missing warning
  // Pass criteria: both calls behave as above with the port imported through the symlink.
  // ROI: 110 (BV:10 x Freq:10 + Legal:0 + Defect:10)
  // @category: core-functionality
  // @dependency: opencode-commit-guard.ts, payload resolver, commit_guard_hook.py
  // @real-dependency: commit_guard_hook.py, filesystem (symlinks)
  // @complexity: high
  test("AC-003: installed through a symlinked plugins/, allows git status and delegates git commit", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [HOOKS])
    expect(realpathSync(installed)).toBe(join(scope.root, "dotfiles", "plugins", "opencode-commit-guard.ts"))

    await inScope(scope, {}, async () => {
      const guard = await startGuard(await importPort(installed), projectRoots(scope))

      await expect(guard.bash("git status")).resolves.toBeUndefined()
      const delegated = await rejection(guard.bash('git commit -m "x"'))

      expect(delegated.message.startsWith(DELEGATED)).toBe(true)
      expect(delegated.message).toContain('\n  git commit -m "x"\n')
      expect(delegated.message).toContain(NO_SNAPSHOT)
      expect(delegated.message).not.toContain("payload not found")
      expect(guard.logs).toEqual([DELEGATED_LOG])
    })
  })

  // AC-004 (D4, [bun]); guards fail closed
  // AC: "If the payload is absent, then a git bash call shall throw a message listing every searched path and
  //   setup-opencode.sh."
  // Given: the same symlinked layout with no payload anywhere: no env override, no project copy, no global copy,
  //   and no dev-layout copy (the port sits outside the repo).
  // When: the factory runs; then tool.execute.before for bash `git status`, and for bash `ls`.
  // Then: git commands are blocked with an actionable message, and nothing else is.
  // Verification items:
  //   - the factory does not throw (IP-1: init never throws)
  //   - `git status` throws a message that contains every searched candidate: <directory>/.opencode/
  //     llm-agent-workflow/commit-guard, the worktree candidate when it differs, <XDG_CONFIG_HOME>/opencode/
  //     llm-agent-workflow/commit-guard, the dev-layout candidate, and "./setup-opencode.sh --global --plugin
  //     commit-guard"
  //   - `ls` does not throw
  //   - the warning is logged once per process: not for `ls`, once for the first git call, never again (a second
  //     plugin instance from the same module included)
  // Pass criteria: all four checks hold.
  // ROI: 45 (BV:9 x Freq:4 + Legal:0 + Defect:9)
  // @category: edge-case
  // @dependency: opencode-commit-guard.ts, payload resolver
  // @real-dependency: filesystem (symlinks)
  // @complexity: medium
  test("AC-004: without a payload, a git call throws listing every searched path and setup-opencode.sh", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const worktree = join(scope.root, "worktree")
    mkdirSync(worktree)
    const missing = missingMessage([
      join(scope.projectDir, ".opencode", PAYLOAD),
      join(worktree, ".opencode", PAYLOAD),
      join(scope.configHome, "opencode", PAYLOAD),
      join(scope.root, "dotfiles"),
    ])

    await inScope(scope, {}, async () => {
      const module = await importPort(installed)
      const guard = await startGuard(module, { directory: scope.projectDir, worktree })

      await expect(guard.bash("ls")).resolves.toBeUndefined()
      expect(guard.logs).toEqual([])
      expect((await rejection(guard.bash("git status"))).message).toBe(missing)
      expect((await rejection(guard.bash("git status"))).message).toBe(missing)
      const second = await startGuard(module, { directory: scope.projectDir, worktree })
      expect((await rejection(second.bash("git status"))).message).toBe(missing)

      expect(guard.logs).toEqual([warnLog(missing)])
      expect(second.logs).toEqual([])
    })
  })

  // AC-006 (D4, [bun]); EARS While + Given
  // AC: "While LLM_AGENT_WORKFLOW_PAYLOAD_ROOT is set, only <env>/<id> shall be searched. Given payloads in both
  //   <directory>/.opencode/... and the global root, the project one shall win."
  // Given (two cases, fresh scope each):
  //   a. LLM_AGENT_WORKFLOW_PAYLOAD_ROOT=<tmp>/override, which has no commit-guard/ dir, with valid payloads in
  //      both the project and the global root.
  //   b. The env var unset; the real hook in <directory>/.opencode/llm-agent-workflow/commit-guard, and in the
  //      global root a hook copy that exits 1 for every command (the losing candidate only).
  // When: tool.execute.before for bash `git status`.
  // Then: the env override is exclusive, and the project candidate beats the global one.
  // Verification items:
  //   - a: the call throws the payload-missing message, and its searched list is exactly ["<tmp>/override/
  //     commit-guard"]
  //   - b: `git status` resolves, which proves the project copy ran and not the global one; with the project copy
  //     removed, a new instance runs the global copy and fails closed with its exit 1
  // Pass criteria: both cases behave as above.
  // ROI: 34 (BV:7 x Freq:3 + Legal:0 + Defect:7) | the Design Doc marks project-before-global "worth a look"
  // @category: edge-case
  // @dependency: opencode-commit-guard.ts, payload resolver, commit_guard_hook.py
  // @real-dependency: commit_guard_hook.py (the winning candidate), filesystem
  // @complexity: medium
  test("AC-006: the env root is exclusive, and a project payload wins over the global one", async () => {
    const exclusive = newScope()
    const exclusivePort = copyPort(exclusive, PORT)
    installPayload(exclusive, PLUGIN_ID, [HOOKS])
    placeHooks(projectPayload(exclusive))
    const override = join(exclusive.root, "override")
    mkdirSync(override)

    await inScope(exclusive, { LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: override }, async () => {
      const guard = await startGuard(await importPort(exclusivePort), projectRoots(exclusive))

      expect((await rejection(guard.bash("git status"))).message).toBe(missingMessage([join(override, PLUGIN_ID)]))
    })

    const layered = newScope()
    const layeredPort = copyPort(layered, PORT)
    installPayload(layered, PLUGIN_ID, [HOOKS])
    const failingHook = 'import sys\nsys.stderr.write("global copy ran\\n")\nsys.exit(1)\n'
    writeFileSync(join(globalPayload(layered), HOOK), failingHook)
    placeHooks(projectPayload(layered))

    await inScope(layered, {}, async () => {
      const guard = await startGuard(await importPort(layeredPort), projectRoots(layered))

      await expect(guard.bash("git status")).resolves.toBeUndefined()

      rmSync(projectPayload(layered), { recursive: true })
      const fallback = await startGuard(await importPort(layeredPort), projectRoots(layered))

      expect((await rejection(fallback.bash("git status"))).message).toBe(
        "[commit-guard] classifier exited 1. Blocking to be safe.\nglobal copy ran",
      )
    })
  })
})

describe("opencode-commit-guard: payload candidates (F1)", () => {
  test("the port in the repo finds plugin-commit-guard/hooks through the dev-layout candidate", async () => {
    const scope = newScope()

    await inScope(scope, {}, async () => {
      const guard = await startGuard(await importPort(join(REPO_ROOT, PORT)), projectRoots(scope))

      await expect(guard.bash("git status")).resolves.toBeUndefined()
      expect((await rejection(guard.bash('git commit -m "x"'))).message.startsWith(DELEGATED)).toBe(true)
    })
  })

  test("the project candidate comes from PluginInput.directory, not the process cwd", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const directory = join(scope.root, "opencode-directory")
    placeHooks(join(directory, ".opencode", PAYLOAD))

    await inScope(scope, {}, async () => {
      const guard = await startGuard(await importPort(installed), { directory, worktree: scope.projectDir })

      expect(process.cwd()).toBe(scope.projectDir)
      await expect(guard.bash("git status")).resolves.toBeUndefined()
    })
  })

  test("a candidate dir without the marker file is skipped for the next candidate", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [HOOKS])
    mkdirSync(join(projectPayload(scope), "hooks"), { recursive: true })

    await inScope(scope, {}, async () => {
      const guard = await startGuard(await importPort(installed), projectRoots(scope))

      await expect(guard.bash("git status")).resolves.toBeUndefined()
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
      const guard = await startGuard(await importPort(installed), roots(scope))

      expect((await rejection(guard.bash("git status"))).message).toBe(missingMessage([
        join(scope.projectDir, ".opencode", PAYLOAD),
        join(scope.configHome, "opencode", PAYLOAD),
        join(scope.root, "dotfiles"),
      ]))
    })
  })

  test("without XDG_CONFIG_HOME the candidates still resolve, and the project payload is used", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    placeHooks(projectPayload(scope))

    await inScope(scope, { XDG_CONFIG_HOME: undefined }, async () => {
      const guard = await startGuard(await importPort(installed), projectRoots(scope))

      await expect(guard.bash("git status")).resolves.toBeUndefined()
    })
  })

  test("a resolver failure leaves the factory working and blocks every git call", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [HOOKS])

    await inScope(scope, {}, async () => {
      const guard = await startGuard(await importPort(installed), { directory: 42, worktree: scope.projectDir })

      await expect(guard.bash("ls")).resolves.toBeUndefined()
      const blocked = await rejection(guard.bash("git status"))

      expect(blocked.message).toStartWith("[commit-guard] payload resolution failed (TypeError")
      expect(blocked.message).toEndWith(`). ${MISSING_TAIL}`)
      expect(guard.logs).toEqual([warnLog(blocked.message)])
    })
  })
})

describe("opencode-commit-guard: fail closed around the classifier", () => {
  // The fast path must see exactly the hook's git words: a word next to `-` or a word character is not git, while a
  // path prefix and the `.exe` suffix (WSL interop) still are.
  test("only bash calls that mention git are blocked when the payload is missing", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const missing = missingMessage([
      join(scope.projectDir, ".opencode", PAYLOAD),
      join(scope.configHome, "opencode", PAYLOAD),
      join(scope.root, "dotfiles"),
    ])

    await inScope(scope, {}, async () => {
      const guard = await startGuard(await importPort(installed), projectRoots(scope))

      await expect(guard.tool("read", { command: "git status", filePath: "/etc/hostname" })).resolves.toBeUndefined()
      await expect(guard.bash("legit --version")).resolves.toBeUndefined()
      await expect(guard.bash("foo-git --version")).resolves.toBeUndefined()
      await expect(guard.bash("git-lfs version")).resolves.toBeUndefined()
      await expect(guard.bash("gitk --all")).resolves.toBeUndefined()
      expect(guard.logs).toEqual([])

      expect((await rejection(guard.bash("/usr/bin/git status"))).message).toBe(missing)
      expect((await rejection(guard.bash("git.exe status"))).message).toBe(missing)
      expect((await rejection(guard.bash("./git status"))).message).toBe(missing)
    })
  })

  test("a path-qualified git reaches the classifier and is delegated", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [HOOKS])

    await inScope(scope, {}, async () => {
      const guard = await startGuard(await importPort(installed), projectRoots(scope))
      const delegated = await rejection(guard.bash('/usr/bin/git commit -m "x"'))

      expect(delegated.message.startsWith(DELEGATED)).toBe(true)
      expect(delegated.message).toContain('\n  /usr/bin/git commit -m "x"\n')
    })
  })

  test("a hook removed after start blocks git with the classifier-not-found message", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    const payload = installPayload(scope, PLUGIN_ID, [HOOKS])

    await inScope(scope, {}, async () => {
      const guard = await startGuard(await importPort(installed), projectRoots(scope))
      rmSync(join(payload, HOOK))

      expect((await rejection(guard.bash("git status"))).message).toBe(
        `[commit-guard] classifier not found at ${join(payload, HOOK)}. ${HAND_OVER}`,
      )
    })
  })

  test("a classifier that cannot start blocks git", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [HOOKS])
    const python = join(scope.root, "no-such-python3")

    await inScope(scope, { COMMIT_GUARD_PYTHON: python }, async () => {
      const guard = await startGuard(await importPort(installed), projectRoots(scope))

      // Only git bash calls reach the classifier, so these pass even though it cannot run.
      await expect(guard.bash("ls")).resolves.toBeUndefined()
      await expect(guard.tool("read", { command: 'git commit -m "x"', filePath: "/etc/hostname" })).resolves
        .toBeUndefined()
      const blocked = await rejection(guard.bash("git status"))

      expect(blocked.message).toStartWith(`[commit-guard] could not run the classifier (${python}): `)
      expect(blocked.message).toContain("ENOENT")
      expect(blocked.message).toEndWith(`. ${HAND_OVER}`)
    })
  })

  test("a classifier killed by a signal blocks git", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [HOOKS])
    const python = writeExecutable(join(scope.root, "killed-python3"), "#!/bin/sh\nkill -KILL $$\n")

    await inScope(scope, { COMMIT_GUARD_PYTHON: python }, async () => {
      const guard = await startGuard(await importPort(installed), projectRoots(scope))

      expect((await rejection(guard.bash("git status"))).message).toBe(
        `[commit-guard] could not run the classifier (${python}): no exit status. ${HAND_OVER}`,
      )
    })
  })

  test("a classifier exit other than 0 or 2 blocks git with its stderr", async () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [HOOKS])
    const stub = "#!/bin/sh\necho 'classifier broke' >&2\nexit 3\n"
    const python = writeExecutable(join(scope.root, "exit3-python3"), stub)

    await inScope(scope, { COMMIT_GUARD_PYTHON: python }, async () => {
      const guard = await startGuard(await importPort(installed), projectRoots(scope))

      await expect(guard.bash("ls")).resolves.toBeUndefined()
      expect((await rejection(guard.bash("git status"))).message).toBe(
        "[commit-guard] classifier exited 3. Blocking to be safe.\nclassifier broke",
      )
    })
  })
})

// Imports the port given as argv[2], starts it on directory = worktree = argv[3], runs one bash tool call with argv[4]
// as the command, and prints {allowed, message} as JSON.
const CHILD_PROBE = [
  "const [port, directory, command] = process.argv.slice(2)",
  "const client = { app: { log: async () => ({ data: true }) } }",
  "const hooks = await (await import(port)).opencodeCommitGuard({ client, directory, worktree: directory })",
  "try {",
  '  await hooks["tool.execute.before"]({ tool: "bash", sessionID: "s", callID: "c" }, { args: { command } })',
  '  console.log(JSON.stringify({ allowed: true, message: "" }))',
  "} catch (error) {",
  "  console.log(JSON.stringify({ allowed: false, message: (error as Error).message }))",
  "}",
  "",
].join("\n")

describe("opencode-commit-guard: in-repo delegation in a child bun whose HOME is the scope's", () => {
  // The token, the ledger and the watcher's result file all hang off the port's config dir, which follows the cached
  // os.homedir(). Only a child process can move that, so the child runs with HOME=<scope>/home and a git repo as its
  // cwd, and nothing it writes can land outside the scope.
  function runChild(scope: Scope, installed: string, command: string): { allowed: boolean; message: string } {
    const probe = join(scope.root, "probe.ts")
    writeFileSync(probe, CHILD_PROBE)
    const env = childEnv({
      HOME: scope.home,
      XDG_CONFIG_HOME: scope.configHome,
      XDG_CACHE_HOME: join(scope.root, "cache"),
      BUN_INSTALL_CACHE_DIR: join(scope.root, "cache", "bun"),
      TMPDIR: scope.root,
      COMMIT_GUARD_MODE: "delegate",
      GIT_CONFIG_NOSYSTEM: "1",
      GIT_CONFIG_GLOBAL: scope.gitconfig,
      GIT_CEILING_DIRECTORIES: scope.root,
    })
    const result = spawnSync(process.execPath, [probe, installed, scope.projectDir, command], {
      cwd: scope.projectDir, env, encoding: "utf8", timeout: 60_000,
    })
    expect({ status: result.status, stderr: result.stderr }).toEqual({ status: 0, stderr: "" })
    return JSON.parse(result.stdout.trim()) as { allowed: boolean; message: string }
  }

  test("a one-time token in the port's config dir is spent, and a delegation arms its result file there", () => {
    const scope = newScope()
    const installed = copyPort(scope, PORT)
    installPayload(scope, PLUGIN_ID, [HOOKS])
    scopeGit(scope, ["init", "-q", scope.projectDir])
    const configDir = join(scope.home, ".config", "opencode")
    const tokenFile = join(configDir, ".commit-guard-token")
    const stateDir = join(configDir, ".commit-guard")
    const approved = 'git commit -m "y"'
    mkdirSync(configDir, { recursive: true })
    const token = createHash("sha256").update(`${approved}\0${join(scope.projectDir, ".git")}`).digest("hex")
    writeFileSync(tokenFile, token)

    const spent = runChild(scope, installed, approved)
    const delegated = runChild(scope, installed, 'git commit -m "x"')

    expect(spent).toEqual({ allowed: true, message: "" })
    expect(existsSync(tokenFile)).toBe(false)
    expect(delegated.allowed).toBe(false)
    expect(delegated.message.startsWith(DELEGATED)).toBe(true)
    expect(delegated.message).toContain(` --result-file ${join(stateDir, "result-")}`)
    expect(readdirSync(join(stateDir, "ledger")).length).toBe(1)
    expect(existsSync(join(scope.home, ".claude"))).toBe(false)
  }, 60_000)
})

// Outside the "opencode-commit-guard: " describes on purpose: the child run below selects exactly those, so it never
// runs this test again.
describe("commit-guard harness: an inherited git environment", () => {
  test("changes no result and writes nothing under the HOME bun started with", () => {
    const scope = newScope()
    const leaked = join(scope.root, "leaked-repo")
    scopeGit(scope, ["init", "-q", leaked])
    const home = join(scope.root, "child-home")
    const tmp = join(scope.root, "child-tmp")
    mkdirSync(home)
    mkdirSync(tmp)
    const env = childEnv({
      HOME: home,
      XDG_CONFIG_HOME: join(home, ".config"),
      XDG_CACHE_HOME: join(scope.root, "child-cache"),
      BUN_INSTALL_CACHE_DIR: join(scope.root, "child-cache", "bun"),
      TMPDIR: tmp,
      PYTHONDONTWRITEBYTECODE: "1",
      // Repo discovery past GIT_CEILING_DIRECTORIES, and config injection that turns `git status` into a commit.
      GIT_DIR: join(leaked, ".git"),
      GIT_CONFIG_PARAMETERS: "'alias.status=commit -m leaked'",
    })

    const argv = ["test", import.meta.path, "--test-name-pattern", "^opencode-commit-guard: "]
    const result = spawnSync(process.execPath, argv, { cwd: REPO_ROOT, env, encoding: "utf8", timeout: 120_000 })
    const output = `${result.stdout}${result.stderr}`

    expect({ status: result.status, failed: output.split("\n").filter((line) => line.startsWith("(fail)")) })
      .toEqual({ status: 0, failed: [] })
    expect(output).toMatch(/^ 0 fail$/m)
    expect(output).toMatch(/^ 1 filtered out$/m)
    expect(readdirSync(home, { recursive: true })).toEqual([])
  }, 120_000)
})
