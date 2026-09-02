# Plugin port — a Claude Code plugin → an opencode plugin

Porting spec for plugin authors. Return to `SKILL.md` for the phase flow and the write gate.

Every pattern below is derived from seven completed ports in `jcchikikomori/llm-agent-workflow` — `commit-guard`, `env-guard`, `claude-attribution`, `markdown-format`, `token-saver`, `memory-guard`, `wandavision`. Citations are `file:line` in that repo. When something here disagrees with an existing port, the port wins: it runs, this file does not.

## Contents

- [File-shape contract](#file-shape-contract)
- [Hook event mapping](#hook-event-mapping)
- [Where tool args live](#where-tool-args-live)
- [Module skeleton](#module-skeleton)
- [Translation idioms](#translation-idioms)
- [No equivalent](#no-equivalent)
- [Companion files](#companion-files)
- [Worked example](#worked-example)
- [Verification checklist](#verification-checklist)

## File-shape contract

| Claude Code | opencode | How it is found |
| --- | --- | --- |
| `.claude-plugin/plugin.json` | `package.json` at the plugin root | npm manifest; not auto-discovered by Claude Code |
| `hooks/hooks.json` + `hooks/*.py` | `plugins/opencode-<name>.ts` | `setup-opencode.sh:90-93` globs `plugins/*.ts` |
| `commands/*.md` | the same file, reused verbatim | `setup-opencode.sh:96-99` globs `commands/*.md` — no prefix |
| `agents/<name>.md` | `agents/opencode-<name>.md` | `setup-opencode.sh:102-105` globs `agents/opencode-*.md` |
| `skills/<name>/SKILL.md` | not ported | opencode reads `.claude/skills/<name>/SKILL.md` natively (`README.md:119`) |

Install targets: `~/.config/opencode/{plugins,commands,agents}/` for global, `<project>/.opencode/...` for project scope (`setup-opencode.sh:201-204`).

Commands are shared rather than duplicated because their frontmatter is a single `description` field, which both runtimes accept. That is worth preserving — a forked command file drifts within a week.

## Hook event mapping

| Claude Code event (matcher) | opencode hook | Proof | Semantic difference |
| --- | --- | --- | --- |
| `PreToolUse` (`Bash`) | `tool.execute.before` | `plugin-commit-guard/hooks/hooks.json:4-6` → `opencode-commit-guard.ts:54-55` | Blocks by `throw` (`:83`), not `exit(2)` |
| `PreToolUse` (`Read\|Edit\|Write\|MultiEdit\|Bash`) | `tool.execute.before` | `plugin-env-guard/hooks/hooks.json:6` → `opencode-env-guard.ts:151` (file branch), `:176` (bash branch) | There is no `MultiEdit` tool; `read`/`write`/`edit` cover all four (`opencode-env-guard.ts:17-18`) |
| `PreToolUse` (`mcp__.*\|Bash`) | `tool.execute.before` | `plugin-attribution/hooks/hooks.json:6` → `opencode-claude-attribution.ts:136`, `:162` | MCP tool names keep the `mcp__` prefix; the config matcher becomes an in-code `startsWith` test |
| `PostToolUse` (`Write\|Edit\|MultiEdit`) | `tool.execute.after` | `plugin-markdown-format/hooks/hooks.json:5-6` → `opencode-markdown-format.ts:147-149` | Cannot block; the matcher becomes a regex constant (`FILE_TOOL_RE`, `:40`) |
| `PostToolUse` (`Write\|Edit\|MultiEdit`) | `event` → `file.edited` | `plugin-memory-guard/hooks/hooks.json:16` → `opencode-memory-guard.ts:391-407` | Fires for any file write by any means; no tool args available (`:11-12`) |
| `PostToolUse` (matcher `""`) | `tool.execute.after` | `plugin-token-saver/hooks/hooks.json:4-5` → `opencode-token-saver.ts:317` | Empty matcher means no tool filter at all |
| `PostToolUse` (tool-name regex) | `tool.execute.after` | `plugin-wandavision/hooks/hooks.json:6` → `opencode-wandavision.ts:97`, `:104` | Regex ported literally (`SCREENSHOT_TOOL_RE`, `:23`); the port adds a `webfetch` case (`:110`) with no Claude Code counterpart |
| `SessionStart` | `event` → `session.created` flag, consumed by `experimental.chat.system.transform` | `plugin-memory-guard/hooks/hooks.json:4` → `opencode-memory-guard.ts:386-389`, `:410-425`; `plugin-token-saver/hooks/hooks.json:15` → `opencode-token-saver.ts:285-288`, `:341-353` | `session.created` cannot inject context; only `system.transform` can `output.system.push(...)` |
| `UserPromptSubmit` | `chat.message` + `experimental.chat.system.transform` | `plugin-token-saver/hooks/hooks.json:26` → `opencode-token-saver.ts:292-314`, `:356-366` | **Not blockable.** Gap documented at `opencode-token-saver.ts:20-35` |
| *(none — added during porting)* | `chat.params` | `opencode-claude-attribution.ts:120-127` | Per-session model detection, enabling provider-specific gating (`:131`) |

One genuine fork in the evidence: two ports mapped `PostToolUse (Write|Edit|MultiEdit)` differently, and both are right for their case.

> Use `tool.execute.after` when you need the tool name, its args, or the output title. Use `event` / `file.edited` when all you need is "a watched file changed, by any means" (`opencode-memory-guard.ts:11-12`).

The second is broader: it catches writes that never went through a tool call, which is exactly what a watcher wants and exactly what a formatter does not.

## Where tool args live

Easy to get backwards, and the failure is silent — you read `undefined` and the hook does nothing.

| Hook | Args | Tool name | Extras |
| --- | --- | --- | --- |
| `tool.execute.before` | **second** param: `output.args` (`opencode-commit-guard.ts:57-58`, `opencode-env-guard.ts:148`, `opencode-claude-attribution.ts:133`) | `input.tool`, or destructured `{ tool }` | — |
| `tool.execute.after` | **first** param: `input.args` (`opencode-markdown-format.ts:151`, `opencode-wandavision.ts:99`) | `input.tool` | `output.title` (`opencode-markdown-format.ts:152`) |

`input.sessionID` is available in both (`opencode-token-saver.ts:318`).

## Module skeleton

Fixed elements, each verified across all seven ports:

- **Import** — `import type { Plugin } from "@opencode-ai/plugin"` (`opencode-commit-guard.ts:4`, `opencode-env-guard.ts:1`, and five more). Node builtins use the `node:` prefix.
- **Export** — always a named `const`, typed `: Plugin`, `async`. **No `export default` anywhere in the repo.** Existing names are inconsistent (`opencodeCommitGuard`, `EnvGuardPlugin`, `Wandavision`); for new ports use PascalCase with a `Plugin` suffix, which is what four of seven do.
- **Destructured args** — only four are ever used: `client` (all), `directory` (`opencode-token-saver.ts:236`), `worktree` (`opencode-wandavision.ts:85`), and `$` for shell access (`opencode-memory-guard.ts:296`, used as `` await $`git -C ${cwd} rev-parse --show-toplevel`.cwd(cwd).quiet().nothrow() `` at `:323-326`).
- **Logging** — exactly one shape: `client.app.log({ body: { service, level, message } })` (`opencode-commit-guard.ts:66-72`), with `service` set to the plugin's short name and an optional `extra` object (`opencode-token-saver.ts:256`). Three of seven wrap it in a local `log()` closure with try/catch so logging can never break a hook (`opencode-markdown-format.ts:95-104`).
- **Blocking** — `throw new Error(MESSAGE)`, only from `tool.execute.before`. Reuse the Python `BLOCKED_MESSAGE` text verbatim so the two runtimes say the same thing.
- **Never throw** from `tool.execute.after`, `event`, or `system.transform`. Every observer port wraps its body and swallows (`opencode-markdown-format.ts:159-166`, `opencode-memory-guard.ts:404-406`).
- **Plugin root** — `path.resolve(import.meta.dir, "..")` (`opencode-token-saver.ts:48`), with a Node-safe fallback where portability matters (`opencode-markdown-format.ts:31-34`).

```ts
/**
 * <plugin> — OpenCode port of hooks/<name>_hook.py
 *
 * Claude Code event → OpenCode event mapping:
 *   PreToolUse(Bash)  → tool.execute.before  (throw to block; was sys.exit(2))
 *   PostToolUse(Write) → tool.execute.after  (observe only; cannot block)
 */
import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

const STATE_DIR = join(homedir(), ".config", "opencode", ".<plugin>")
const BLOCKED_MESSAGE = "…"   // kept byte-identical to the Python constant

export const MyThingPlugin: Plugin = async ({ client }) => {
  const log = async (level: string, message: string) => {
    try { await client.app.log({ body: { service: "my-thing", level, message } }) } catch {}
  }

  return {
    "tool.execute.before": async ({ tool }, output) => {
      if (tool !== "bash") return
      const command: string = typeof output?.args?.command === "string" ? output.args.command : ""
      if (shouldBlock(command)) throw new Error(BLOCKED_MESSAGE)
    },

    "tool.execute.after": async (input, output) => {
      try { await handle(input, output) } catch (err) { await log("error", String(err)) }
    },
  }
}
```

## Translation idioms

### 1. `~/.claude/*` → `~/.config/opencode/*`

State, token, and config paths remap wholesale:

- `Path.home()/".claude"/".commit-guard-token"` (`plugin-commit-guard/hooks/commit_guard_hook.py:22`) → `join(homedir(), ".config", "opencode", ".commit-guard-token")` (`opencode-commit-guard.ts:19`)
- `~/.claude/.token-saver` → `opencode-token-saver.ts:51`; `~/.claude/.memory-guard` → `opencode-memory-guard.ts:47`

Remap the **message text too**, not only the constant. `opencode-commit-guard.ts:42` rewrote the inline `python3 -c` one-liner it prints for the user so the path it tells them to write matches the path it reads. A port that updates the constant and leaves the instructions pointing at `~/.claude` produces a token nobody can write.

The line to draw: remap a path the plugin itself reads or writes, and leave a path that belongs to the *other* runtime. env-guard's advice text still names `.claude/settings.json` (`opencode-env-guard.ts:105`) because it is telling the user where Claude Code keeps its allow-list — rewriting that would make the advice wrong. Ask "does the plugin touch this path, or is it just talking about it?" before changing a string.

### 2. State-file interop

When both runtimes may run on one machine, keep one on-disk format:

- Write the same JSON payload the Python side writes, even where the TypeScript only reads mtime (`opencode-token-saver.ts:329-331`; "identical to the Python side", `opencode-memory-guard.ts:141-143`).
- The Python scripts pick up the opencode path at import time: `if (Path.home() / ".config" / "opencode").exists(): _mgc.STATE_DIR = …` (`plugin-memory-guard/scripts/apply_action.py:35-36`, and the three sibling scripts). Accuracy note: only `scripts/*.py` carry that override — the shared `hooks/memory_guard_common.py:32` still hardcodes the Claude Code path, which is correct, because the Claude Code hooks should keep using it.
- Replace Python's `flock` with a promise chain (`withStateLock`, `opencode-memory-guard.ts:144-150`). Parallel tool calls within one turn interleave in a way sequential hook processes never did.

### 3. `CLAUDE.md` → `AGENTS.md`, and why the literal is concatenated

Both doc-aware ports declare the pair as constants:

```ts
const CLAUDE_DOC_NAME = "CLAUDE" + ".md"
const OPENCODE_DOC_NAME = "AGENTS.md"
```

`opencode-memory-guard.ts:55-56`, `opencode-token-saver.ts:67-68`. The reason is in the source (`opencode-memory-guard.ts:52-54`): the config still lists the legacy filename, so the literal is built by concatenation "so it never appears verbatim here". Written out in full, the string would sit inside a file under a watched path and make the plugin trip its own watcher. Concatenation keeps the runtime value and removes the token from the text.

Two consumption patterns, both legitimate:

- **Transform on load** — `files.map(f => f === CLAUDE_DOC_NAME ? OPENCODE_DOC_NAME : f)` (`opencode-memory-guard.ts:86`), so `AGENTS.md` replaces the old name in the watched set.
- **Try both, first hit wins** — iterate `[CLAUDE_DOC_NAME, OPENCODE_DOC_NAME]` and parameterise the message on which was found (`opencode-token-saver.ts:265-279`, `:108-118`).

### 4. `SessionStart` emulation

Three parts, identical in both ports that need it:

1. Closure state: `let sessionFresh = false` plus `currentSessionID` (`opencode-memory-guard.ts:297-298`, `opencode-token-saver.ts:240-241`).
2. `event` handler sets the flag on `session.created` (`opencode-memory-guard.ts:385-390`).
3. `experimental.chat.system.transform` clears the flag *before* doing the work and calls `output.system.push(text)` (`opencode-memory-guard.ts:410-425`, `opencode-token-saver.ts:337-353`).

Clearing first is what makes it once-per-session. `output.system.push(...)` is the only context-injection channel available — it replaces Claude Code's stdout-as-context. The same flag-then-drain shape scales to batching: accumulate in an array or Map from `event`, drain in the next `system.transform` (`pendingFlags`, `opencode-memory-guard.ts:305`, `:427-446`).

### 5. Reusing the Python scripts — via instructions, not `spawn`

Worth stating plainly because the intuitive answer is wrong: **no port shells out to Python.** Reuse happens two ways.

- The plugin prints an absolute `python3 <script>` command into the injected instruction and lets the model run it (`opencode-memory-guard.ts:260-261`, `:290`, with `SCRIPTS_DIR` from `:48`). The rationale is at `:14-19` — the Python scripts remain the only code that mutates the working tree, and only the model can make the judgement calls that precede running them. The instruction even forbids hand-rolled git equivalents (`:257-259`).
- Same trick for user setup steps (`opencode-commit-guard.ts:42`).

Actual subprocesses are reserved for external binaries: `spawnSync` for the formatter plus a hand-rolled `which` (`opencode-markdown-format.ts:72-92`, POSIX `command -v` then Windows `where`), and `$` for git, always `.quiet().nothrow()`.

When the logic is pure, port the algorithm rather than the process: `isVague()` is a documented one-to-one rewrite of `prompt_quality.py:is_vague()` (`opencode-token-saver.ts:162-199`), down to using `Array.from(...).length` to match Python's `len()` over code points (`:193-194`).

### 6. Exit codes → control flow

| Python | TypeScript |
| --- | --- |
| `sys.exit(2)` (block, stderr fed back as context) | `throw new Error(MESSAGE)` from `tool.execute.before` (`opencode-commit-guard.ts:83`) |
| `sys.exit(0)` (allow, silent) | `return` (`opencode-env-guard.ts:14-15`) |
| Always-exit-0 observer hook | Never-throw hook: try/catch and swallow (`opencode-markdown-format.ts:18-20`) |
| stderr diagnostics | `client.app.log` (diagnostics) or `output.system.push` (model-visible) |

Keep message bodies byte-identical where you can — `opencode-token-saver.ts:81` notes it explicitly, and it makes the two implementations diffable.

### 7. Arg-extraction helpers worth copying verbatim

- `const FILE_PATH_ARGS = ["filePath", "file_path", "path", "filepath"]` — identical in `opencode-env-guard.ts:119`, `opencode-markdown-format.ts:43`, `opencode-wandavision.ts:46`. Tool arg names vary; this list absorbs the variation.
- `candidatePaths(args, title)`, which reads those keys and falls back to parsing `output.title` with `/(?:Edited|Wrote|Created|Updated|Patched)\s+(.+)$/` (`opencode-markdown-format.ts:49-66`).
- Regex flag translation: `re.IGNORECASE|re.MULTILINE` → `new RegExp(pattern, "im")` (`opencode-env-guard.ts:139-140`); bare `re.IGNORECASE` → `"i"`.
- Normalise Windows paths before matching: `p.replace(/\\/g, "/")` (`opencode-env-guard.ts:122-124`).

## No equivalent

| Claude Code capability | Status | What the ports do instead |
| --- | --- | --- |
| `UserPromptSubmit` blocking | No equivalent | Detect in `chat.message`, inject guidance via `system.transform`, and prepend a header saying this is guidance rather than a block (`opencode-token-saver.ts:100-104`; rationale `:20-35`) |
| `MultiEdit` tool | Does not exist | `read`/`write`/`edit` cover it (`opencode-env-guard.ts:17-18`) |
| Config-level matchers | No equivalent | In-code tool checks or a regex constant |
| stderr as model context | No equivalent | `client.app.log` for diagnostics, `output.system.push` for the model |
| `${CLAUDE_PLUGIN_ROOT}` | No equivalent | `path.resolve(import.meta.dir, "..")` — **and it breaks after install.** Once the file is copied into `~/.config/opencode/plugins/`, sibling `config/` and `scripts/` directories are gone (`opencode-token-saver.ts:53-56`). Every bundled-config read needs inline defaults (`opencode-token-saver.ts:121-131`, `opencode-memory-guard.ts:92`, `opencode-markdown-format.ts:125`) |
| Image-returning tool inspection | No equivalent | Fire on tool-name match unconditionally and let the skill enforce (`opencode-wandavision.ts:10-14`) |
| Skills | Already native | Do not duplicate (`README.md:119`) |
| Multi-agent orchestration plugins | Not loadable | Left Claude-Code-only (`README.md:168`) |
| MCP registration inside the plugin | No equivalent | The user adds an `opencode.json` `mcp` block; the command file prints it (`plugin-wandavision/commands/wandavision.md:28-43`) |
| Agent auto-registration | Commands auto-load; agents do not | The agent also needs an `opencode.json` `agent` entry (`setup-opencode.sh:316-329`) |

That last row catches people out. Dropping a file into `agents/` is necessary but not sufficient — without the config entry the agent has no permissions and may not be selectable.

## Companion files

### `package.json`

Exactly seven keys, identical across all six that exist:

```json
{
  "name": "commit-guard",
  "version": "0.2.0",
  "description": "Asks for approval before every git commit; preserves GPG signing",
  "type": "module",
  "author": { "name": "…", "url": "…" },
  "homepage": "…",
  "license": "MIT"
}
```

**`name` is the plugin's own name, unprefixed** — `commit-guard`, `env-guard`; never `opencode-env-guard`, never `claude-env-guard`. The six `package.json` files written before this convention settled still carry an `opencode-` prefix; leave them alone until they are touched for another reason, and do not copy the prefix into a new one. Nothing at runtime reads this field (`setup-opencode.sh` globs `plugins/*.ts` and never opens `package.json`), so it is naming hygiene rather than behaviour: the prefix restates the directory it already sits in, and goes stale the day the plugin works in a third runtime.

`"type": "module"` is required. No `dependencies`, `main`, `scripts`, `keywords`, or `repository` — the `@opencode-ai/plugin` import is types-only, so there is nothing to install.

Rewrite the `description` where the Claude Code text is runtime-specific: `memory-guard`'s package.json says "root AGENTS.md" where its plugin.json says the legacy name.

Known gap to avoid repeating: `plugin-env-guard/` and `plugin-markdown-format/` ship `plugins/*.ts` with **no** `package.json`.

### `commands/*.md`

A single frontmatter field, `description`, and nothing else — verified across all five (`plugin-commit-guard/commands/commit-guard.md:1-3` and siblings). No `name`, no `allowed-tools`, no `argument-hint`. That minimalism is why one file serves both runtimes. `$ARGUMENTS` works in the body (`plugin-gh-issue-to-pr/commands/gh-issue-to-pr.md:9`).

### `agents/opencode-<name>.md`

| Claude Code agent | opencode agent |
| --- | --- |
| `name` | dropped — the filename is the identity |
| `description` | kept, but edited |
| `tools: Read, Grep, Bash, …` | replaced by a `permission` map (`edit`, `bash`, `webfetch`) |
| `model: inherit` | dropped |
| — | `mode: subagent` (required) |

Compare `plugin-gh-issue-to-pr/agents/opencode-gh-issue-to-pr.md:1-8` against `agents/gh-issue-to-pr.md:1-6`.

Body edits are mandatory, not cosmetic: the port drops the legacy doc filename from its precedence list (line 12 vs 10) and from the `description`, and rewrites "a CLAUDE.md-documented base branch" to name `AGENTS.md` (line 20 vs 18). Instructions that reference a document the runtime never reads are worse than no instructions.

### What `plugin.json` does not track

Verified across all ten manifests: only `name`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`, plus optional `skills` and `agents`. Therefore:

- `hooks/hooks.json` and `commands/` are auto-discovered, never listed.
- `plugins/*.ts`, `package.json`, `scripts/`, and `config/` are invisible to Claude Code entirely — `setup-opencode.sh` finds them by filesystem glob.
- `agents:` lists only the Claude Code agent. `plugin-gh-issue-to-pr/.claude-plugin/plugin.json` deliberately omits `opencode-gh-issue-to-pr.md`. **Never add the opencode agent to `plugin.json`.**
- The two manifests' versions drift on purpose (memory-guard: `0.3.0` in plugin.json, `0.2.0` in package.json). Do not "fix" that; see the versioning scheme in the repo's own `CLAUDE.md`.

## Worked example

`markdown-format` is the smallest complete port.

Before — `plugin-markdown-format/hooks/hooks.json:4-6` plus the Python body:

```text
"PostToolUse": [{ "matcher": "Write|Edit|MultiEdit", "hooks":
  [{ "type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/markdown_format_hook.py" }] }]

# file_path = data["tool_input"]["file_path"]
# if not file_path.endswith(".md"): sys.exit(0)
# cmd = [shutil.which("markdownlint-cli2"), "--fix", "--config", <plugin_root>/config/..., file_path]
# if result.returncode not in (0, 1): print(..., file=sys.stderr)
# sys.exit(0)   # always
```

After — `opencode-markdown-format.ts:147-167`, condensed:

```ts
"tool.execute.after": async (input, output) => {
  if (!FILE_TOOL_RE.test(input.tool ?? "")) return        // matcher → regex constant
  const paths = candidatePaths(input.args as Record<string, unknown>, output.title ?? "")
  const mdPath = paths.find((p) => p.endsWith(".md"))
  if (!mdPath) return                                     // sys.exit(0) → return
  try { await runFormatter(mdPath) }                       // spawnSync; exit 0 and 1 both fine
  catch (err) { await log("error", String(err)) }          // never throws
}
```

Four substitutions carry the whole port: `${CLAUDE_PLUGIN_ROOT}` → `PLUGIN_ROOT` (`:31-34`), `shutil.which` → `findExecutable` (`:72-92`), stderr → `client.app.log` (`:100`), and every `sys.exit(0)` → `return` with a swallowing catch.

## Verification checklist

Mechanically checkable, in order:

1. `plugins/opencode-<name>.ts` exists and carries the `opencode-` prefix.
2. Exactly one `import type { Plugin } from "@opencode-ai/plugin"`; all Node imports use `node:`.
3. Export is a named `const … : Plugin = async ({ … }) => { return { … } }`; no `export default`; destructured args limited to `client`, `directory`, `worktree`, `$`.
4. The header docblock lists the Claude Code event → opencode event mapping and names the source Python file (`opencode-markdown-format.ts:6-26` is the model).
5. Every hook in `hooks/hooks.json` is either mapped or has an explicit "no equivalent, here is the workaround" paragraph in that docblock.
6. `grep -n "\.claude" plugins/*.ts` — every hit is either a remapped `.config/opencode` path or an intentional literal (env-guard's advice text at `opencode-env-guard.ts:105`, memory-guard's watched directory at `:50`). No `homedir()` state path still points at `~/.claude`.
7. `grep -n 'CLAUDE\.md' plugins/*.ts` returns nothing; the literal exists only as `"CLAUDE" + ".md"`.
8. Every `throw` sits inside `tool.execute.before`. `tool.execute.after`, `event`, and `system.transform` bodies each have a try/catch and no throw.
9. Every bundled-config read has an inline default and a comment about the post-install path break.
10. Every `client.app.log` call uses `{ body: { service, level, message } }` with one consistent `service` string.
11. Shared state files keep the Python JSON schema; if the Python scripts are still invoked, they carry the `~/.config/opencode` `STATE_DIR` override.
12. `package.json` exists at the plugin root with the seven keys, `"type": "module"`, and `name` set to the plugin's own unprefixed name.
13. `commands/*.md` frontmatter has `description` and nothing else.
14. If an agent was ported: the file is `agents/opencode-<name>.md`; frontmatter is `description` + `mode` + `permission`; `name`/`tools`/`model` are gone; the body names `AGENTS.md`; and it is **not** listed in `.claude-plugin/plugin.json`.
15. `./setup-opencode.sh --list` shows the plugin with the expected rows, and `./setup-opencode.sh --dry-run --global --plugin <name>` writes nothing while listing the expected targets.
16. If the plugin needs `opencode.json` entries (an agent or an MCP server), add a post-install hint block to `setup-opencode.sh` and a row to the README compatibility table.
17. Bump the `package.json` version and add a README changelog entry.
