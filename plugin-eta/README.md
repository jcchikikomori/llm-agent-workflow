# eta plugin

Draws a live ETA band above the prompt while a tool call runs long. A spinner says that something runs. It doesn't
say whether a test suite is 30 seconds or 10 minutes in. The band does:

```text
▸ Run model specs  1:12 / ~3:06  █████░░░░░░░  hist×4
▸ general-purpose: Audit the hook scripts (bg)  0:48 / ~5:00  ██░░░░░░░░░░  guess
```

It has three parts:

- **A timer.** A `tool.call` hook times every tool call and keeps the median of the last 7 clean runs per command.
- **A hint.** A system-prompt section asks Claude to end the description of a long call with its own estimate, like
  `~3m`.
- **A band.** A `ui.render` hook draws one line per long run above the prompt, background shells, agents and
  workflows included, and raises one alert when a run blows well past its estimate.

This is a **mod**: it runs on Claude Code's function-hook mods API, not on command hooks. `hooks/hooks.json` names a
TypeScript module (`hooks/register.tsx`) that the engine loads and runs in a sandbox of its own, with no Node and no
DOM. Every other plugin in this repo runs shell or Python scripts per event instead.

Claude generated this plugin.

## Install

```bash
/plugin install eta@llm-agent-workflow
/reload-plugins
```

Requirements: Claude Code with the function-hook mods API, which is early access. It was built and tested on
`2.1.295`. Nothing else: no Python, Node or Docker, because the engine runs the module itself.

**Keep the plugin name `eta`.** The folder is `plugin-eta` to match this repo's layout, but the `$.state` refs
(`{ plugin: 'eta', ... }`) and the history in `$.store` are keyed by the plugin name. A rename starts the history
from zero and breaks the state contract.

## What the band shows

One line per run, once the run has lasted 3 s:

```text
▸ <label>  <elapsed> / ~<eta>  <12-cell bar>  <basis>  [+<over> over]
```

- **Label.** The call's description with the `~3m` hint removed, else the Bash command. Agents show
  `<subagent_type>: <description>`, workflows show `workflow <name>`, and MCP tools drop the `mcp__` /
  `mcp__plugin_` prefix. A long label is cut to fit the terminal width.
- **Basis.** `hist×N` when the ETA is the median of N past runs, `guess` when it is Claude's hint.
- **Colour.** Normal while on time, warning once past the ETA, error once past 150% of it. A run with no estimate
  yet shows a dim `<elapsed>  no estimate yet`.
- **`(bg)`** marks a background shell, agent or workflow.
- **At most 4 lines** (fewer when the prompt area is short). The rest collapse into `+N more running`.

What stays off the band:

- Runs shorter than 3 s.
- A subagent's own tool calls. They still feed the history, but the agent's line already covers them.
- `AskUserQuestion`, `EnterPlanMode`, `ExitPlanMode`, `Monitor` and `ScheduleWakeup`. They wait on the person or run
  open-ended, so an ETA means nothing there.
- Everything while a survey is showing: the band steps aside for it.

## How an ETA is chosen

Each call is filed under a **signature**, the key its past durations are stored under:

| Tool | Signature | Key |
| ---- | --------- | --- |
| `Bash` | the command, whitespace-normalised, first 200 characters | strong |
| `Workflow` | its name: the `name` input, else `name:` in the script's meta, else the script file name, else `inline` | strong |
| `Agent` | the subagent type (`general-purpose` when unset) | weak |
| every other tool, MCP tools included | the tool name | weak |

A **weak** key lumps together work of very different sizes. One agent type covers a 20-second lookup and a
10-minute refactor, so the median of past agents says little about the next one. Then, in order:

1. **History, on a strong key with 2+ past runs.** The median of them, shown as `hist×N`. Here history beats Claude's
   hint, because the same command tends to take about the same time again.
1. **Claude's hint**, when the description carries one. Shown as `guess`. On agents and MCP tools this beats any
   history.
1. **Any past runs.** Their median: a lone past run on a strong key, or a weak key's history when Claude gave no
   hint.
1. **Nothing yet.** The line shows elapsed time only.

Worked example, `sleep 20` (no hint):

- **First run:** no history, so `0:05  no estimate yet`. It ends cleanly after 20 s and is filed.
- **Second run:** one sample, so `0:05 / ~0:20  ███░░░░░░░░░  hist×1`.
- **Third run onward:** two or more samples, so history wins even if Claude now adds a hint.

**The hint.** The system-prompt section (`eta:hint`) asks Claude to end the `description` of a Bash, Agent or
Workflow call that will likely run longer than 30 seconds with `~<n>s`, `~<n>m` or `~<n>h`, for example
`Run model specs ~3m`. The parser also reads mixed forms like `~1h30m` and ignores values under 1 s or over 24 h.

**What gets filed.** Only clean runs: not denied, not errored, not interrupted, and at least 2 s long, so quick
commands never crowd out long ones. Each signature keeps its last 7 runs. The store keeps 300 signatures and drops
the least recently used first. A background run is filed when its notification says `completed`, or when a
subagent's turn ends without being aborted.

## Background work

A call that goes to the background stays on the band, marked `(bg)`, until it ends. The mod reads the id the call
answered with: Bash's `backgroundTaskId`, or the `agentId` / `taskId` of an Agent or Workflow call that returned
`async_launched` or `remote_launched`. The line clears on whichever lands first:

- a `<task-notification>` row that names its task id or tool-use id, or
- for a subagent, its `turn.complete` event.

Foreground lines clear when the main turn ends, because no foreground call of that turn can still be running.

## Overrun alert

Once a run passes **150%** of its ETA **and** is at least **15 s** over it, the mod raises one toast (8 s) and one
desktop notification titled `Claude Code: ETA overrun`. It fires once per run.

```text
Run model specs is 1:30 past its ~3:00 estimate. Press Esc to interrupt, or type to steer.
```

A background run says `Ask Claude to stop it, or keep waiting.` instead.

Worked examples of when it fires:

- **ETA `~0:20`:** 150% is 30 s, but that is only 10 s over, so it waits until **35 s**.
- **ETA `~3:00`:** 150% is 4:30, which is 90 s over, so it fires at **4:30**.

## Where state lives

| Where | Key | Holds | Lifetime |
| ----- | --- | ----- | -------- |
| `$.state` | `eta.running` | the runs on the band (contract: `types/index.d.ts`) | the session; survives a hot reload |
| `$.store` | `h:<signature>` | the last 7 durations, in ms | across sessions |
| `$.store` | `idx` | the `h:` keys, least recently used first | across sessions |

## Known limits

- **Durations include time spent waiting on a permission prompt.** The median keeps one slow approval from skewing
  the estimate much.
- **Background shells end by text, not by API.** Detecting the end of a background shell relies on the
  `<task-notification>` row text, which is not typed API. A fallback drops a stale line after max(3× its ETA, 30 min),
  or after 2 h when it has no ETA. A foreground line whose end never arrived leaves after max(3× its ETA, 15 min).
- **The mods API is early access.** Re-run validate and test after every Claude Code update.

## Tests

```bash
claude plugin validate plugin-eta
claude plugin test plugin-eta
```

`tests/eta.test.tsx` holds 28 tests. Most cover the pure logic in `hooks/eta.ts`: hint parsing, the estimate order,
signatures, the band line, the alert and stale rules, and notification parsing. Five run the whole mod in a session
through the test kit (`claude-code/testing`), with a mocked clock and store.

The background path is only tested against a synthetic notification row. A check against a real
`<task-notification>` is still open.

**Type-check.** Once the engine has loaded the mod, it writes the API types to `.claude-plugin/types/` (gitignored),
and `tsc -p` works against them. Before that, use a `tsconfig.json` kept outside the plugin folder, with the options
from the header of the `plugin-authoring` skill's `types/claude-code.d.ts`, and an `include` that names that file plus
`plugin-eta/hooks`, `plugin-eta/types` and `plugin-eta/tests`.

## Changelog

### 0.1.0

Initial release.

- **Timer.** A `tool.call` hook times every call and files clean runs of 2 s or more under a per-command signature,
  keeping the last 7 per signature and 300 signatures (least recently used dropped first) in `$.store`.
- **Estimate.** The median of past runs wins on Bash and Workflow once there are 2+. Claude's `~3m` hint wins on
  agents and MCP tools. A system-prompt section asks Claude for that hint on long calls.
- **Band.** An `AbovePrompt` render hook draws up to 4 lines: label, elapsed / ETA, a 12-cell bar, `hist×N` or
  `guess`, with warning and error colours past 100% and 150%.
- **Background runs.** Shells, agents and workflows stay on the band until their `<task-notification>` row arrives or
  the subagent's `turn.complete` fires. A stale-line fallback covers a lost end.
- **Overrun alert.** One toast and one desktop notification once a run passes 150% of its ETA and is at least 15 s
  over.
