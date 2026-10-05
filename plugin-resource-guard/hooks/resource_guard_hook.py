#!/usr/bin/env python3
"""
resource-guard hook entrypoint for every event the plugin registers.

- SessionStart: put the docker label shim on the Bash tool's PATH, keep the
  stable CLI link fresh, make sure the watchdog runs, warn when the machine
  is already loaded or crowded.
- UserPromptSubmit: mark this session as the one the user is typing in,
  resume it if it was frozen, surface what the guard did meanwhile.
- PreToolUse: the gate (see rg_gate).
- PostToolUse: tell Claude, once, that its work was frozen or resumed, and
  give an occasional nudge when load is elevated. It also runs the
  dead-man switch, so a watchdog that dies mid-turn is noticed mid-turn.
- SessionEnd: hand the session to the watchdog; the event's ~1.5 s budget
  leaves no room for real work.

Fail-open: any error exits 0 with no output. A broken resource guard must
never block the user's work (the opposite of commit-guard, which guards a
security boundary and fails closed).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import sys
import time
import traceback
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

GATED_EVENTS = ("PreToolUse", "PostToolUse")
# PreToolUse times out at 45 s; past that Claude Code lets the call through
# anyway, so a longer wait would only hide the deny.
MAX_GATE_WAIT = 35.0
HUNG_NOTE = ("its watchdog has stopped reporting (hung?); `resource-guard watchdog stop` restarts it and "
             "resumes frozen work")


def supported() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    import rg_wsl

    info = rg_wsl.detect()
    return not (info["wsl"] and info["version"] == 1)


def _session(data: dict):
    import rg_sessions

    own = rg_sessions.own_session()
    if own is None:
        return None
    pid, start = own
    return rg_sessions.Session(pid=pid, start=start, session_id=str(data.get("session_id", "")))


def _label(key: str, sessions: list) -> str:
    for session in sessions:
        if session.key == key:
            name = os.path.basename(session.cwd.rstrip("/")) if session.cwd else ""
            return f"{name or 'session'} (pid {session.pid})"
    return f"pid {key.split('-')[0]}"


def _summarize(events: list, sessions: list) -> list:
    lines = []
    for event in events:
        kind, key = event.get("kind"), event.get("session", "")
        if kind == "freeze":
            containers = ", ".join(event.get("containers") or []) or "no containers"
            lines.append(f"froze {_label(key, sessions)}: {event.get('procs', 0)} processes, {containers}")
        elif kind == "resume":
            lines.append(f"resumed {_label(key, sessions)}")
        elif kind == "orphan-stopped":
            lines.append(f"stopped orphaned containers of {_label(key, sessions)}: {', '.join(event['containers'])}")
        elif kind == "held-by-idle":
            lines.append("memory stays low but calm: idle servers hold it; `resource-guard sessions` shows which")
        elif kind == "unpause-failed":
            lines.append(f"could not unpause {', '.join(event['containers'])} of {_label(key, sessions)}: "
                         "`docker unpause` them by hand")
    return lines


def _fmt_kb(kb: float) -> str:
    return f"{kb / (1024 * 1024):.1f} GB" if kb >= 1024 * 1024 else f"{kb / 1024:.0f} MB"


def _footprints(cfg: dict) -> str:
    """Per-session memory from the watchdog's last status, when it is fresh:
    a hook can't afford its own /proc scan."""
    import rg_common as common
    import rg_pressure as pressure

    status = common.read_json(pressure.status_path(), {}) or {}
    if not isinstance(status, dict) or time.time() - status.get("ts", 0) > 4 * cfg.get("status_max_age_seconds", 15):
        return ""
    def cost_kb(row: dict) -> float:
        # A session's own MCP/LSP server containers are part of what it costs.
        servers = sum((c.get("mem_bytes") or 0) for c in row.get("containers") or []
                      if isinstance(c, dict) and c.get("role") == "server")
        return row.get("tree_rss_kb", 0) + servers / 1024

    rows = sorted((r for r in status.get("sessions") or [] if isinstance(r, dict)), key=cost_kb, reverse=True)
    parts = [f"{os.path.basename(str(r.get('cwd', '')).rstrip('/')) or r.get('key')} {_fmt_kb(cost_kb(r))}"
             for r in rows[:5]]
    return ", ".join(parts)


def _ensure_watchdog() -> str:
    import rg_watchdog_ctl as ctl

    return ctl.ensure_watchdog()


def _unseen(cursor_name: str, kinds: tuple, key: str | None = None) -> list:
    import rg_common as common

    path = common.state_dir() / "cursors" / f"{cursor_name}.json"
    read_at = time.time()
    since = (common.read_json(path, {}) or {}).get("ts", read_at - 3600)
    events = [e for e in common.read_events(since) if e.get("kind") in kinds and (key is None or e.get("session") == key)]
    # The read time, not "now" after it: an event appended while reading
    # stays newer than the cursor and is shown next time.
    common.atomic_write_json(path, {"ts": max([read_at] + [e.get("ts", 0) for e in events])})
    return events


# -- SessionStart -----------------------------------------------------------


def _write_shim_path(env_file: str) -> None:
    import rg_common as common

    line = f'export PATH={shlex.quote(str(common.plugin_root() / "shims"))}:"$PATH"\n'
    path = Path(env_file)
    try:
        existing = path.read_text() if path.exists() else ""
        if line not in existing:
            with path.open("a") as fh:
                fh.write(line)
    except OSError:
        pass


def _relink(link: Path, target: Path) -> None:
    """Point the stable `link` at this plugin version's `target`. The rename
    swaps it in one step, so nobody resolving it ever finds it missing."""
    try:
        link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if link.is_symlink() and os.readlink(link) == str(target):
            return
        # A name of its own: sessions starting together must not unlink
        # each other's half-made link.
        tmp = link.with_name(f".{link.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        os.symlink(target, tmp)
        try:
            os.replace(tmp, link)
        except OSError:
            os.unlink(tmp)
            raise
    except OSError:
        pass


def _link_cli() -> None:
    import rg_common as common

    _relink(common.state_dir() / "bin" / "resource-guard", common.plugin_root() / "scripts" / "resource_guard.py")


def _link_shims() -> None:
    """~/.claude/.resource-guard/shims is what goes on Claude Code's own PATH
    (the plugin cache path changes with every version)."""
    import rg_common as common

    _relink(common.state_dir() / "shims", common.plugin_root() / "shims")


# docker's own floor is 6 MiB: a smaller --memory makes `docker run` fail,
# and the MCP server with it.
CAP_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
MIN_CAP = 6 * 1024 ** 2


def _cap(value) -> str | None:
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if text == "none":
        return text
    # ASCII digits and no leading zero: the shim's own check, which would
    # otherwise reject the value and leave that image uncapped.
    match = re.fullmatch(r"([1-9][0-9]*)([bkmgBKMG]?)", text)
    if not match or int(match.group(1)) * CAP_UNITS[match.group(2).lower()] < MIN_CAP:
        return None
    return text


def shim_conf(cfg: dict) -> str:
    """shim.conf from cfg["server_caps"]. The shim takes the first image glob
    that matches, so the most literal one goes first. Values that docker
    would refuse are dropped here, never handed to `docker run`."""
    lines = ["# resource-guard: memory caps for session MCP/LSP containers.",
             "# Written at SessionStart from server_caps; edit ~/.claude/resource-guard.json instead."]
    caps = cfg.get("server_caps")
    if isinstance(caps, dict):
        images = caps.get("images") if isinstance(caps.get("images"), dict) else {}
        for pattern in sorted(images, key=lambda p: (-len(p.replace("*", "").replace("?", "")), p)):
            value = _cap(images[pattern])
            if value and pattern and pattern.isascii() and pattern.isprintable() and " " not in pattern:
                lines.append(f"image {pattern} {value}")
        default = _cap(caps.get("default", ""))
        if default:
            lines.append(f"default {default}")
    return "\n".join(lines) + "\n"


def _write_shim_conf(cfg: dict) -> None:
    import rg_common as common

    path = common.state_dir() / "shim.conf"
    text = shim_conf(cfg)
    try:
        if path.is_file() and path.read_text() == text:
            return
        common.atomic_write_text(path, text)
    except OSError:
        pass


def on_session_start(data: dict, cfg: dict) -> dict | None:
    import rg_pressure as pressure
    import rg_sessions
    import rg_wsl

    if os.environ.get("CLAUDE_ENV_FILE"):
        _write_shim_path(os.environ["CLAUDE_ENV_FILE"])
    _link_cli()
    _link_shims()
    _write_shim_conf(cfg)
    state = _ensure_watchdog()

    if data.get("source") == "compact":
        return None
    notes = []
    if state == "gave_up":
        notes.append("its watchdog keeps crashing (see ~/.claude/.resource-guard/watchdog.log); only the gate is active")
    elif state == "hung":
        notes.append(HUNG_NOTE)
    sessions = rg_sessions.cc_sessions()
    if len(sessions) >= cfg.get("max_sessions", 3):
        notes.append(f"{len(sessions)} Claude sessions are running")
    level, reasons, _ = pressure.current_level(cfg, rg_wsl.detect()["wsl"])
    if level != "ok":
        notes.append(f"load is {level} ({', '.join(reasons)})")
    if not notes:
        return None
    footprints = _footprints(cfg)
    if footprints:
        notes.append(f"sessions by memory: {footprints}")
    message = "resource-guard: " + "; ".join(notes) + ". Heavy work in background sessions will be held back; " \
        "/resource-guard shows what is using the machine."
    return {"systemMessage": message,
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message}}


# -- UserPromptSubmit -------------------------------------------------------


def on_user_prompt(data: dict, cfg: dict) -> dict | None:
    import rg_actions as actions
    import rg_common as common
    import rg_docker as docker_mod
    import rg_sessions
    import rg_watchdog_ctl as ctl

    if data.get("is_continuation"):
        return None
    session = _session(data)
    if session is None:
        return None
    rg_sessions.touch_prompt(session.key)
    own_note = None
    # The user's own session first: resuming it is what they are waiting for.
    if session.key in actions.frozen_keys():
        try:
            actions.resume_session(session.key, docker=docker_mod.client(), reason="prompt",
                                   stop_grace=ctl.HOOK_STOP_GRACE)
            own_note = "resource-guard resumed this session's frozen work because the user is working in it again."
        except common.LockTimeout:
            ctl.post_request("resume", session.key)
    state = _ensure_watchdog()

    events = _unseen(f"{session.key}-prompt", ("freeze", "resume", "orphan-stopped", "held-by-idle", "unpause-failed"))
    lines = _summarize([e for e in events if e.get("session") != session.key or e["kind"] != "resume"],
                       rg_sessions.cc_sessions())
    if state == "hung":
        lines.append(HUNG_NOTE)
    if not lines and not own_note:
        return None
    out = {}
    context = [own_note] if own_note else []
    if lines:
        out["systemMessage"] = "resource-guard: " + "; ".join(lines[-5:])
        context.append("resource-guard since the last prompt: " + "; ".join(lines[-5:]) + ".")
    out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": " ".join(context)}
    return out


# -- PreToolUse -------------------------------------------------------------


def on_pre_tool(data: dict, cfg: dict) -> dict | None:
    import rg_gate as gate

    tool = str(data.get("tool_name", ""))
    kind = gate.classify_tool(tool, data.get("tool_input") or {}, cfg)
    if kind != "heavy" or not cfg.get("gate", True):
        return None

    import rg_pressure as pressure
    import rg_sessions
    import rg_wsl

    wsl = rg_wsl.detect()["wsl"]
    level, reasons, _ = pressure.current_level(cfg, wsl)
    session = _session(data)
    now = time.time()
    if session is None:
        is_foreground = True
    else:
        protected = rg_sessions.foreground(rg_sessions.cc_sessions(), now, cfg.get("foreground_grace_seconds", 60))
        is_foreground = session.key in protected
    decision = gate.decide(level, is_foreground, kind)
    if decision == "pass":
        return None
    if decision == "ask":
        return gate.ask_output(tool, level, reasons)

    def level_now():
        return pressure.current_level(cfg, wsl)[0]

    below = "elevated" if decision == "soft_wait" else "critical"
    wait = min(float(cfg.get("gate_wait_seconds", 20)), MAX_GATE_WAIT)
    final = gate.wait_for_calm(level_now, wait, cfg.get("gate_poll_seconds", 2), below)
    if decision == "soft_wait" or gate.decide(final, is_foreground, kind) != "wait_deny":
        return None
    final_level, final_reasons, _ = pressure.current_level(cfg, wsl)
    burst = cfg.get("deny_burst", {})
    key = session.key if session else "unknown"
    count = gate.record_deny(key, time.time(), burst.get("window_seconds", 300))
    if count >= burst.get("count", 3):
        return gate.stop_output(count, final_level, final_reasons)
    return gate.deny_output(tool, final_level, final_reasons)


# -- PostToolUse ------------------------------------------------------------


def on_post_tool(data: dict, cfg: dict) -> dict | None:
    import rg_common as common
    import rg_pressure as pressure
    import rg_wsl

    _ensure_watchdog()
    session = _session(data)
    if session is None:
        return None
    notes = []
    for event in _unseen(f"{session.key}-post", ("freeze", "resume"), key=session.key):
        if event["kind"] == "freeze":
            notes.append(
                "resource-guard froze this session's running Bash work and containers because the machine is "
                "overloaded. They resume on their own when load drops, or when the user types in this session. "
                "A command that seems stuck is frozen, not hung: do not retry or kill it."
            )
        else:
            notes.append("resource-guard resumed this session's frozen work.")
    stamp = common.state_dir() / "advisory" / f"{session.key}.json"
    last = (common.read_json(stamp, {}) or {}).get("ts", 0)
    if time.time() - last >= cfg.get("advisory_interval_seconds", 600):
        level, reasons, _ = pressure.current_level(cfg, rg_wsl.detect()["wsl"])
        if level != "ok":
            common.atomic_write_json(stamp, {"ts": time.time()})
            notes.append(
                f"resource-guard: machine load is {level} ({', '.join(reasons)}). Prefer light work: don't start "
                "parallel subagents, test suites or containers unless the user needs them now."
            )
    if not notes:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": " ".join(notes)}}


# -- SessionEnd -------------------------------------------------------------


def on_session_end(data: dict, cfg: dict) -> dict | None:
    import rg_watchdog_ctl as ctl

    session = _session(data)
    if session is not None:
        ctl.post_request("end", session.key)
    return None


HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt,
    "PreToolUse": on_pre_tool,
    "PostToolUse": on_post_tool,
    "SessionEnd": on_session_end,
}


def _log_error() -> None:
    try:
        import rg_common as common

        with open(common.state_dir() / "hook-errors.log", "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {traceback.format_exc(limit=5)}\n")
    except Exception:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return 0
    except (ValueError, OSError):
        return 0
    try:
        if not supported():
            return 0
        import rg_common as common

        cfg = common.load_config()
        if common.disabled(cfg):
            return 0
        handler = HANDLERS.get(str(data.get("hook_event_name", "")))
        out = handler(data, cfg) if handler else None
        if out:
            print(json.dumps(out))
    except Exception:
        _log_error()
    return 0


if __name__ == "__main__":
    sys.exit(main())
