# mempalace-docker

Runs [MemPalace](https://github.com/MemPalace/mempalace) entirely from Docker —
MCP server, CLI, and save hooks — with per-machine GPU selection, one shared
palace, and per-project auto-mining.

This is an **alternative** to the official `mempalace` plugin, not an add-on.
Install one or the other. See [Conflicts](#conflicts).

## Why

The hand-rolled setup this replaces had three problems worth naming:

- **A CUDA image running on CPU.** `docker run` without `--gpus` /
  `--runtime=nvidia` starts the 7 GB CUDA image with `DeviceRequests=null` and
  `Runtime=runc`. The `MEMPALACE_EMBEDDING_DEVICE=cuda` baked into the image
  does nothing, and nothing warns you.
- **A split-brain palace.** A shim that passes `-e HOME=$HOME` so host paths
  resolve *also* relocates the palace to the host, while the MCP server keeps
  writing the volume. Two palaces, neither aware of the other.
- **One project.** A hardcoded `-v /some/one/project:/workspace` means nothing
  else is mineable.

## How it works

`.mcp.json` cannot hold shell logic, so it points at a wrapper
(`scripts/run-mempalace.sh`) that decides at launch time.

### Image selection

Checked in order; the first match wins, and every fallback logs its reason to
stderr:

| Condition | Image | GPU flags |
| --------- | ----- | --------- |
| `MEMPALACE_DOCKER_IMAGE` set | that value | only with `MEMPALACE_FORCE_GPU=1` |
| NVIDIA usable + `mempalace:gpu` built | `mempalace:gpu` | `--runtime=nvidia --gpus all` |
| NVIDIA present, host not x86_64 | CPU | — |
| NVIDIA present, no `nvidia` docker runtime | CPU | — |
| NVIDIA present, `mempalace:gpu` not built | CPU | — |
| AMD / ROCm-class device | CPU | — |
| anything else | CPU | — |

CPU default is `ghcr.io/mempalace/mempalace:latest`.

**Upstream publishes CPU tags only** — `latest`, `main`, `3.4.0`…`3.9.0`. There
is no `gpu`, `cuda`, or `rocm` tag on GHCR. The CUDA variant has to be built
locally from `Dockerfile.gpu`:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/build-image.sh" gpu
```

It is x86_64-only, because `onnxruntime-gpu` publishes no aarch64 Linux wheels.

### AMD detection

Probed in order: `rocm-smi`, `rocminfo`, `/dev/kfd`, `/opt/rocm`, then `lspci`
for a VGA / 3D / display device with vendor `1002` or "Advanced Micro Devices".
The vendor-id match is what catches integrated APU iGPUs, which have neither
`rocm-smi` nor `/opt/rocm`.

Detection exists to **explain**, not to accelerate. Upstream ships no ROCm
image, so an AMD hit logs that and runs CPU. No `Dockerfile.rocm` is shipped —
`onnxruntime-rocm` is not on PyPI, and most integrated APUs are not
ROCm-supported anyway. Guessing at it would have meant shipping a build that
fails in a way harder to read than "your GPU isn't used".

### Mounts

| Container path | Host source | Mode |
| -------------- | ----------- | ---- |
| `/data` | volume `mempalace-data` | rw |
| `/work` | the current project (`$PWD`) | ro |
| `/transcripts` | `~/.claude/projects` | ro |
| `$HOME/.claude` (same path) | `~/.claude` | ro |
| `$PWD` (same path) | `$PWD` | ro |

The last two look redundant and are not. The vendored hooks hand the CLI real
host paths, so those have to resolve verbatim inside the container — while
`HOME` stays `/data` so the palace stays in the volume. Decoupling path
resolution from `HOME` is what ends the split-brain.

Overrides: `MEMPALACE_VOLUME`, `MEMPALACE_CPU_IMAGE`, `MEMPALACE_GPU_IMAGE`.

### Auto-mining

A `SessionStart` hook checks a per-project stamp under
`~/.claude/.mempalace-docker/projects/` and asks Claude to mine `/work` when
the project has never been mined, `HEAD` has moved, or the stamp is older than
`MEMPALACE_MINE_MAX_AGE_DAYS` (default 7). The skill adds the lazy half: a
project search that comes back empty triggers a mine, then one retry.

Stamps live outside the repo, so a mined project stays mined across worktrees
and never shows up in `git status`.

### Save hooks

`Stop`, `PreCompact`, and `SessionEnd` run MemPalace's own hook scripts,
vendored **unmodified** under `hooks/vendor/` and redirected into the container
purely through the `MEMPAL_PYTHON` and `PATH` overrides they already support.
See `hooks/vendor/README.md`.

## Setup

### 1. Install

```bash
/plugin install mempalace-docker@llm-agent-workflow
/reload-plugins
```

### 2. Build the GPU image (optional, NVIDIA + x86_64 only)

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/build-image.sh" gpu
```

Skip it to run CPU. Requires `nvidia-container-toolkit` for the runtime to
register.

### 3. Verify

```bash
/mcp
```

`mempalace` should read **connected**. Then confirm the GPU actually attached:

```bash
docker inspect "$(docker ps -q --filter ancestor=mempalace:gpu | head -1)" \
  --format '{{.HostConfig.DeviceRequests}} {{.HostConfig.Runtime}}'
```

`null runc` means CPU. `[...] nvidia` is correct.

Dry-run the selection logic without starting a server:

```bash
MEMPALACE_DRY_RUN=1 "$CLAUDE_PLUGIN_ROOT/scripts/run-mempalace.sh"
```

## Conflicts

The `SessionStart` hook reports these once per session, with the fix for each.
It never edits your settings.

| Conflict | Why it breaks | Fix |
| -------- | ------------- | --- |
| Official `mempalace` plugin enabled | Registers a second MCP server also named `mempalace`; which one answers a tool call is undefined | `/plugin uninstall mempalace@mempalace` |
| `mcpServers.mempalace` in `~/.claude.json` | Same name collision, plus wrong mounts and no GPU flags | Remove that one entry |
| `~/.local/bin/mempalace{,-python3}` | Shadow this plugin's shims on `PATH` and write the host palace | Remove or rename |
| mempalace hooks in `settings.json` / `settings.local.json` | Double-fire alongside this plugin's, and their paths break once the official plugin is gone | Remove those entries |

The server name is deliberately `mempalace`, not `mempalace-docker`, so tool
names stay `mcp__mempalace__*` and existing prompts keep working. That is
exactly why the duplicates have to go.

Silence the warning for good:

```bash
touch ~/.claude/.mempalace-docker/conflicts-dismissed
```

## Consolidating a split palace

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/migrate-host-palace.sh"          # report only
"$CLAUDE_PLUGIN_ROOT/scripts/migrate-host-palace.sh" --yes    # re-mine
```

Report-only prints `mempalace status` for both palaces side by side so the
decision is evidence-based. Then:

- `--strategy remine` (default) — the volume stays canonical and sources are
  re-mined into it. Additive.
- `--strategy replace` — overwrite the volume palace from the host copy. The
  displaced palace is kept in-volume at `/data/.mempalace.replaced`.

Both back the volume up to `~/.mempalace-backups/` first, and neither ever
deletes `~/.mempalace`.

Why re-mine instead of merge: the CLI has no export, import, or merge verb
(`migrate` is ChromaDB-version migration only). A palace is a Chroma
collection plus `knowledge_graph.sqlite3`; those do not union by copying files.

## Requirements

- Docker, with the daemon running
- Python 3 (hooks)
- For GPU: NVIDIA driver, `nvidia-container-toolkit`, x86_64, and a locally
  built `mempalace:gpu`
- First call on a cold volume downloads the embedding model (~80 MB default) —
  slow and network-dependent, once per volume
- Linux bind mounts must be readable by uid 1000; a `0755` checkout is fine,
  `0700` is not. Do not work around it with `--user` — `/data` is owned by uid
  1000 inside the image
- Each hook invocation starts a container. On the CUDA image that is a few
  seconds of startup, which is why the `Stop` / `PreCompact` timeouts here are
  higher than a native install needs

## License & Attribution

MIT. This plugin vendors MIT-licensed code from MemPalace, copyright (c) 2026
MemPalace Contributors. Full attribution, vendored-file list, source commit,
and upstream license text are in [`NOTICE`](./NOTICE).

## Version History

### 1.0.0

- Major release for repository rename to `llm-agent-workflow`
- Updated install target to `mempalace-docker@llm-agent-workflow`
- No runtime or hook behavior changes

### 0.1.0

- Initial implementation
- `.mcp.json` + `scripts/run-mempalace.sh` wrapper: image selection with
  NVIDIA and AMD detection, GPU flags, per-project `/work` mount
- `scripts/lib/common.sh` shared by the MCP wrapper and both CLI shims so all
  three agree on image and mounts
- `scripts/bin/mempalace` and `scripts/bin/mempalace-python3`: containerized
  CLI and interpreter that keep `HOME=/data`, ending the host/volume
  split-brain
- `mempalace-python3` attaches container stdin only for non-`-c` invocations —
  `docker run -i` drains the host's stdin even when the container never reads
  it, and the hooks' pre-`$(cat)` `auto_save` probe was swallowing the payload
  (see `hooks/vendor/README.md`, "The stdin trap")
- `scripts/build-image.sh`: builds `mempalace:gpu` from a pinned upstream ref
  (upstream publishes no GPU tag)
- `scripts/migrate-host-palace.sh`: pre-flight comparison, mandatory volume
  backup, `remine` and `replace` strategies
- `SessionStart` hook: four-way conflict scan plus per-project auto-mine
  prompting, with stamps under `~/.claude/.mempalace-docker/`
- `Stop` / `PreCompact` / `SessionEnd` via MemPalace's own hook scripts,
  vendored unmodified and redirected through `MEMPAL_PYTHON` / `PATH`
- `mempalace-docker` skill: container-path translation and lazy mine-on-empty
- `NOTICE` + `hooks/vendor/README.md` for MIT attribution and provenance
