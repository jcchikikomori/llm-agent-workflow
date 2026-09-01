#!/usr/bin/env bash
# Build a mempalace image locally.
#
#   build-image.sh gpu    -> mempalace:gpu  (CUDA, x86_64 only, ~7 GB)
#   build-image.sh cpu    -> mempalace:cpu  (only useful for a custom EXTRAS
#                                            build; otherwise just pull
#                                            ghcr.io/mempalace/mempalace:latest)
#
# Why this exists: upstream publishes CPU tags ONLY (latest, main, 3.x). There
# is no gpu/cuda tag on GHCR, so the CUDA variant has to be built from
# Dockerfile.gpu on the machine that will run it.
set -euo pipefail

VARIANT="${1:-gpu}"
REF="${MEMPALACE_SRC_REF:-main}"
EXTRAS="${MEMPALACE_EXTRAS:-}"
DEFAULT_SRC="${XDG_DATA_HOME:-$HOME/.local/share}/mempalace-src"
SRC="${MEMPALACE_SRC:-$DEFAULT_SRC}"

die() { printf 'build-image.sh: %s\n' "$*" >&2; exit 1; }
info() { printf '[build-image] %s\n' "$*" >&2; }

case "$VARIANT" in
    gpu|cpu) ;;
    *) die "unknown variant '$VARIANT' (expected 'gpu' or 'cpu')" ;;
esac

# onnxruntime-gpu publishes no aarch64 Linux wheels, so the CUDA build fails
# on ARM with an opaque dependency-resolution error. Fail early and say why.
if [ "$VARIANT" = "gpu" ] && [ "$(uname -m)" != "x86_64" ]; then
    die "the CUDA image is x86_64-only (onnxruntime-gpu has no aarch64 Linux wheels); this host is $(uname -m)"
fi

command -v docker >/dev/null 2>&1 || die "docker not found on PATH"

if [ -d "$SRC/.git" ]; then
    info "using existing checkout: $SRC"
else
    [ -e "$SRC" ] && die "$SRC exists but is not a git checkout; remove it or set MEMPALACE_SRC"
    info "cloning MemPalace into $SRC"
    git clone --depth 50 https://github.com/MemPalace/mempalace.git "$SRC"
fi

# The upstream default branch is `develop`. Building from whatever happens to
# be checked out is how you end up running an unreleased palace, so pin the
# ref explicitly (default: main, the release branch).
info "checking out ref: $REF"
git -C "$SRC" fetch --depth 50 origin "$REF"
git -C "$SRC" checkout -q FETCH_HEAD

BUILD_ARGS=()
[ -n "$EXTRAS" ] && BUILD_ARGS+=(--build-arg "EXTRAS=$EXTRAS")

if [ "$VARIANT" = "gpu" ]; then
    info "building mempalace:gpu from Dockerfile.gpu (this is a large image)"
    docker build -f "$SRC/Dockerfile.gpu" "${BUILD_ARGS[@]}" -t mempalace:gpu "$SRC"
    info "done. run-mempalace.sh will now pick mempalace:gpu automatically."
else
    info "building mempalace:cpu from Dockerfile"
    docker build -f "$SRC/Dockerfile" "${BUILD_ARGS[@]}" -t mempalace:cpu "$SRC"
    info "done. set MEMPALACE_CPU_IMAGE=mempalace:cpu to use it."
fi

info "built from $(git -C "$SRC" rev-parse --short HEAD) ($REF)"
