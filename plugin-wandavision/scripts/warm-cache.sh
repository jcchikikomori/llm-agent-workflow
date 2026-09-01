#!/usr/bin/env bash
# Pre-download the object detection model into the shared cache volume.
#
# The MCP server loads its pipeline inside the FastMCP lifespan, before it
# answers `initialize`. On a cold cache that means ~1.7 GB of downloads with
# the client sitting there waiting, which blows the MCP startup timeout long
# before the server ever speaks. Downloading here, outside the MCP handshake,
# is what keeps startup inside that budget.
set -euo pipefail

HF_VOLUME="${WANDAVISION_HF_CACHE_VOLUME:-wandavision-hf-cache}"
IMAGE="${WANDAVISION_IMAGE:-mcp-vision}"

GPU_ARGS=()
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  GPU_ARGS=(--runtime=nvidia --gpus all)
fi

echo "[wandavision] warming cache volume '${HF_VOLUME}' using image '${IMAGE}'"
echo "[wandavision] google/owlvit-large-patch14 is ~1.7 GB — first run takes a few minutes"

# Calls the server's own initializer, so the cached file set is exactly what the
# server asks for at startup. Guessing the file list here would risk caching
# safetensors while the server reaches for the .bin, leaving it to download
# again on the next connect.
exec docker run --rm "${GPU_ARGS[@]}" \
  -v "${HF_VOLUME}:/root/.cache/huggingface" \
  --entrypoint /app/.venv/bin/python3 \
  "${IMAGE}" -c '
import time
from mcp_vision.server import init_objdet_pipeline

start = time.time()
init_objdet_pipeline()
print(f"[wandavision] pipeline ready in {time.time() - start:.1f}s", flush=True)
' </dev/null
