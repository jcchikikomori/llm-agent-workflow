#!/usr/bin/env bash
# MCP server entrypoint for the mempalace-docker plugin.
#
# Referenced from .mcp.json as ${CLAUDE_PLUGIN_ROOT}/scripts/run-mempalace.sh.
# .mcp.json cannot hold shell logic, hence this wrapper: it picks the CPU or
# CUDA image for THIS machine, passes the GPU flags the image actually needs,
# and mounts the current project instead of one hardcoded directory.
#
# Speaks JSON-RPC over stdio, so stdout belongs to the protocol -- all
# diagnostics go to stderr.
set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

mp_select_image
mp_add_mounts

# `-i` keeps stdin open for JSON-RPC; no `-t` (raw stream, not a terminal).
# Bare image with no command: docker-entrypoint.sh defaults to `mcp`.
mp_exec_docker run -i --rm "${MP_RUN_ARGS[@]}" "$MP_IMAGE"
