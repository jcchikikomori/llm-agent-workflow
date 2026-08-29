# wandavision

Bundles a local computer-vision MCP server ([`mcp-vision`](https://github.com/jcchikikomori/mcp-vision), run via Docker) and enforces that every image — user-submitted or produced by any other tool — gets pixel-accurate, deterministic analysis instead of the LLM eyeballing it.

## How It Works

- A **`.mcp.json`** registers the `wandavision` MCP server, launched through a bundled wrapper script (`scripts/run-wandavision.sh`) instead of calling `docker` directly.
- The wrapper **auto-detects NVIDIA GPU support**: if `nvidia-smi` is present and runs successfully, it adds `--runtime=nvidia --gpus all`; otherwise it falls back to CPU-only. Either way it bind-mounts `./.wandavision_workspace` (created if missing) to `/data` inside the container.
- A **PostToolUse hook** fires on `Write|Edit|Bash` and on any tool name that looks like a screenshot capture (`*_take_screenshot`, `*browse_screenshot` — covers Playwright, chrome-devtools, camoufox), reminding Claude to save the image into the workspace and run it through `wandavision`.
- A companion **skill** is the primary enforcement: every image (user-submitted or tool-generated) must land in `./.wandavision_workspace/`, then get analyzed by a `wandavision` MCP tool — never described from a raw visual read when precision matters (coordinates, positions, colors).

## Setup

### 1. Build the `mcp-vision` Docker image (one-time, per machine)

```bash
git clone https://github.com/jcchikikomori/mcp-vision.git "${XDG_DATA_HOME:-$HOME/.local/share}/mcp-vision"
cd "${XDG_DATA_HOME:-$HOME/.local/share}/mcp-vision"
docker build -t mcp-vision .
```

This repo doesn't vendor or build the third-party image — the plugin's wrapper script assumes `mcp-vision:latest` already exists locally.

### 2. Install the plugin

```bash
/plugin install wandavision@claude-workflow
/reload-plugins
```

### 3. Verify

```bash
/mcp
```

Should show `wandavision` connected, not `CONNECTION_CLOSED`. If it's closed, confirm the Docker image was built (step 1) and that Docker itself is running.

## Notes

- GPU acceleration is **optional and automatic** — no config flag to flip. If `nvidia-smi` fails or isn't installed, the server runs CPU-only with the exact same tool behavior.
- The workspace directory (`.wandavision_workspace/`) is created relative to wherever Claude Code was launched from (the project root) — it's project-local, not shared across projects.

## Version History

### 0.2.0

- Added OpenCode TypeScript port using `tool.execute.after` (`plugins/opencode-wandavision.ts`)
- Added `/wandavision` slash command (`commands/wandavision.md`) with 3-step MCP setup instructions for OpenCode
- MCP server now references the `jcchikikomori/mcp-vision` fork instead of upstream
- Added OpenCode npm package manifest

### 0.1.0

- Initial implementation
- `.mcp.json` + GPU-auto-detecting wrapper script for the `wandavision` MCP server
- `wandavision` skill: workspace enforcement for all image sources, auto-trigger on save, strict no-eyeballing rule
- PostToolUse hook covering file writes and known screenshot-tool names
