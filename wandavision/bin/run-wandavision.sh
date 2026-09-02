#!/usr/bin/env bash
set -euo pipefail

HF_VOLUME="${WANDAVISION_HF_CACHE_VOLUME:-wandavision-hf-cache}"
IMAGE="${WANDAVISION_IMAGE:-mcp-vision}"
MODEL_DIR="models--google--owlvit-large-patch14"

log() { printf '[wandavision] %s\n' "$*" >&2; }

GPU_ARGS=()
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  GPU_ARGS=(--runtime=nvidia --gpus all)
fi

mkdir -p "${PWD}/.wandavision_workspace"

# Cold-cache preflight. The server builds its pipeline inside the FastMCP
# lifespan, before `initialize` gets an answer, so a cold cache means minutes of
# silence — well past the client's MCP startup timeout. Failing here with a
# reason beats hanging until the client gives up and leaks a still-downloading
# container behind it.
if [ -z "${WANDAVISION_SKIP_CACHE_CHECK:-}" ]; then
  if ! docker run --rm -v "${HF_VOLUME}:/c" --entrypoint sh "${IMAGE}" -c "
        [ -d /c/hub/${MODEL_DIR}/snapshots ] || exit 1
        ls /c/hub/${MODEL_DIR}/blobs/*.incomplete >/dev/null 2>&1 && exit 1
        exit 0
      " >/dev/null 2>&1; then
    log "model cache in volume '${HF_VOLUME}' is missing or half-downloaded."
    log "google/owlvit-large-patch14 is ~1.7 GB and loads before the server answers 'initialize',"
    log "so starting now would time out instead of connecting."
    log "run this once, then reconnect with /mcp:"
    log "  \"\${CLAUDE_PLUGIN_ROOT}\"/scripts/warm-cache.sh"
    log "(set WANDAVISION_SKIP_CACHE_CHECK=1 to start anyway)"
    exit 1
  fi
fi

# Name the container per project directory so leftovers are identifiable and
# reapable. When the client's startup timeout fires it kills the `docker` CLI,
# but the container survives and keeps loading with nobody listening — so every
# timeout leaves a ~9 GB-image container behind. Reap those here.
#
# A container is only an orphan if no `docker run` process is holding it. That
# test is what separates a real orphan from a live server belonging to another
# session in this same directory, which must not be killed. Matching by prefix
# rather than exact name also catches previously suffixed orphans.
BASE="wandavision-mcp-$(printf '%s' "${PWD}" | sha256sum | cut -c1-12)"
NAME="${BASE}"

if command -v pgrep >/dev/null 2>&1; then
  while IFS= read -r stale; do
    [ -n "${stale}" ] || continue
    if ! pgrep -f "docker run .*--name ${stale}( |\$)" >/dev/null 2>&1; then
      log "reaping orphaned container ${stale} (no client attached)"
      docker rm -f "${stale}" >/dev/null 2>&1 || true
    elif [ "${stale}" = "${NAME}" ]; then
      # Live server already owns this name — take a distinct one.
      NAME="${BASE}-$$"
    fi
  done < <(docker ps --filter "name=^${BASE}" --format '{{.Names}}' 2>/dev/null)
fi

# Remove any exited leftover still holding the name.
docker rm "${NAME}" >/dev/null 2>&1 || true

exec docker run -i --rm --name "${NAME}" "${GPU_ARGS[@]}" \
  -v "${PWD}/.wandavision_workspace:/data" \
  -v "${HF_VOLUME}:/root/.cache/huggingface" \
  "${IMAGE}"
