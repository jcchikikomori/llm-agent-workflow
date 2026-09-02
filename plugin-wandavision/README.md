# wandavision

Bundles a local computer-vision MCP server ([`mcp-vision`](https://github.com/jcchikikomori/mcp-vision), run via Docker) and enforces that every image — user-submitted or produced by any other tool — gets pixel-accurate, deterministic analysis instead of the LLM eyeballing it.

## Layout

Since `0.4.0`, the runtime files live in the **XDG data directory** so Claude Code and OpenCode share one canonical install:

```
~/.local/share/com.jcchikikomori.llmworkflow/wandavision/
├── bin/
│   ├── run-wandavision.sh        # GPU-detect wrapper, hf-cache preflight
│   └── warm-cache.sh             # one-time model download
├── skill/wandavision/SKILL.md    # tool-agnostic skill body
└── opencode-plugin/
    └── opencode-wandavision.ts   # reminder hook (also copied to the dotfiles project)
```

This Claude Code marketplace plugin (`plugin-wandavision/`) is a **thin shim**: it ships only metadata plus a redirector (`scripts/run-wandavision.sh`) that hands off to the XDG wrapper. The actual scripts, skill body, and TS plugin live in `wandavision/` at the repo root and are installed into XDG by `./setup-wandavision.sh`.

## How It Works

- A **`.mcp.json`** registers the `wandavision` MCP server, launched through `scripts/run-wandavision.sh` (the redirector), which `exec`s the XDG wrapper at `$HOME/.local/share/com.jcchikikomori.llmworkflow/wandavision/bin/run-wandavision.sh`.
- The XDG wrapper **auto-detects NVIDIA GPU support**: if `nvidia-smi` is present and runs successfully, it adds `--runtime=nvidia --gpus all`; otherwise it falls back to CPU-only. Either way it bind-mounts `./.wandavision_workspace` (created if missing) to `/data` inside the container.
- A companion **skill** is the primary enforcement: every image (user-submitted or tool-generated) must land in `./.wandavision_workspace/`, then get analyzed by a `wandavision` MCP tool — never described from a raw visual read when precision matters (coordinates, positions, colors).
- An **OpenCode reminder hook** (`opencode-plugin/opencode-wandavision.ts`) fires after every tool call and logs a reminder when an image is written, a screenshot tool runs, or an image lands outside `.wandavision_workspace/`. Loaded by opencode from the dotfiles project; see `~/.local/share/com.jcchikikomori.llmworkflow/wandavision/dotfiles-migration.md` for the wiring.

## Setup

### 1. Clone the fork and build the `mcp-vision` Docker image (one-time, per machine)

```bash
git clone https://github.com/jcchikikomori/mcp-vision.git "${XDG_DATA_HOME:-$HOME/.local/share}/mcp-vision"
cd "${XDG_DATA_HOME:-$HOME/.local/share}/mcp-vision"
docker build -t mcp-vision .
```

This plugin doesn't vendor or build the third-party image — the wrapper assumes `mcp-vision:latest` already exists locally.

### 2. Install the runtime files into XDG (one-time, per machine)

From the repo root:

```bash
./setup-wandavision.sh
```

Copies the four files in `wandavision/` (source of truth) into `~/.local/share/com.jcchikikomori.llmworkflow/wandavision/`. Idempotent — re-run any time the repo copy changes.

### 3. Warm the model cache (one-time, per machine)

```bash
~/.local/share/com.jcchikikomori.llmworkflow/wandavision/bin/warm-cache.sh
```

Downloads `google/owlvit-large-patch14` (~1.7 GB) into the `wandavision-hf-cache` Docker volume. Takes a few minutes on a cold cache. **Don't skip it** — see the timeout note below for why the server can't do this itself.

### 4. Install the marketplace plugin (Claude Code users)

```bash
/plugin install wandavision@claude-workflow
/reload-plugins
```

The plugin's `.mcp.json` registers `wandavision`, pointing at the redirector (`scripts/run-wandavision.sh`) which hands off to the XDG wrapper from step 2.

### 5. OpenCode users

Ensure `~/.config/opencode/opencode.jsonc` has a `wandavision` entry under `mcp` pointing at the XDG wrapper:

```json
"wandavision": {
  "type": "local",
  "command": ["{env:HOME}/.local/share/com.jcchikikomori.llmworkflow/wandavision/bin/run-wandavision.sh"],
  "enabled": true
}
```

The reminder hook (`opencode-wandavision.ts`) is loaded from the dotfiles project at `~/.config/opencode/plugins/opencode-wandavision.ts` — see `~/.local/share/com.jcchikikomori.llmworkflow/wandavision/dotfiles-migration.md` for the keep-in-sync procedure.

### 6. Verify

Claude Code:

```bash
/mcp
```

OpenCode:

```bash
# check that the wandavision MCP resources are exposed
```

Both should show `wandavision` connected, not `CONNECTION_CLOSED`. If it's closed, read the wrapper's stderr — it names the reason. Common ones: the Docker image was never built (step 1), the model cache is cold (step 3), or Docker itself isn't running.

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
- The workspace directory (`.wandavision_workspace/`) is created relative to wherever the agent was launched from (the project root) — it's project-local, not shared across projects.
- The model cache **is** shared across projects, in the Docker volume `wandavision-hf-cache` (override with `WANDAVISION_HF_CACHE_VOLUME`). Warming it once covers every project on the machine.
- Concurrent cold starts don't share work — HuggingFace takes a per-file lock, so a second container blocks on the first instead of downloading in parallel. Another reason to warm the cache serially, up front.
- Containers are named `wandavision-mcp-<hash of project dir>`. Exited leftovers get removed at startup; a running one is reaped only when no client process holds it, so a second session in the same directory won't kill the first session's server.
- Override the image name with `WANDAVISION_IMAGE`, and bypass the cold-cache preflight with `WANDAVISION_SKIP_CACHE_CHECK=1`.
- The XDG install is reusable across Claude Code and OpenCode; both agents reference the same wrapper script and skill body. The dotfiles project holds the OpenCode-side plugin copy; the Claude Code marketplace install holds its own redirector.

## Version History

### 0.4.0

- Runtime files moved out of the Claude Code marketplace plugin into the XDG data directory `~/.local/share/com.jcchikikomori.llmworkflow/wandavision/`. Both Claude Code and OpenCode now reference the same canonical home.
- New source-of-truth layout: `wandavision/` at the repo root holds `bin/`, `skill/`, `opencode-plugin/`; `./setup-wandavision.sh` copies them into XDG.
- `plugin-wandavision/` is now a thin shim — only metadata (`plugin.json`, `package.json`, `.mcp.json`, `README.md`) plus a 1-line redirector at `scripts/run-wandavision.sh` that `exec`s the XDG wrapper.
- Removed from the plugin (moved to XDG via `setup-wandavision.sh`): `hooks/`, `skills/wandavision/`, `commands/`, `plugins/opencode-wandavision.ts`, and `scripts/warm-cache.sh`.
- Generated `~/.local/share/com.jcchikikomori.llmworkflow/wandavision/dotfiles-migration.md` explaining the keep-in-sync procedure for the dotfiles project.

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
