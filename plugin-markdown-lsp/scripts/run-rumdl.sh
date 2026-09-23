#!/usr/bin/env bash
# Docker-first, native-binary-fallback launcher for the markdown-lsp plugin.
#
#   run-rumdl.sh server       -> rumdl LSP server (JSON-RPC on stdio)
#   run-rumdl.sh check FILE   -> any other rumdl subcommand, same selection
#
# Referenced from .lsp.json. The current working directory is treated as the
# project root.
#
# stdout belongs to rumdl (JSON-RPC for the LSP), so every diagnostic goes to
# stderr. stdin is never read here -- `exec` hands it to rumdl untouched.
#
# Config selection:
#   1. MARKDOWN_LSP_PLUGIN_CONFIG=<path>  always wins.
#   2. Project has its own rumdl/markdownlint config (searched upward to the
#      .git boundary, like rumdl itself) -> no --config, rumdl discovers it.
#   3. Otherwise the bundled config/rumdl.toml.
#
# Runtime selection:
#   1. Docker image, when the daemon is reachable. Same order as the ruby-lsp
#      plugin: one pinned-by-tag runtime, nothing to install on the host.
#   2. Host `rumdl` on PATH.
#   3. `uvx rumdl`, then `npx --yes rumdl`.
#   4. Nothing found -> exit 127.
#
# Env overrides:
#   MARKDOWN_LSP_PLUGIN_CONFIG=<path>  explicit rumdl config file
#   MARKDOWN_LSP_PLUGIN_IMAGE=<ref>    image (default ghcr.io/rvben/rumdl:latest)
#   MARKDOWN_LSP_PLUGIN_FORCE_HOST=1   skip Docker entirely
#   MARKDOWN_LSP_PLUGIN_FORCE_DOCKER=1 fail (exit 1) instead of falling back
set -euo pipefail

log() { printf '[markdown-lsp] %s\n' "$*" >&2; }

if [ "$#" -lt 1 ]; then
  log 'usage: run-rumdl.sh <server|rumdl-subcommand> [args...]'
  exit 64
fi

project_dir="$PWD"
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
image="${MARKDOWN_LSP_PLUGIN_IMAGE:-ghcr.io/rvben/rumdl:latest}"

# True when the directory holds a config rumdl would discover on its own.
dir_has_config() {
  local dir="$1" name
  for name in .rumdl.toml rumdl.toml .config/rumdl.toml \
    .markdownlint.json .markdownlint.jsonc .markdownlint.yaml .markdownlint.yml \
    markdownlint.json markdownlint.yaml; do
    [ -f "$dir/$name" ] && return 0
  done
  [ -f "$dir/pyproject.toml" ] && grep -q '^\[tool\.rumdl' "$dir/pyproject.toml"
}

project_has_config() {
  local dir="$project_dir"
  while :; do
    dir_has_config "$dir" && return 0
    # rumdl stops at the repository boundary; so does this search.
    [ -d "$dir/.git" ] && return 1
    [ "$dir" = / ] && return 1
    dir="$(dirname "$dir")"
  done
}

config_args=()
if [ -n "${MARKDOWN_LSP_PLUGIN_CONFIG:-}" ]; then
  log "using config $MARKDOWN_LSP_PLUGIN_CONFIG"
  config_args=(--config "$MARKDOWN_LSP_PLUGIN_CONFIG")
elif project_has_config; then
  log 'project config found, letting rumdl discover it'
else
  config_args=(--config "$plugin_root/config/rumdl.toml")
fi

# ${arr[@]+...} keeps `set -u` quiet on bash 3.2 when the array is empty.
run_args=(${config_args[@]+"${config_args[@]}"} "$@")

docker_usable() {
  if [ "${MARKDOWN_LSP_PLUGIN_FORCE_HOST:-}" = '1' ]; then
    log 'MARKDOWN_LSP_PLUGIN_FORCE_HOST=1, skipping Docker'
    return 1
  fi
  if ! command -v docker >/dev/null 2>&1; then
    log 'docker not on PATH'
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    log 'docker daemon not reachable'
    return 1
  fi
}

run_docker() {
  log "running rumdl in $image"
  # Identical-path mounts keep LSP file URIs and the bundled config path valid.
  # When the plugin lives inside the project (developing this repo), a second
  # read-only mount would shadow that subtree and silently break `fmt` writes.
  local mounts=(-v "$project_dir:$project_dir")
  case "$plugin_root/" in
    "$project_dir"/*) ;;
    *) mounts+=(-v "$plugin_root:$plugin_root:ro") ;;
  esac
  # -i: keep stdin open for JSON-RPC. No -t: a TTY would mangle the stream.
  # --user: cache files land with the caller's ownership.
  exec docker run --rm -i \
    --user "$(id -u):$(id -g)" \
    "${mounts[@]}" \
    -w "$project_dir" \
    "$image" "${run_args[@]}"
}

if docker_usable; then
  run_docker
fi

if [ "${MARKDOWN_LSP_PLUGIN_FORCE_DOCKER:-}" = '1' ]; then
  log 'MARKDOWN_LSP_PLUGIN_FORCE_DOCKER=1 but Docker is unusable, refusing host fallback'
  exit 1
fi

if command -v rumdl >/dev/null 2>&1; then
  log 'running host rumdl'
  exec rumdl "${run_args[@]}"
fi

if command -v uvx >/dev/null 2>&1; then
  log 'running rumdl via uvx'
  exec uvx rumdl "${run_args[@]}"
fi

if command -v npx >/dev/null 2>&1; then
  log 'running rumdl via npx'
  exec npx --yes rumdl "${run_args[@]}"
fi

log 'Docker unusable and rumdl not found. Start Docker, or install one of: brew install rumdl | uv tool install rumdl | pip install rumdl | npm install -g rumdl | cargo install rumdl'
exit 127
