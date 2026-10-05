---
description: Show machine load and per-session footprint; freeze, resume or stop session work
argument-hint: "[status | sessions | freeze <pid>|--others | resume <pid>|--all | stop <pid> | doctor | watchdog start|stop|status]"
allowed-tools: Bash(~/.claude/.resource-guard/bin/resource-guard status:*), Bash(~/.claude/.resource-guard/bin/resource-guard sessions:*), Bash(~/.claude/.resource-guard/bin/resource-guard doctor:*)
---

# resource-guard

Run the resource-guard CLI with the Bash tool through its stable link,
`~/.claude/.resource-guard/bin/resource-guard` (SessionStart keeps it pointing at
`${CLAUDE_PLUGIN_ROOT}/scripts/resource_guard.py`). If the link is missing, run
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/resource_guard.py"` instead; that call goes
through the normal permission prompt.

- Without arguments: run `~/.claude/.resource-guard/bin/resource-guard status`, then the same with `sessions`.
- With arguments: run `~/.claude/.resource-guard/bin/resource-guard $ARGUMENTS`. Only `status`, `sessions`
  and `doctor` are pre-approved; `freeze`, `resume`, `stop` and `watchdog` go through the normal permission prompt.

Rules:

- Never add `--yes` to `stop` on your own. Run `stop <pid>` without it, show what it would stop, and ask the
  user before re-running with `--yes`: it terminates that session's running commands and stops its containers.
- `freeze` and `resume` are reversible; run them when the user asks.
- Never suggest `wsl.exe --shutdown` as a fix without saying it stops every running distro and session.

Then summarize for the user in a few lines:

1. The load level and the metrics that set it (on WSL, include host available memory and commit).
1. Which sessions use the most memory: baseline (the claude process plus its MCP/LSP servers, which can't be
   frozen) vs work (Bash commands and their containers, which can).
1. What is frozen right now, and whether the watchdog runs in `observe` or `enforce` mode.
1. One concrete next step, for example resuming a session, stopping a forgotten test container, or closing an
   idle session.
