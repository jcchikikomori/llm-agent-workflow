"""
PreToolUse gate: classify a tool call and decide what to do at the current
load level.

- relief work (docker stop/rm/pause, kill, the resource-guard CLI) is never
  held back: it is how load comes down;
- heavy work (subagents, workflows, background commands, Monitor, builds,
  test suites, containers) is gated;
- everything else passes.

The gate never answers `allow`: that would skip the user's own permission
rules. It either stays silent, asks, or denies.
"""

from __future__ import annotations

import functools
import re
import time

import rg_common as common

SPAWN_TOOLS = {"Agent", "Task", "Workflow", "Monitor"}
SHELL_TOOLS = {"Bash", "PowerShell"}

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")


def split_segments(command: str) -> list:
    """Split a shell command on && || ; | and newlines. Quotes are not
    parsed: a separator inside a string only makes a segment shorter, which
    can't hide a heavy command that is there."""
    return [seg.strip() for seg in _SEGMENT_SPLIT.split(command or "") if seg.strip()]


@functools.lru_cache(maxsize=8)
def _compile(patterns: tuple) -> tuple:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            continue
    return tuple(compiled)


def _matches(patterns: list, segment: str) -> bool:
    return any(regex.search(segment) for regex in _compile(tuple(patterns)))


def classify_tool(tool_name: str, tool_input: dict, cfg: dict) -> str:
    """Per segment, relief wins (`pkill -f rspec` names a test runner but
    kills it); across segments, any heavy one makes the call heavy."""
    if tool_name in SPAWN_TOOLS:
        return "heavy"
    if tool_name not in SHELL_TOOLS:
        return "light"
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    relief = heavy = False
    for segment in split_segments(str(tool_input.get("command", ""))):
        if _matches(cfg.get("relief_patterns", []), segment):
            relief = True
        elif _matches(cfg.get("heavy_patterns", []), segment):
            heavy = True
    if heavy:
        return "heavy"
    if relief:
        return "relief"
    return "heavy" if tool_input.get("run_in_background") else "light"


def decide(level: str, is_foreground: bool, kind: str) -> str:
    """'pass', 'soft_wait' (wait for calm, then pass anyway), 'wait_deny'
    (wait for calm, else deny) or 'ask'."""
    if kind != "heavy":
        return "pass"
    idx = common.level_index(level)
    if idx >= common.level_index("critical"):
        return "ask" if is_foreground else "wait_deny"
    if idx == common.level_index("elevated") and not is_foreground:
        return "soft_wait"
    return "pass"


def wait_for_calm(level_fn, wait_seconds: float, poll_seconds: float, below: str,
                  sleep=time.sleep, clock=time.monotonic) -> str:
    """Poll level_fn() until it drops under `below` or the wait runs out.
    Returns the last level seen."""
    deadline = clock() + max(0.0, wait_seconds)
    limit = common.level_index(below)
    level = level_fn()
    while common.level_index(level) >= limit:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(poll_seconds, remaining))
        level = level_fn()
    return level


def record_deny(key: str, now: float, window: float) -> int:
    """Remember one deny for the session; return how many fall in the window."""
    path = common.state_dir() / "denies" / f"{key}.json"
    stamps = common.read_json(path, []) or []
    stamps = [t for t in stamps if isinstance(t, (int, float)) and now - t < window]
    stamps.append(now)
    common.atomic_write_json(path, stamps)
    return len(stamps)


def _describe(level: str, reasons: list) -> str:
    return f"{level}: {', '.join(reasons)}" if reasons else level


def deny_output(tool_name: str, level: str, reasons: list) -> dict:
    load = _describe(level, reasons)
    reason = (
        f"resource-guard: the machine is under heavy load ({load}). New heavy work from this background "
        "session is held back so the session the user is typing in stays responsive. Do not retry in a "
        "loop. Wait for running work to finish, stop background tasks you no longer need (TaskStop), "
        "or tell the user; /resource-guard shows what is using the machine."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": f"resource-guard held back {tool_name} in a background session ({load})",
    }


def ask_output(tool_name: str, level: str, reasons: list) -> dict:
    load = _describe(level, reasons)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"resource-guard: machine load is {load}. This {tool_name} call starts heavy work that "
                "could tip the machine into a hang. Run it anyway?"
            ),
        }
    }


def stop_output(count: int, level: str, reasons: list) -> dict:
    message = (
        f"resource-guard stopped this turn after {count} held-back calls in a row "
        f"(load {_describe(level, reasons)}). Resume when the machine has calmed down."
    )
    return {"continue": False, "stopReason": message, "systemMessage": message}
