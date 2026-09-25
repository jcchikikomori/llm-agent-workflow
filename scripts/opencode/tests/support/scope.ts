// Shared harness for the opencode port bun suites (Design Doc "Test Strategy > Conventions").
//
// A temp scope under os.tmpdir(): HOME=<root>/home, XDG_CONFIG_HOME=<root>/config and a project dir, optionally
// with plugins/ as a symlink into <root>/dotfiles/plugins (the live layout, N20). Ports are copied into plugins/ and
// imported by a unique path, so every test gets a fresh module and Bun reports import.meta.url at the realpath
// (N19), as opencode does. Bun built-ins and node: modules only. Keep support/ free of *.test.ts files.

import { copyFileSync, cpSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { basename, join, resolve, sep } from "node:path"

export const REPO_ROOT = resolve(import.meta.dir, "..", "..", "..", "..")

export type ScopeKind = "global" | "project"

export interface Scope {
  kind: ScopeKind
  root: string
  home: string
  configHome: string
  scopeRoot: string
  pluginsDir: string
  // Where copied ports physically live: <root>/dotfiles/plugins when symlinked, else pluginsDir itself.
  dotfilesPlugins: string
  projectDir: string
  gitconfig: string
  cleanup(): void
}

export type EnvVars = Record<string, string | undefined>

export interface FakeClient {
  app: { log(entry: unknown): Promise<{ data: boolean }> }
}

const SCOPE_KINDS: readonly ScopeKind[] = ["global", "project"]

// Same private git config as _support.py, so git run by a port never reads the user's ~/.gitconfig.
const GITCONFIG = [
  "[user]",
  "\tname = Sandbox Test",
  "\temail = sandbox@example.invalid",
  "[init]",
  "\tdefaultBranch = main",
  "[commit]",
  "\tgpgsign = false",
  "[tag]",
  "\tgpgsign = false",
  '[protocol "file"]',
  "\tallow = always",
  "",
].join("\n")

// Build artefacts the installer never ships (AC-020).
const SKIPPED_PAYLOAD_ENTRIES = new Set(["__pycache__", "evals"])

let importCounter = 0

export function makeScope(options: { kind: ScopeKind; symlinkPlugins?: boolean }): Scope {
  const { kind, symlinkPlugins = false } = options
  if (!SCOPE_KINDS.includes(kind)) throw new Error(`makeScope: kind must be global or project, got ${kind}`)
  const root = realpathSync(mkdtempSync(join(tmpdir(), "opencode-scope-")))
  if (root.startsWith(REPO_ROOT + sep)) {
    rmSync(root, { recursive: true, force: true })
    throw new Error(`makeScope: temp root ${root} is inside the repo; set TMPDIR outside ${REPO_ROOT}`)
  }
  const home = join(root, "home")
  const configHome = join(root, "config")
  const projectDir = join(root, "project")
  const scopeRoot = kind === "global" ? join(configHome, "opencode") : join(projectDir, ".opencode")
  const pluginsDir = join(scopeRoot, "plugins")
  const dotfilesPlugins = symlinkPlugins ? join(root, "dotfiles", "plugins") : pluginsDir
  for (const dir of [home, configHome, projectDir, scopeRoot, dotfilesPlugins]) mkdirSync(dir, { recursive: true })
  if (symlinkPlugins) symlinkSync(dotfilesPlugins, pluginsDir, "dir")
  const gitconfig = join(root, "gitconfig")
  writeFileSync(gitconfig, GITCONFIG)
  return {
    kind, root, home, configHome, scopeRoot, pluginsDir, dotfilesPlugins, projectDir, gitconfig,
    cleanup: () => rmSync(root, { recursive: true, force: true }),
  }
}

// Env for withEnv() that keeps every path inside the scope. Bun caches os.homedir() at process start (verified on
// 1.3.11), so setting HOME alone does not redirect homedir(); the XDG dirs are therefore set explicitly and code under
// test that honours them never falls back to the real home.
//
// Every inherited GIT_* variable is dropped, as _support.Sandbox.env inherits none, and only the scope's own git
// settings are set. GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE and GIT_COMMON_DIR bypass GIT_CEILING_DIRECTORIES, so a
// hook under test would snapshot (and write state for) a repo outside the scope. GIT_CONFIG, GIT_CONFIG_PARAMETERS
// and GIT_CONFIG_COUNT inject config such as aliases. GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES and
// GIT_NAMESPACE change what objects and refs resolve to, and GIT_TRACE* write to files outside the scope.
export function scopeEnv(scope: Scope, overrides: EnvVars = {}): EnvVars {
  return {
    ...inheritedGitVars(),
    HOME: scope.home,
    XDG_CONFIG_HOME: scope.configHome,
    XDG_CACHE_HOME: join(scope.home, ".cache"),
    XDG_DATA_HOME: join(scope.home, ".local", "share"),
    LLM_AGENT_WORKFLOW_PAYLOAD_ROOT: undefined,
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: scope.gitconfig,
    ...overrides,
  }
}

// Each GIT_* key of the current process env, mapped to undefined so withEnv() removes it.
function inheritedGitVars(): EnvVars {
  const cleared: EnvVars = {}
  for (const key of Object.keys(process.env)) {
    if (key.startsWith("GIT_")) cleared[key] = undefined
  }
  return cleared
}

// Copies each payload dir (absolute, or relative to REPO_ROOT) to <scopeRoot>/llm-agent-workflow/<pluginId>/<name>.
export function installPayload(scope: Scope, pluginId: string, sourceDirs: string[]): string {
  const dest = join(scope.scopeRoot, "llm-agent-workflow", pluginId)
  mkdirSync(dest, { recursive: true })
  for (const source of sourceDirs) {
    const from = resolve(REPO_ROOT, source)
    cpSync(from, join(dest, basename(from)), {
      recursive: true,
      filter: (src) => !SKIPPED_PAYLOAD_ENTRIES.has(basename(src)) && !src.endsWith(".pyc"),
    })
  }
  return dest
}

// Copies a port (absolute, or relative to REPO_ROOT) into pluginsDir; returns the path through pluginsDir.
export function copyPort(scope: Scope, portPath: string): string {
  const from = resolve(REPO_ROOT, portPath)
  const installed = join(scope.pluginsDir, basename(from))
  copyFileSync(from, installed)
  return installed
}

// Imports a fresh module instance. The query goes on the raw path: on Bun 1.3.11 a file:// URL loses its query in
// resolution, so two imports of the same URL share one instance.
export async function importPort(path: string): Promise<Record<string, unknown>> {
  importCounter += 1
  return (await import(`${resolve(path)}?t=${importCounter}`)) as Record<string, unknown>
}

export function fakeClient(): { client: FakeClient; logs: unknown[] } {
  const logs: unknown[] = []
  const client: FakeClient = {
    app: {
      log: async (entry: unknown) => {
        logs.push(entry)
        return { data: true }
      },
    },
  }
  return { client, logs }
}

// Sets (or, for undefined, deletes) env vars around fn and restores the previous values afterwards, even on throw.
export async function withEnv<T>(vars: EnvVars, fn: () => T | Promise<T>): Promise<T> {
  const saved: EnvVars = {}
  for (const [key, value] of Object.entries(vars)) {
    saved[key] = process.env[key]
    setEnv(key, value)
  }
  try {
    return await fn()
  } finally {
    for (const [key, value] of Object.entries(saved)) setEnv(key, value)
  }
}

function setEnv(key: string, value: string | undefined): void {
  if (value === undefined) delete process.env[key]
  else process.env[key] = value
}
