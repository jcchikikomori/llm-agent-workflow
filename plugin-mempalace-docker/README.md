# mempalace-docker

Runs [MemPalace](https://github.com/MemPalace/mempalace) entirely from Docker as **one shared hub** for every
Claude Code session on the machine: MCP server, CLI, and save hooks, with per-machine GPU selection, one palace,
and per-project auto-mining.

This is an **alternative** to the official `mempalace` plugin, not an add-on.
Install one or the other. See [Conflicts](#conflicts).

## Why

The hand-rolled setup this replaced in 0.1.0 had three problems worth naming:

- **A CUDA image running on CPU.** `docker run` without `--gpus` /
  `--runtime=nvidia` starts the 7 GB CUDA image with `DeviceRequests=null` and
  `Runtime=runc`. The `MEMPALACE_EMBEDDING_DEVICE=cuda` baked into the image
  does nothing, and nothing warns you.
- **A split-brain palace.** A shim that passes `-e HOME=$HOME` so host paths
  resolve _also_ relocates the palace to the host, while the MCP server keeps
  writing the volume. Two palaces, neither aware of the other.
- **One project.** A hardcoded `-v /some/one/project:/workspace` means nothing
  else is mineable.

Then 1.x had its own two, which is what 2.0.0 fixes:

- **A server per session.** Every session started its own container, each loading chromadb, the embedding model
  and (on the CUDA image) its own CUDA context. Every `Stop` hook started two or three more. Four sessions open
  meant four copies of all of it.
- **Refused saves.** MemPalace lets one process hold the palace writer lease. With a server per session, the hook
  mines lost that race all the time. On the machine this was built on, 327 of 854 logged hook mines ended in
  `palace /data/.mempalace/palace is held by PID 1 (/app/.venv/bin/mempalace-mcp)`.

Upstream already has the answer: `mempalace serve` runs a long-lived HTTP **hub** that owns the lease, and the CLI
forwards `mine` to a live hub instead of fighting it. This plugin runs exactly one of those.

## How it works

```text
Claude Code session --HTTP + bearer token--> 127.0.0.1:8765 --> container mempalace-hub (mempalace serve)
save hooks --docker exec--> mempalace-hub --(CLI forwards mine over HTTP)--> same hub
```

- `scripts/hub.sh` owns the `mempalace-hub` container. It creates it on first use, starts it again after an idle
  stop, and recreates it when its config changed.
- `.mcp.json` is an `http` server at `http://127.0.0.1:${MEMPALACE_HUB_PORT:-8765}/mcp`. Its `headersHelper`
  (`scripts/hub-headers.sh`) runs on every connect and reconnect: it starts the hub if needed, waits a few seconds
  for `/healthz`, and prints the `Authorization` header.
- The CLI shims in `scripts/bin/` `docker exec` into the hub instead of starting a container per call. Inside the
  hub the CLI finds the hub's own record under `/data/.mempalace/server/` and forwards `mine` to it.
- The server name stays `mempalace`, so tool names do not change.

### Image selection

Checked in order when the hub container is created; the first match wins, and every fallback logs its reason to
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

The hub needs MemPalace **3.9.0 or later** (the first release whose CLI forwards mines to a hub).

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

### Mounts and the project registry

| Container path | Host source | Mode |
| -------------- | ----------- | ---- |
| `/data` | volume `mempalace-data` | rw |
| `/transcripts` | `~/.claude/projects` | ro |
| `$HOME/.claude` (same path) | `~/.claude` | ro |
| each registry target (same path) | the target | ro |

A per-session server could mount "the current project". A shared hub can't, so projects are **registered**. The
registry is a directory of symlinks:

```text
${XDG_CONFIG_HOME:-~/.config}/mempalace-docker/projects/
```

Every symlink target is mounted read-only at its identical host path, so a project is mined by its real path and
mining never writes to it. The `SessionStart` hook registers the current git checkout for you (only inside
`$HOME`, never `$HOME` itself or anything under `~/.claude`). To register a whole tree at once:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/hub.sh" register ~/Projects
```

A mount can only be added when the container is created, so a new registration shows up at the hub's **next
start**. The idle stop does that on its own; `hub.sh restart` does it now. Dangling symlinks are skipped with a
note, a target inside another target is covered by its parent, and `/`, the image's system directories, `/data`,
`/transcripts` and `/app` are refused.

`HOME` stays `/data` inside the container, so the palace stays in the volume while host paths still resolve.
Decoupling path resolution from `HOME` is what ended the split-brain in 0.1.0.

Overrides: `MEMPALACE_HUB_PROJECTS_DIR`, `MEMPALACE_VOLUME`, `MEMPALACE_CPU_IMAGE`, `MEMPALACE_GPU_IMAGE`.

### Lifecycle

- **Stop when idle** (default). Upstream's idle watchdog exits the server after `MEMPALACE_HUB_IDLE_HOURS`
  (default `2`) hours without a request. The next hook, shim call or MCP connect starts it again.
- **Always on.** `MEMPALACE_HUB_IDLE_HOURS=0` disables the watchdog and adds `--restart unless-stopped`, so the
  hub also comes back after a reboot.
- **Config changes.** The registry, image, port and idle hours go into a fingerprint label. A stopped hub with an
  old fingerprint is recreated; a running one keeps serving and the `SessionStart` hook mentions it once.

### The token

A published port means the server binds `0.0.0.0` inside the container, and MemPalace refuses a non-loopback bind
without `MEMPALACE_MCP_HTTP_TOKEN`. `hub.sh` mints one token per machine, stores it `0600` at
`~/.claude/.mempalace-docker/hub/token`, and hands it to the container through a `0600` `--env-file`, never on
argv. The port itself is published on `127.0.0.1` only.

### Auto-mining

A `SessionStart` hook checks a per-project stamp under `~/.claude/.mempalace-docker/projects/` and asks Claude to
mine the project's host path when it has never been mined, `HEAD` has moved, or the stamp is older than
`MEMPALACE_MINE_MAX_AGE_DAYS` (default 7). When the running hub was started without that path, it asks for a hub
restart instead of a mine that would fail. The skill adds the lazy half: a project search that comes back empty
triggers a mine, then one retry.

Stamps live outside the repo, so a mined project stays mined across worktrees and never shows up in `git status`.

### Save hooks

`Stop`, `PreCompact`, and `SessionEnd` run MemPalace's own hook scripts,
vendored **unmodified** under `hooks/vendor/` and redirected into the hub
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
"$CLAUDE_PLUGIN_ROOT/scripts/hub.sh" status
docker logs mempalace-hub
```

`status` should read `running` / `healthy`, and the log should contain
`MemPalace MCP HTTP server listening on http://0.0.0.0:8765/mcp`. In Claude Code, `/mcp` should show `mempalace`
connected. Confirm the GPU actually attached:

```bash
docker inspect mempalace-hub --format '{{.HostConfig.DeviceRequests}} {{.HostConfig.Runtime}}'
```

`null runc` means CPU. `[...] nvidia` is correct.

Print the exact `docker run` without creating anything:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/hub.sh" print-run
```

## Commands

| Command | What it does |
| ------- | ------------ |
| `hub.sh status` | Container state, health, endpoint and registered projects; exit 0 only when running |
| `hub.sh start` | Start (or create) the hub and wait for `/healthz` |
| `hub.sh stop` | Stop it; the next use starts it again |
| `hub.sh restart` | Recreate it with the current config (new registrations, image, port) |
| `hub.sh rm` | Remove the container; the palace volume is untouched |
| `hub.sh register <dir>` | Add a project, or a parent directory of projects |
| `hub.sh logs [args]` | `docker logs` for the hub |
| `hub.sh print-run` | Print the `docker run` it would use |

## Settings

| Variable | Default | Effect |
| -------- | ------- | ------ |
| `MEMPALACE_HUB_IDLE_HOURS` | `2` | Idle hours before the hub exits; `0` = always on |
| `MEMPALACE_HUB_PORT` | `8765` | Loopback port; set it where Claude Code starts too, since `.mcp.json` reads it |
| `MEMPALACE_HUB_NAME` | `mempalace-hub` | Container name |
| `MEMPALACE_HUB_MEMORY` | unset | `--memory` for the hub |
| `MEMPALACE_HUB_PROJECTS_DIR` | XDG path above | The project registry |
| `MEMPALACE_HUB_WAIT_SECONDS` | `90` | How long the CLI shim and `hub.sh start` wait for health |
| `MEMPALACE_HUB_HELPER_WAIT_SECONDS` | `8` | How long the connect helper waits (Claude Code allows it 10 s) |
| `MEMPALACE_HUB_AUTOSTART` | `1` | `0` stops the `SessionStart` hook from starting the hub |

With the `resource-guard` plugin's shim on `PATH`, the hub gets that plugin's `server_caps` memory cap
(`*mempalace*` is `3g` by default) and is never paused. `MEMPALACE_HUB_MEMORY` wins over it.

## Known limits

- **The first connect after an idle stop can fail.** The connect helper waits up to 8 seconds, and a cold CUDA
  start can take longer. Claude Code retries a refused connection, and `/mcp` → reconnect runs the helper again.
  Set `MEMPALACE_HUB_IDLE_HOURS=0` if this happens often.
- **New registrations need a restart.** Docker cannot add a mount to a running container.
- **Drawers mined by 1.x are labelled `/work/...`.** They stay searchable. New mines use host paths, so the same
  file can appear twice until the old drawers are deleted by source.

## Conflicts

The `SessionStart` hook reports these once per session, with the fix for each.
It never edits your settings.

| Conflict | Why it breaks | Fix |
| -------- | ------------- | --- |
| Official `mempalace` plugin enabled | Registers a second MCP server also named `mempalace`; which one answers a tool call is undefined | `/plugin uninstall mempalace@mempalace` |
| `mcpServers.mempalace` in `~/.claude.json` | Same name collision, plus a second palace writer the hub refuses | Remove that one entry |
| `~/.local/bin/mempalace{,-python3}` | Shadow this plugin's shims on `PATH` and write the host palace | Remove or rename |
| mempalace hooks in `settings.json` / `settings.local.json` | Double-fire alongside this plugin's, and their paths break once the official plugin is gone | Remove those entries |

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
  re-mined into it through the hub. Additive. A `--project` must be registered.
- `--strategy replace` — overwrite the volume palace from the host copy, with the hub stopped around the copy.
  The displaced palace is kept in-volume at `/data/.mempalace.replaced`.

Both back the volume up to `~/.mempalace-backups/` first, and neither ever
deletes `~/.mempalace`.

Why re-mine instead of merge: the CLI has no export, import, or merge verb
(`migrate` is ChromaDB-version migration only). A palace is a Chroma
collection plus `knowledge_graph.sqlite3`; those do not union by copying files.

## Requirements

- Docker 25 or later, with the daemon running (`--health-start-interval`)
- Python 3 (hooks, token generation)
- MemPalace image 3.9.0 or later
- For GPU: NVIDIA driver, `nvidia-container-toolkit`, x86_64, and a locally
  built `mempalace:gpu`
- First embedding call on a cold volume downloads the model (~80 MB default) —
  slow and network-dependent, once per volume
- Registered directories must be readable by uid 1000; a `0755` checkout is fine,
  `0700` is not. Do not work around it with `--user` — `/data` is owned by uid
  1000 inside the image

## Tests

```bash
python3 -m unittest discover -s plugin-mempalace-docker/tests
```

The tests use a stub `docker` on `PATH`, so they never start a container.

## License & Attribution

MIT. This plugin vendors MIT-licensed code from MemPalace, copyright (c) 2026
MemPalace Contributors. Full attribution, vendored-file list, source commit,
and upstream license text are in [`NOTICE`](./NOTICE).

## Version History

### 2.0.0

Breaking: one shared hub replaces the per-session server.

- `scripts/hub.sh`: one `mempalace-hub` container per machine running `mempalace serve` on `127.0.0.1:8765`, with
  a health check, a config fingerprint, an idle stop (`MEMPALACE_HUB_IDLE_HOURS`, default 2) and `status`,
  `start`, `stop`, `restart`, `rm`, `register`, `logs`, `print-run`
- `.mcp.json` is now an `http` server; `scripts/hub-headers.sh` starts the hub on connect and adds the bearer token
- `scripts/run-mempalace.sh` removed; sessions no longer start their own container
- CLI and `MEMPAL_PYTHON` shims `docker exec` into the hub instead of `docker run --rm` per call, so hook mines are
  forwarded to the hub rather than refused by the writer lease
- `/work` removed: projects are mounted from a symlink registry under
  `${XDG_CONFIG_HOME:-~/.config}/mempalace-docker/projects/` at their host paths, and mined by host path
- `SessionStart` registers the current git checkout, starts the hub, and asks for a restart when the hub has not
  mounted the project yet
- `mark_mined.py --report [--tool-name NAME]` prints the mine prompt without writing a stamp
- `migrate-host-palace.sh` runs through the hub and stops it around `--strategy replace`
- First test suite for the plugin (`tests/`, stub `docker`, no containers)

Migrating from 1.x:

1. Update the plugin, then `/reload-plugins` (or restart) **every** open session. A 1.x session's own server holds
   the palace writer lease, and the hub refuses to start while it does.
1. Open a project; the `SessionStart` hook registers it and starts the hub.
1. Optionally register a whole tree with `hub.sh register ~/Projects`, then `hub.sh restart`.

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
