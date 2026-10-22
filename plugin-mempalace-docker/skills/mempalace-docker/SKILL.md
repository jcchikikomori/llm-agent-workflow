---
name: mempalace-docker
description: Rules for talking to the shared Dockerized MemPalace hub — pass projects by their absolute host path (registered, mounted read-only), transcripts as /transcripts, and mine a project on demand when a search of it comes back empty. Use whenever calling any mempalace MCP tool, mining a project or conversation, or diagnosing a slow, empty, failing or disconnected mempalace call.
---

# MemPalace over Docker

One `mempalace-hub` container serves every Claude Code session on this machine
over HTTP. The model sees host paths; the hub only sees what is mounted into
it. Getting that wrong fails quietly — a mine of a path the hub cannot see
reports an error at best and files nothing at worst.

## Paths

| What you mean | Path to pass | Notes |
| ------------- | ------------ | ----- |
| A project | its absolute host path, e.g. `/home/me/Projects/app` | Must be registered (see below). Mounted read-only at that same path, so mining never writes to the source. |
| Claude Code transcripts | `/transcripts` | Read-only, this is `~/.claude/projects`. Mine with `--mode convos`. |
| The palace itself | `/data/.mempalace` | Named volume, shared across containers and WSL distros. |

Never pass `~`, `$HOME`, or a relative path. `/work` no longer exists.

## Registered projects

The hub mounts every target in `${XDG_CONFIG_HOME:-~/.config}/mempalace-docker/projects/` (a directory of
symlinks). The SessionStart hook registers the current project on its own; a whole tree is one command:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/hub.sh" register ~/Projects
```

A new registration is mounted at the hub's **next start**. The hub stops itself after
`MEMPALACE_HUB_IDLE_HOURS` (default 2) idle hours and picks the change up then; to apply it now, the user runs:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/hub.sh" restart
```

Do not mine a project the hub has not mounted yet — the SessionStart hook says which case you are in.

## Mine when a search comes back empty

If a search that should have hit this project returns nothing:

1. Mine the project by its absolute host path.
1. Retry the search once.
1. Answer.

Then record it so the SessionStart hook stops raising it:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/mark_mined.py"
```

Do not mine more than once per session, and do not mine as a reflex — an empty
result for a genuinely unrelated question is just an empty result.

## Reading a slow, failing or disconnected call

- **First call after an idle stop is a cold start.** The container boots, and the first embedding call loads the
  model (~80 MB for the default `minilm`, downloaded once per volume). Not a hung hub.
- **`mempalace` shows as failed in `/mcp`.** The hub was probably still booting when the session connected.
  `/mcp` → reconnect runs the connection helper again, which starts the hub and waits for it. Check it with:

  ```bash
  "$CLAUDE_PLUGIN_ROOT/scripts/hub.sh" status
  docker logs mempalace-hub
  ```

- **`PermissionError: [Errno 13]`** on a mounted path means the image's uid 1000 cannot read it. A `0755`
  checkout is fine, `0700` is not. Do not "fix" it with `--user` — `/data` is owned by uid 1000 inside the
  image, so another uid cannot write the palace at all.
- **Embeddings unexpectedly slow** — check whether the GPU actually attached:

  ```bash
  docker inspect mempalace-hub --format '{{.HostConfig.DeviceRequests}} {{.HostConfig.Runtime}}'
  ```

  `null runc` means a CUDA image is running on CPU. `[...] nvidia` is correct. `hub.sh` logs its image choice
  and reason to stderr when it creates the container.

## Do not assume the tool set

Check the live MCP tool list rather than guessing at tool names. The image is
pinned to whatever was pulled or built locally, which may lag upstream by
several minor versions, and the tool surface changes between them.
