#!/usr/bin/env bash
# Shared image selection and mount assembly for the mempalace-docker plugin.
#
# Sourced by run-mempalace.sh (MCP server), bin/mempalace (CLI shim) and
# bin/mempalace-python3 (MEMPAL_PYTHON target) so all three agree on which
# image to run, which GPU flags to pass, and how host paths map into the
# container.
#
# Populates, for the caller:
#   MP_IMAGE       resolved image reference
#   MP_RUN_ARGS[]  docker run flags (GPU + mounts)
#
# Everything this file prints goes to stderr on purpose -- the MCP server's
# stdout is JSON-RPC, so a stray echo there corrupts the protocol.

MP_CPU_IMAGE="${MEMPALACE_CPU_IMAGE:-ghcr.io/mempalace/mempalace:latest}"
MP_GPU_IMAGE="${MEMPALACE_GPU_IMAGE:-mempalace:gpu}"
MP_VOLUME="${MEMPALACE_VOLUME:-mempalace-data}"

mp_log() { printf '[mempalace-docker] %s\n' "$*" >&2; }

mp_have_image() { docker image inspect "$1" >/dev/null 2>&1; }

# Docker only honours --gpus/--runtime=nvidia when the nvidia container
# runtime is actually registered. Without this check a missing
# nvidia-container-toolkit turns into a dead MCP server instead of a
# CPU fallback.
mp_has_nvidia_runtime() {
    docker info 2>/dev/null | grep -qiE '^[[:space:]]*Runtimes:.*nvidia'
}

# Discrete AND integrated AMD. The lspci vendor-id match ([1002:...]) is what
# catches APU iGPUs, which report no rocm-smi and have no /opt/rocm.
mp_detect_amd() {
    command -v rocm-smi >/dev/null 2>&1 && return 0
    command -v rocminfo >/dev/null 2>&1 && return 0
    [ -e /dev/kfd ] && return 0
    [ -d /opt/rocm ] && return 0
    if command -v lspci >/dev/null 2>&1; then
        lspci -nn 2>/dev/null \
            | grep -iE 'vga compatible|3d controller|display controller' \
            | grep -qiE 'advanced micro devices|\[1002:' && return 0
    fi
    return 1
}

# Sets MP_IMAGE and seeds MP_RUN_ARGS with GPU flags.
#
# Order matters: an explicit MEMPALACE_DOCKER_IMAGE always wins, then the
# NVIDIA path with three separate guards (each falling back to CPU with its
# own reason), then the AMD notice, then plain CPU.
mp_select_image() {
    MP_IMAGE="$MP_CPU_IMAGE"
    MP_RUN_ARGS=()

    if [ -n "${MEMPALACE_DOCKER_IMAGE:-}" ]; then
        MP_IMAGE="$MEMPALACE_DOCKER_IMAGE"
        if [ "${MEMPALACE_FORCE_GPU:-0}" = "1" ]; then
            MP_RUN_ARGS+=(--runtime=nvidia --gpus all)
        fi
        return 0
    fi

    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
        local arch
        arch="$(uname -m)"
        if [ "$arch" != "x86_64" ]; then
            mp_log "NVIDIA GPU found, but the CUDA image is x86_64-only (onnxruntime-gpu ships no aarch64 Linux wheels) and this host is ${arch}. Using CPU image ${MP_CPU_IMAGE}."
        elif ! mp_has_nvidia_runtime; then
            mp_log "NVIDIA GPU found, but Docker has no 'nvidia' runtime registered. Install nvidia-container-toolkit, or set MEMPALACE_FORCE_GPU=1 with MEMPALACE_DOCKER_IMAGE to override. Using CPU image ${MP_CPU_IMAGE}."
        elif ! mp_have_image "$MP_GPU_IMAGE"; then
            mp_log "NVIDIA GPU found, but ${MP_GPU_IMAGE} is not built locally. Upstream publishes CPU tags only -- build it with: \$CLAUDE_PLUGIN_ROOT/scripts/build-image.sh gpu. Using CPU image ${MP_CPU_IMAGE}."
        else
            MP_IMAGE="$MP_GPU_IMAGE"
            MP_RUN_ARGS+=(--runtime=nvidia --gpus all)
            return 0
        fi
    elif mp_detect_amd; then
        mp_log "AMD/ROCm-class device detected, but upstream ships no ROCm image (Dockerfile.gpu is CUDA-only). Embeddings will run on CPU via ${MP_CPU_IMAGE}."
    fi

    return 0
}

# Appends the standard mounts to MP_RUN_ARGS.
#
# /data           the palace, config and model cache (named volume, so it is
#                 shared across WSL distros and rebuilt containers)
# /work           the CURRENT project, read-only -- mining never writes to
#                 the source
# /transcripts    Claude Code session transcripts, read-only
#
# $HOME/.claude and $PWD are ALSO mounted at their identical host paths. The
# vendored hooks hand the CLI real host paths (e.g. $(dirname
# "$TRANSCRIPT_PATH")), so those paths have to resolve verbatim inside the
# container. Mounting them without touching HOME is the whole trick: HOME
# stays /data, so the palace stays in the volume, while host paths still
# work. Setting HOME to the host path is what split the palace in two under
# the previous hand-rolled shims.
mp_add_mounts() {
    local projects_dir="$HOME/.claude/projects"
    mkdir -p "$projects_dir" 2>/dev/null || true

    MP_RUN_ARGS+=(-v "${MP_VOLUME}:/data")
    MP_RUN_ARGS+=(-v "${PWD}:/work:ro")
    MP_RUN_ARGS+=(-v "${projects_dir}:/transcripts:ro")

    if [ -d "$HOME/.claude" ]; then
        MP_RUN_ARGS+=(-v "$HOME/.claude:$HOME/.claude:ro")
    fi
    # Skip when $PWD is already covered by the $HOME/.claude mount above.
    case "$PWD/" in
        "$HOME/.claude/"*) ;;
        *) MP_RUN_ARGS+=(-v "${PWD}:${PWD}:ro") ;;
    esac
}

# Honour MEMPALACE_DRY_RUN=1 by printing the argv instead of running it.
# This is the only practical way to test image selection without starting a
# real stdio server.
mp_exec_docker() {
    if [ "${MEMPALACE_DRY_RUN:-0}" = "1" ]; then
        printf 'docker'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    exec docker "$@"
}
