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

### 2. Warm the model cache (one-time, per machine)

```bash
"${CLAUDE_PLUGIN_ROOT:-.}"/scripts/warm-cache.sh
```

Downloads `google/owlvit-large-patch14` (~1.7 GB) into the `wandavision-hf-cache` Docker volume. Takes a few minutes on a cold cache. **Don't skip it** — see the timeout note below for why the server can't do this itself.

### 3. Install the plugin

```bash
/plugin install wandavision@claude-workflow
/reload-plugins
```

### 4. Verify

```bash
/mcp
```

Should show `wandavision` connected, not `CONNECTION_CLOSED`. If it's closed, read the wrapper's stderr — it names the reason. Common ones: the Docker image was never built (step 1), the model cache is cold (step 2), or Docker itself isn't running.

## Why Startup Times Out on a Cold Cache

`mcp_vision/server.py` builds its detection pipeline inside the FastMCP lifespan, **before** `yield`:

```python
@asynccontextmanager
async def app_lifespan(server: FastMCP):
    init_objdet_pipeline()   # blocking, ~1.7 GB download on a cold cache
    yield
```

FastMCP doesn't service stdio until that lifespan yields, so `initialize` goes unanswered for the entire download. Measured on an RTX 500 Ada with a ~8.7 MB/s link: **~3.5 minutes**. The client's MCP startup timeout is 30s by default, so it gives up long before — and because it kills the `docker` CLI rather than the container, the container keeps downloading with nobody listening. That's where orphaned `mcp-vision` containers come from.

Two consequences worth internalizing:

- **No mount or volume change can fix a cold start.** The cache volume only helps *after* the model is on disk. The first population has to happen outside the MCP handshake, which is what `warm-cache.sh` is for.
- **Warm startup still isn't instant** — the weights are read from disk and moved onto the GPU. If a warm connect still times out on your machine, raise the client-side budget (Claude Code reads `MCP_TIMEOUT`, in milliseconds, from its own environment — not from `.mcp.json`, whose `env` block is passed to the server process instead):

```bash
export MCP_TIMEOUT=120000
```

Clean up any orphans left behind by earlier timeouts:

```bash
docker ps --filter ancestor=mcp-vision -q | xargs -r docker rm -f
```

## Notes

- GPU acceleration is **optional and automatic** — no config flag to flip. If `nvidia-smi` fails or isn't installed, the server runs CPU-only with the exact same tool behavior.
- The workspace directory (`.wandavision_workspace/`) is created relative to wherever Claude Code was launched from (the project root) — it's project-local, not shared across projects.
- The model cache **is** shared across projects, in the Docker volume `wandavision-hf-cache` (override with `WANDAVISION_HF_CACHE_VOLUME`). Warming it once covers every project on the machine.
- Concurrent cold starts don't share work — HuggingFace takes a per-file lock, so a second container blocks on the first instead of downloading in parallel. Another reason to warm the cache serially, up front.
- Containers are named `wandavision-mcp-<hash of project dir>`. Exited leftovers get removed at startup; a running one is reaped only when no client process holds it, so a second session in the same directory won't kill the first session's server.
- Override the image name with `WANDAVISION_IMAGE`, and bypass the cold-cache preflight with `WANDAVISION_SKIP_CACHE_CHECK=1`.

## Version History

### 0.3.0

- Added `scripts/warm-cache.sh` — pre-downloads `google/owlvit-large-patch14` into the cache volume outside the MCP handshake, by calling the server's own `init_objdet_pipeline()` so the cached file set matches what startup asks for
- `scripts/run-wandavision.sh` now refuses to start on a cold or half-downloaded cache, with the warm-up command on stderr, instead of hanging until the client times out
- Containers are now named per project directory (`wandavision-mcp-<hash>`); exited leftovers are removed on start, and a running one is reaped only when no client process holds it
- Documented the real cause of `CONNECTION_CLOSED`: the pipeline loads inside the FastMCP lifespan, before `initialize` is answered, so cold-cache startup exceeds the client's MCP startup timeout no matter what the mounts look like

### 0.2.1

- Fixed `CONNECTION_CLOSED` on startup — container had no persistent model cache, so every run re-downloaded the HF pipeline weights and blew the MCP handshake timeout
- Mounted named volume `wandavision-hf-cache` (override via `WANDAVISION_HF_CACHE_VOLUME`) at `/root/.cache/huggingface` in `scripts/run-wandavision.sh`

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
