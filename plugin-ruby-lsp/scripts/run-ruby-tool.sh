#!/usr/bin/env bash
# Docker-first, host-fallback launcher for the ruby-lsp plugin.
#
#   run-ruby-tool.sh lsp [args...]    -> ruby-lsp (LSP server, JSON-RPC on stdio)
#   run-ruby-tool.sh reek [args...]   -> reek
#
# Referenced from .lsp.json and hooks/reek_hook.py. The current working
# directory is treated as the project root.
#
# stdout belongs to the tool (JSON-RPC for the LSP, JSON for reek), so every
# diagnostic goes to stderr. stdin is never read here -- `exec` hands it to the
# tool untouched.
#
# Selection order:
#   1. Docker   -- compose file present, gem in Gemfile.lock, docker usable,
#                  service resolved. Runs `bundle exec <tool>` in the service
#                  with the project mounted at its identical host path.
#   2. Host bundle -- gem in Gemfile.lock and `bundle` on PATH.
#   3. Global binary on PATH.
#   4. Nothing found -> exit 127.
#
# Env overrides:
#   RUBY_LSP_PLUGIN_SERVICE=<name>  compose service to run in
#   RUBY_LSP_PLUGIN_FORCE_HOST=1    skip Docker entirely
#   RUBY_LSP_PLUGIN_FORCE_DOCKER=1  fail (exit 1) instead of falling back
set -euo pipefail

log() { printf '[ruby-lsp] %s\n' "$*" >&2; }

usage() {
  log "usage: run-ruby-tool.sh <lsp|reek> [args...]"
  exit 64
}

[ "$#" -ge 1 ] || usage
mode="$1"
shift

case "$mode" in
  lsp) tool='ruby-lsp' ;;
  reek) tool='reek' ;;
  *) usage ;;
esac

project_dir="$PWD"
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

compose_file() {
  local name
  for name in compose.yml compose.yaml docker-compose.yml docker-compose.yaml; do
    if [ -f "$project_dir/$name" ]; then
      printf '%s' "$name"
      return 0
    fi
  done
  return 1
}

gem_in_lockfile() {
  [ -f "$project_dir/Gemfile.lock" ] && grep -Eq "^    $tool \(" "$project_dir/Gemfile.lock"
}

resolve_service() {
  if [ -n "${RUBY_LSP_PLUGIN_SERVICE:-}" ]; then
    printf '%s' "$RUBY_LSP_PLUGIN_SERVICE"
    return 0
  fi
  local services candidate
  services="$(docker compose config --services 2>/dev/null)" || return 1
  for candidate in web app rails api backend; do
    if printf '%s\n' "$services" | grep -qx "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

# Prints the service name when Docker is usable; logs why when it is not.
docker_service() {
  if [ "${RUBY_LSP_PLUGIN_FORCE_HOST:-}" = '1' ]; then
    log 'RUBY_LSP_PLUGIN_FORCE_HOST=1, skipping Docker'
    return 1
  fi
  if ! compose_file >/dev/null; then
    log 'no compose file, using host'
    return 1
  fi
  if ! gem_in_lockfile; then
    log "$tool not in Gemfile.lock, container cannot bundle exec it"
    return 1
  fi
  if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    log 'docker compose not available, using host'
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    log 'docker daemon not reachable, using host'
    return 1
  fi
  if ! resolve_service; then
    log 'no compose service matched (web app rails api backend); set RUBY_LSP_PLUGIN_SERVICE'
    return 1
  fi
}

if service="$(docker_service)"; then
  log "running $tool in compose service '$service'"
  # -T: no TTY, raw stdio stream. stdin stays attached (compose run default).
  # --no-deps: do not boot db/redis just to lint.
  # Identical-path mounts keep LSP file URIs and reek paths valid on the host.
  exec docker compose run --rm --no-deps -T \
    -e RUBYOPT=-W0 \
    -e BUNDLE_GEMFILE="$project_dir/Gemfile" \
    -v "$project_dir:$project_dir" \
    -v "$plugin_root:$plugin_root:ro" \
    -w "$project_dir" \
    "$service" bundle exec "$tool" "$@"
fi

if [ "${RUBY_LSP_PLUGIN_FORCE_DOCKER:-}" = '1' ]; then
  log 'RUBY_LSP_PLUGIN_FORCE_DOCKER=1 but Docker is unusable, refusing host fallback'
  exit 1
fi

if gem_in_lockfile && command -v bundle >/dev/null 2>&1; then
  log "running $tool via host bundle exec"
  exec bundle exec "$tool" "$@"
fi

if command -v "$tool" >/dev/null 2>&1; then
  log "running global $tool"
  exec "$tool" "$@"
fi

log "$tool not found. Add \`gem '$tool', require: false\` to the :development group, or \`gem install $tool\`."
exit 127
