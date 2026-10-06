#!/usr/bin/env bash
# Shared image selection, hub configuration and mount assembly for the
# mempalace-docker plugin.
#
# Sourced by hub.sh (the one shared hub container), bin/mempalace (CLI shim)
# and bin/mempalace-python3 (MEMPAL_PYTHON target) so all of them agree on
# which image runs, which GPU flags it gets, where the hub listens and how
# host paths map into the container.
#
# Populates, for the caller:
#   MP_IMAGE              resolved image reference
#   MP_RUN_ARGS[]         docker run flags (GPU + mounts)
#   MP_MOUNTED_TARGETS[]  registry targets that were turned into mounts
#
# Everything this file prints goes to stderr on purpose -- hub-headers.sh's
# stdout is parsed as JSON by Claude Code, so a stray echo there breaks the
# MCP connection.

MP_CPU_IMAGE="${MEMPALACE_CPU_IMAGE:-ghcr.io/mempalace/mempalace:latest}"
MP_GPU_IMAGE="${MEMPALACE_GPU_IMAGE:-mempalace:gpu}"
MP_VOLUME="${MEMPALACE_VOLUME:-mempalace-data}"

# One hub per machine. The name is also what the shims `docker exec` into.
MP_HUB_NAME="${MEMPALACE_HUB_NAME:-mempalace-hub}"
# Host-side loopback port. The container always listens on 8765 inside; the
# publish rule maps 127.0.0.1:$MP_HUB_PORT to it, so nothing off-host can reach
# the palace. Keep in sync with the ${MEMPALACE_HUB_PORT:-8765} in .mcp.json.
MP_HUB_PORT="${MEMPALACE_HUB_PORT:-8765}"
MP_HUB_INNER_PORT=8765
# Upstream's idle watchdog (MEMPALACE_MCP_IDLE_HOURS) exits the server after
# this many idle hours; the next hook or MCP connect starts it again. 0 means
# always-on, and hub.sh then also adds --restart unless-stopped.
MP_HUB_IDLE_HOURS="${MEMPALACE_HUB_IDLE_HOURS:-2}"
MP_HUB_WAIT_SECONDS="${MEMPALACE_HUB_WAIT_SECONDS:-90}"

# Same state root the Python side uses (MEMPALACE_DOCKER_STATE), so one
# override moves everything.
MP_STATE_DIR="${MEMPALACE_DOCKER_STATE:-$HOME/.claude/.mempalace-docker}"
MP_HUB_STATE_DIR="$MP_STATE_DIR/hub"
MP_HUB_TOKEN_FILE="$MP_HUB_STATE_DIR/token"
MP_HUB_ENV_FILE="$MP_HUB_STATE_DIR/env"
MP_HUB_STATE_FILE="$MP_HUB_STATE_DIR/state.json"

# The project registry: a directory of symlinks, one per project (or parent
# directory of projects). Every target is bind-mounted read-only at its
# identical host path. XDG config, namespaced to this plugin, so upstream's
# own ~/.mempalace can never collide with it and the opencode port can share it.
MP_REGISTRY_DIR="${MEMPALACE_HUB_PROJECTS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/mempalace-docker/projects}"

mp_log() { printf '[mempalace-docker] %s\n' "$*" >&2; }

mp_have_image() { docker image inspect "$1" >/dev/null 2>&1; }

# Docker only honours --gpus/--runtime=nvidia when the nvidia container
# runtime is actually registered. Without this check a missing
# nvidia-container-toolkit turns into a dead hub instead of a CPU fallback.
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

# Resolve a registry entry to the real directory it names. Symlinks are the
# normal case; a plain directory dropped in the registry counts as itself.
# `cd && pwd -P` is the portable realpath (readlink -f is GNU-only).
mp_resolve_dir() {
    (cd "$1" 2>/dev/null && pwd -P)
}

# Paths that must never become a hub mount. Mounting a host path at the same
# path inside the container overlays whatever the image has there, so `/`,
# the image's own system directories and this plugin's container paths would
# break the hub (or silently shadow the palace volume at /data).
mp_reserved_target() {
    case "$1" in
        / | /bin | /boot | /dev | /etc | /home | /lib | /lib32 | /lib64 | /media | /mnt | /opt \
            | /root | /run | /sbin | /srv | /tmp | /usr | /var) return 0 ;;
        /data | /data/* | /transcripts | /transcripts/* | /app | /app/* \
            | /proc | /proc/* | /sys | /sys/* | /dev/*) return 0 ;;
    esac
    return 1
}

# Fills MP_REGISTRY_TARGETS[] with the resolved, de-duplicated registry
# targets, sorted so the result (and the fingerprint built from it) is stable.
# A target inside another target is dropped: the parent mount already covers
# it. Dangling and reserved entries are skipped with a note, never a failure
# -- a deleted project must not keep the hub from starting.
mp_registry_targets() {
    MP_REGISTRY_TARGETS=()
    [ -d "$MP_REGISTRY_DIR" ] || return 0
    local entry resolved kept sorted=() covered
    for entry in "$MP_REGISTRY_DIR"/* "$MP_REGISTRY_DIR"/.[!.]*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        if ! resolved="$(mp_resolve_dir "$entry")"; then
            mp_log "registry entry ${entry} does not resolve to a directory; skipping it (remove it with: rm \"${entry}\")"
            continue
        fi
        if mp_reserved_target "$resolved"; then
            mp_log "registry entry ${entry} -> ${resolved} would overlay a system or container path; skipping it"
            continue
        fi
        sorted+=("$resolved")
    done
    [ ${#sorted[@]} -gt 0 ] || return 0
    # Sorted, so a parent always comes before its children. Every kept path is
    # checked, not just the previous one: `/a/b-x` sorts between `/a/b` and
    # `/a/b/c`.
    while IFS= read -r resolved; do
        [ -n "$resolved" ] || continue
        covered=0
        for kept in ${MP_REGISTRY_TARGETS[@]+"${MP_REGISTRY_TARGETS[@]}"}; do
            case "$resolved/" in
                "$kept/"*) covered=1; break ;;
            esac
        done
        [ "$covered" = 1 ] || MP_REGISTRY_TARGETS+=("$resolved")
    done < <(printf '%s\n' "${sorted[@]}" | LC_ALL=C sort -u)
}

# Appends the hub's mounts to MP_RUN_ARGS and records the registry targets it
# mounted in MP_MOUNTED_TARGETS.
#
# /data           the palace, config, model cache and the hub's serverinfo
#                 (named volume, so it is shared across WSL distros and
#                 rebuilt containers)
# /transcripts    Claude Code session transcripts, read-only
# $HOME/.claude   the same transcripts at their identical host path, because
#                 the vendored hooks hand the CLI real host paths
# <target>        every registry target at its identical host path, read-only,
#                 so `mempalace_mine <host path>` just works and mining never
#                 writes to a project
#
# HOME stays /data inside the image, so the palace stays in the volume while
# host paths still resolve verbatim. That decoupling is what ended the
# split-brain palace in 0.1.0, and a shared hub depends on it even more.
mp_add_mounts() {
    local projects_dir="$HOME/.claude/projects"
    mkdir -p "$projects_dir" 2>/dev/null || true

    MP_MOUNTED_TARGETS=()
    MP_RUN_ARGS+=(-v "${MP_VOLUME}:/data")
    MP_RUN_ARGS+=(-v "${projects_dir}:/transcripts:ro")
    if [ -d "$HOME/.claude" ]; then
        MP_RUN_ARGS+=(-v "$HOME/.claude:$HOME/.claude:ro")
    fi

    mp_registry_targets
    local target
    for target in ${MP_REGISTRY_TARGETS[@]+"${MP_REGISTRY_TARGETS[@]}"}; do
        # Already covered by the $HOME/.claude mount above.
        case "$target/" in
            "$HOME/.claude/"*) continue ;;
        esac
        MP_RUN_ARGS+=(-v "${target}:${target}:ro")
        MP_MOUNTED_TARGETS+=("$target")
    done
}

# Honour MEMPALACE_DRY_RUN=1 by printing the argv instead of running it.
# This is the only practical way to test image selection without a daemon.
mp_exec_docker() {
    if [ "${MEMPALACE_DRY_RUN:-0}" = "1" ]; then
        printf 'docker'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    exec docker "$@"
}
