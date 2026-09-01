---
name: mempalace-docker
description: Rules for talking to the Dockerized MemPalace MCP server — translate every host path to its container path (/work, /transcripts, /data), and mine a project on demand when a search of it comes back empty. Use whenever calling any mcp__mempalace__* tool, mining a project or conversation, or diagnosing a slow, empty, or failing mempalace call.
---

# MemPalace over Docker

The `mempalace` MCP server runs inside a container. The model sees host paths;
the server only sees what is mounted. Getting that translation wrong is the
single most common failure, and it fails quietly — a mine of a path the
container cannot see reports success and files nothing.

## Path translation is mandatory

| What you mean | Path to pass | Notes |
| ------------- | ------------ | ----- |
| The current project | `/work` | Read-only. Mining never writes to the source. |
| Claude Code transcripts | `/transcripts` | Read-only, this is `~/.claude/projects`. Mine with `--mode convos`. |
| The palace itself | `/data/.mempalace` | Named volume, shared across containers and WSL distros. |

Never pass `~`, `$HOME`, or an absolute host path to a mempalace tool.

One exception, and only inside a hook: paths under `~/.claude` and the current
project are *also* mounted at their identical host paths, because the vendored
upstream hooks hand the CLI real host paths. That exists for the hooks, not for
you — when you call a tool, use the table above.

## Mine when a search comes back empty

If a search that should have hit this project returns nothing:

1. Mine `/work`.
2. Retry the search once.
3. Answer.

Then record it so the SessionStart hook stops raising it:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/mark_mined.py"
```

Do not mine more than once per session, and do not mine as a reflex — an empty
result for a genuinely unrelated question is just an empty result.

## Reading a slow or failing call

- **First call on a cold volume is slow and needs network.** It downloads the
  embedding model (~80 MB for the default `minilm`, ~300 MB for
  `embeddinggemma`) into `/data`. One-off per volume. Not a hung container.
- **`PermissionError: [Errno 13]`** on a mounted path means the image's uid
  1000 cannot read it. A `0755` checkout is fine, `0700` is not. Do not
  "fix" it with `--user` — `/data` is owned by uid 1000 inside the image, so
  another uid cannot write the palace at all.
- **Embeddings unexpectedly slow** — check whether the GPU actually attached:

  ```bash
  docker inspect "$(docker ps -q --filter ancestor=mempalace:gpu | head -1)" \
    --format '{{.HostConfig.DeviceRequests}} {{.HostConfig.Runtime}}'
  ```

  `null runc` means a CUDA image is running on CPU. `[...] nvidia` is correct.
  The wrapper logs its image choice and reason to stderr on startup.

## Do not assume the tool set

Check the live MCP tool list rather than guessing at tool names. The image is
pinned to whatever was pulled or built locally, which may lag upstream by
several minor versions, and the tool surface changes between them.
