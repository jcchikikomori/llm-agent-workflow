#!/usr/bin/env python3
"""
mempalace-docker SessionStart hook.

Does two things, both advisory -- SessionStart cannot block anything, so it
only ever surfaces context for Claude to act on (exit 0, additionalContext).

1. CONFLICT SCAN. This plugin owns the `mempalace` MCP server name, the
   containerized CLI shims, and the save hooks. Any leftover copy of the
   official plugin, the hand-rolled ~/.claude.json server, or the
   ~/.local/bin shims will fight it -- two MCP servers named `mempalace`
   collide outright. Reported once per session, and silenced for good once
   the dismissed marker is written.

2. AUTO-MINE. Checks the per-project stamp and, when the project has never
   been mined / HEAD has moved / the stamp is stale, tells Claude to mine it.
   The container path is /work, never the host path.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mempalace_docker_common import (  # noqa: E402
    conflicts_dismissed,
    gc_sessions,
    mark_session,
    mine_reason,
    plugin_root,
    project_root,
    session_marked,
)

HOME = Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
SETTINGS_LOCAL = HOME / ".claude" / "settings.local.json"
CLAUDE_JSON = HOME / ".claude.json"

# ~/.claude.json accumulates per-project history and can get large. Parsing a
# runaway file on every session start is not worth it; skip that one check
# rather than stall the session.
MAX_CLAUDE_JSON_BYTES = 50 * 1024 * 1024

STALE_HOOK_MARKERS = ("plugins/marketplaces/mempalace", ".local/bin/mempal_")


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_official_plugin():
    data = load_json(SETTINGS) or {}
    enabled = data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return None
    stale = [
        key
        for key, on in enabled.items()
        if on and key.split("@", 1)[0] == "mempalace"
    ]
    if not stale:
        return None
    return (
        f"Official mempalace plugin still enabled: {', '.join(sorted(stale))}.\n"
        "  It registers a second MCP server also named `mempalace`, and its\n"
        "  server command (`mempalace-mcp`) is a bare binary that is not on\n"
        "  PATH, so it fails to connect. Uninstall it:\n"
        f"      /plugin uninstall {sorted(stale)[0]}\n"
        "      /reload-plugins"
    )


def check_plain_mcp_server():
    try:
        if not CLAUDE_JSON.is_file() or CLAUDE_JSON.stat().st_size > MAX_CLAUDE_JSON_BYTES:
            return None
    except OSError:
        return None
    data = load_json(CLAUDE_JSON)
    if not isinstance(data, dict):
        return None
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or "mempalace" not in servers:
        return None
    return (
        "A hand-rolled `mempalace` MCP server is still defined in "
        "~/.claude.json (top-level mcpServers).\n"
        "  It duplicates this plugin's server under the same name, mounts a\n"
        "  single hardcoded project directory, and passes no GPU flags -- so\n"
        "  a CUDA image runs its embeddings on CPU. Remove that one entry\n"
        "  (leave the rest of the file alone; it holds unrelated config)."
    )


def check_local_shims():
    found = [
        str(HOME / ".local" / "bin" / name)
        for name in ("mempalace", "mempalace-python3")
        if (HOME / ".local" / "bin" / name).exists()
    ]
    if not found:
        return None
    return (
        "Hand-rolled CLI shims still present:\n"
        + "".join(f"      {p}\n" for p in found)
        + "  These shadow this plugin's shims on PATH, and they pass\n"
        "  `--palace $HOME/.mempalace/palace` with `-e HOME=$HOME`, which\n"
        "  writes the HOST palace instead of the shared docker volume. That\n"
        "  is what split the palace in two. Remove or rename them."
    )


def check_stale_hooks():
    hits = []
    for path in (SETTINGS, SETTINGS_LOCAL):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(marker in raw for marker in STALE_HOOK_MARKERS):
            hits.append(path.name)
    if not hits:
        return None
    return (
        f"Save hooks pointing at the old setup are still registered in: {', '.join(hits)}.\n"
        "  This plugin already registers Stop / PreCompact / SessionEnd, so\n"
        "  those entries will double-fire, and their paths break once the\n"
        "  official plugin is uninstalled. Remove the mempalace hook entries\n"
        "  from those files (note Stop and PreCompact are currently declared\n"
        "  in BOTH settings.json and settings.local.json)."
    )


CONFLICT_CHECKS = (
    check_official_plugin,
    check_plain_mcp_server,
    check_local_shims,
    check_stale_hooks,
)


def conflict_report():
    if conflicts_dismissed():
        return None
    findings = []
    for check in CONFLICT_CHECKS:
        try:
            result = check()
        except Exception:  # a broken check must never break a session start
            result = None
        if result:
            findings.append(result)
    if not findings:
        return None

    numbered = "\n".join(f"  {i}. {f}" for i, f in enumerate(findings, 1))
    return (
        "[mempalace-docker] Conflicting mempalace setups detected. Tell the "
        "user about these, in this order, and let them decide -- do not edit "
        "their settings without asking:\n"
        f"{numbered}\n"
        "  Until the duplicates are gone, which `mempalace` server answers a "
        "tool call is undefined.\n"
        "  Once the user has dealt with them (or says to stop being asked), "
        "silence this permanently:\n"
        f"      touch {Path(os.environ.get('MEMPALACE_DOCKER_STATE', HOME / '.claude' / '.mempalace-docker')) / 'conflicts-dismissed'}"
    )


def mine_report():
    root = project_root()
    reason = mine_reason(root)
    if reason is None:
        return None
    return (
        f"[mempalace-docker] Project not mined into the palace ({reason}):\n"
        f"      {root}\n"
        "  This project is bind-mounted into the mempalace container at "
        "`/work` (read-only).\n"
        "  Mine it, then record the stamp so this stops being raised:\n"
        "    1. call mcp__mempalace__mempalace_mine with the path `/work` "
        "-- the CONTAINER path, never the host path\n"
        f"    2. run: python3 {plugin_root() / 'scripts' / 'mark_mined.py'}\n"
        "  Do this in the background of whatever the user actually asked "
        "for; do not block their request on it, and do not mine twice in one "
        "session. If the mine fails, say so once and move on -- mention that "
        "a cold palace volume downloads the ~80 MB embedding model on first "
        "use, so a slow first call is expected rather than a hung container."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}
    session_id = str(payload.get("session_id") or "")

    blocks = []

    # The conflict warning is per session; the mine prompt is per project
    # state, so it stands on its own.
    if not session_marked(session_id):
        report = conflict_report()
        if report:
            blocks.append(report)
        mark_session(session_id)

    mine = mine_report()
    if mine:
        blocks.append(mine)

    gc_sessions()

    if blocks:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n\n".join(blocks),
            }
        }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never let this hook take a session down with it.
        sys.exit(0)
