---
name: resource-guard
description: This skill should be used when a resource-guard hook denies, holds back or asks to confirm a tool call, stops a turn after repeated held-back calls, says this session's Bash work or containers were frozen or resumed, or reports elevated, critical or hard load; also when the user asks "why is my machine slow", "which session is eating memory", "WSL froze", "should I run wsl --shutdown", "freeze/resume a session", "stop that test container" or "is resource-guard running".
---

# resource-guard

resource-guard keeps several concurrent Claude Code sessions from hanging the machine. It reads memory, swap and
PSI stall (plus Windows host memory and commit on WSL2) and acts by level:

| Level | What the guard does |
| --- | --- |
| ok | nothing |
| elevated | heavy calls from background sessions wait up to ~20 s for calm, then run; occasional advisory |
| critical | background heavy calls wait, then are denied; the foreground gets an `ask` prompt; the watchdog freezes background sessions one at a time |
| hard | same as critical, and the watchdog freezes every background session's running work at once |

Freezing happens only with `freeze_mode: enforce`. The default, `observe`, only logs what it would freeze.

"Heavy" means Agent, Task, Workflow and Monitor calls, background Bash, and commands that build, test, install or
start containers. Relief commands (`docker stop|rm|pause`, `kill`, `pkill`, the resource-guard CLI) pass, but only
when no other segment of the same command is heavy: run relief commands as their own call. The foreground session
is the one the user last typed in; the previous foreground keeps a 60 s grace period.

## On a denied or held-back tool call

1. Do not retry the same call in a loop. After three denies in five minutes the guard ends the turn.
1. Finish what can be finished with light commands (reading files, `git status`, editing).
1. Stop background tasks of this session that are no longer needed (TaskStop), except frozen ones, and stop
   containers this session started and no longer needs.
1. Tell the user in one or two sentences what was held back and why (quote the level and metric from the deny
   reason), and that `/resource-guard` shows what is using the machine.

## On an `ask` prompt

The foreground session gets a confirmation prompt instead of a deny. If the user declines, do not re-issue the call;
continue with light work or ask what to do instead.

## On a "this session's work was frozen" notice

- Frozen processes are stopped with SIGSTOP and their containers are paused. They are not hung or crashed.
- Do not kill, restart or re-run them. A foreground command that appears stuck is frozen and resumes on its own.
- They resume when load drops, or as soon as the user types in this session.
- Work that doesn't depend on the frozen commands can go on.

## On an elevated-load advisory

Prefer the lighter option: one subagent instead of several, a single spec file instead of the whole suite, reuse a
running container instead of starting a new one. Ask before starting anything heavy that the user did not request.

## The CLI

`~/.claude/.resource-guard/bin/resource-guard <command>` works from any terminal. From Windows it needs the
absolute path (`wsl.exe -d <distro> -e /home/<user>/.claude/.resource-guard/bin/resource-guard ...`), because
`wsl.exe -e` starts no shell to expand `~`; `doctor` prints the exact line. Inside the plugin,
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/resource_guard.py" <command>` is the same.

| Command | Effect |
| --- | --- |
| `status` | level, metrics, host memory on WSL, watchdog health, frozen sessions |
| `sessions [--json]` | per session: baseline vs freezable work, owned containers, flags |
| `freeze <pid>` / `freeze --others` | freeze one session's work / every session except the foreground (never this session itself without `--include-self`) |
| `resume <pid>` / `resume --all` | undo a freeze |
| `stop <pid>` | show what it would terminate; add `--yes` (only after the user agrees) to terminate that session's Bash work and stop its containers |
| `doctor` | platform, PSI, docker, shim bypass, `.wslconfig` advice |
| `watchdog start\|stop\|status` | manage the watchdog |

## Never

- Run `stop ... --yes` without the user's explicit go-ahead.
- Resume, unpause or SIGCONT work the guard froze (`resource-guard resume`, `docker unpause`, `kill -CONT`), or run
  `freeze` or `watchdog stop`, unless the user asks.
- Kill, remove or restart a `Paused` container or a frozen process to get unstuck.
- Run `wsl.exe --shutdown`: it stops every distro and every session. Only mention it, with that warning.
- Edit `~/.claude/resource-guard.json` (`enabled`, `gate`, `freeze_mode`, thresholds) or set
  `RESOURCE_GUARD_DISABLE=1` unless the user asks.
- Bypass the docker shim (`command docker`, absolute paths to the real binary): unlabeled containers can't be
  paused for this session.
