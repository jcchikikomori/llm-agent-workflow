#!/usr/bin/env bash
set -euo pipefail

GPU_ARGS=()
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  GPU_ARGS=(--runtime=nvidia --gpus all)
fi

mkdir -p "${PWD}/.wandavision_workspace"

exec docker run -i --rm "${GPU_ARGS[@]}" -v "${PWD}/.wandavision_workspace:/data" mcp-vision
