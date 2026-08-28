#!/usr/bin/env python3
"""
wandavision PostToolUse hook. Matcher: Write|Edit|Bash|.*_take_screenshot|.*browse_screenshot.

Fires after any file write/edit/bash call, or after any tool whose name looks
like a screenshot-capture tool (Playwright's browser_take_screenshot,
chrome-devtools' take_screenshot, camoufox's browse_screenshot, ...).

Screenshot tools return image content directly in the tool response rather
than a guaranteed on-disk path under .wandavision_workspace/, so this hook
can't verify placement for them the way it can for Write/Edit/Bash -- it just
fires unconditionally on those tool names as a reminder. The wandavision
skill is the primary enforcement for that case (Claude must proactively save
the result into the workspace itself).

For Write/Edit/Bash, this only reminds Claude when a path under
.wandavision_workspace/ with an image extension is actually involved --
staying silent otherwise so it doesn't nag on every unrelated file write.

Never blocks (PostToolUse can't undo an action that already happened) --
always exits 0.
"""

import json
import re
import sys

SCREENSHOT_TOOL_RE = re.compile(r"(_take_screenshot|browse_screenshot)$")
WORKSPACE_IMAGE_RE = re.compile(
    r"\.wandavision_workspace[/\\].*\.(png|jpe?g|gif|bmp|webp)$", re.IGNORECASE
)

SCREENSHOT_TOOL_MESSAGE = (
    "[wandavision] Screenshot tool just ran. Save its output image into "
    "./.wandavision_workspace/ (create it if missing) and then call the "
    "wandavision MCP tool (e.g. locate_objects) on it now -- never eyeball "
    "or estimate pixel data from a screenshot."
)

WORKSPACE_IMAGE_MESSAGE = (
    "[wandavision] Image detected in .wandavision_workspace/. Call the "
    "wandavision MCP tool (e.g. locate_objects) on it now instead of "
    "reasoning about it visually."
)


def emit(message: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    }))


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if SCREENSHOT_TOOL_RE.search(tool_name):
        emit(SCREENSHOT_TOOL_MESSAGE)
        sys.exit(0)

    candidate = tool_input.get("file_path") or tool_input.get("command") or ""
    if WORKSPACE_IMAGE_RE.search(candidate):
        emit(WORKSPACE_IMAGE_MESSAGE)

    sys.exit(0)


if __name__ == "__main__":
    main()
