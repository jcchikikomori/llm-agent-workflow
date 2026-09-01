---
description: Routes every image — user-submitted or produced by any tool (Playwright, chrome-devtools, camoufox screenshots included) — into ./.wandavision_workspace/ and through the wandavision MCP server for deterministic, pixel-accurate analysis. Use whenever an image needs measurable data extracted from it (object locations, coordinates, colors) instead of being visually described.
---

# wandavision

Bundles a local computer-vision MCP server ([`mcp-vision`](https://github.com/jcchikikomori/mcp-vision), run via Docker) and enforces that every image — user-submitted or produced by any other tool — gets pixel-accurate, deterministic analysis instead of the LLM eyeballing it.

## MCP Setup

### 1. Clone the fork

```bash
git clone https://github.com/jcchikikomori/mcp-vision
```

### 2. Build the Docker image (one-time, per machine)

From the cloned directory:

```bash
cd mcp-vision
docker build -t mcp-vision .
```

This plugin doesn't vendor or build the third-party image — the wrapper script assumes `mcp-vision:latest` already exists locally.

### 3. Warm the model cache (one-time, per machine)

```bash
<path-to-plugin-wandavision>/scripts/warm-cache.sh
```

Pulls `google/owlvit-large-patch14` (~1.7 GB) into the `wandavision-hf-cache` Docker volume. Skipping this doesn't just make the first connect slow — it makes it *fail*: the server builds its pipeline inside the FastMCP lifespan, before `initialize` is answered, so a cold cache keeps the client waiting minutes past its startup timeout. The wrapper refuses to start on a cold cache and prints this command rather than hanging.

### 4. Register the MCP server in `opencode.json`

Add the `wandavision` entry to the `"mcp"` key of your project's `opencode.json` (create it if missing). Point `command` at this plugin's wrapper script:

```json
{
  "mcp": {
    "wandavision": {
      "type": "local",
      "command": ["<path-to-plugin-wandavision>/scripts/run-wandavision.sh"],
      "enabled": true
    }
  }
}
```

The wrapper script auto-detects NVIDIA GPU support (`nvidia-smi`): if present it adds `--runtime=nvidia --gpus all`, otherwise it falls back to CPU-only. Either way it bind-mounts `./.wandavision_workspace` (created if missing) to `/data` inside the container.

## Workspace Enforcement (all sources)

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

## Notes

- MCP servers are configured in `opencode.json` (`"mcp"` key), not in a `.mcp.json` file.
- This plugin is a port of the Claude Code `wandavision` plugin — config paths here follow OpenCode conventions: user config lives under `~/.config/opencode/` and project instructions live in `AGENTS.md`.
- The workspace directory (`.wandavision_workspace/`) is created relative to wherever OpenCode was launched from (the project root) — it's project-local, not shared across projects.
