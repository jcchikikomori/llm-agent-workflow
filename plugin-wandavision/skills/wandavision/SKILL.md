---
name: wandavision
description: Routes every image — user-submitted or produced by any tool (Playwright, chrome-devtools, camoufox screenshots included) — into ./.wandavision_workspace/ and through the wandavision MCP server for deterministic, pixel-accurate analysis. Use whenever an image needs measurable data extracted from it (object locations, coordinates, colors) instead of being visually described.
---

# wandavision

Enforces one workspace directory for images and bans visual guesswork in favor of the `wandavision` MCP server (local `mcp-vision` computer-vision tools running in Docker).

## Workspace enforcement (all sources)

Every image must be saved under `./.wandavision_workspace/` before any further use — no exceptions:

- **User-submitted images/screenshots** — save into the workspace first.
- **Tool-generated screenshots** — Playwright's `browser_take_screenshot`, chrome-devtools' `take_screenshot`, camoufox's `browse_screenshot`, or any other tool that returns image content — must be written into the workspace too, not left in `/tmp` or whatever default path the tool used.
- Create the directory if it doesn't exist: `mkdir -p .wandavision_workspace`.
- If the image already exists elsewhere on disk, copy it in — don't leave a second copy of the "real" file outside the workspace.

## Auto-trigger on save

The moment an image lands in `.wandavision_workspace/`, call the matching `wandavision` MCP tool against it — don't wait to be asked. The workspace is bind-mounted into the `wandavision` MCP server's container at `/data`, so pass the in-container path (`/data/<filename>`), not the host path.

If unsure of the exact tool name available (e.g. `locate_objects`), check the live MCP tool list rather than guessing — the bundled `mcp-vision` server version may expose a different tool set.

## STRICT RULE — never eyeball visual data

Never estimate, guess, or visually approximate:

- Coordinates or bounding boxes
- Object positions or counts
- Colors
- Any other measurable property of an image

Any tool-generated screenshot must be run through `wandavision` to extract pixel-accurate, deterministic data before that data is used, reported, or acted on (e.g. clicking at a coordinate, verifying a layout, comparing colors). Treat a raw visual read of an image as untrustworthy for anything requiring precision — a "looks about right" read is not acceptable when a deterministic tool can give the exact answer.
