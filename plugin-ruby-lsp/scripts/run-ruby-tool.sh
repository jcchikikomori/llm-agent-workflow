#!/usr/bin/env bash
# Docker-first, host-fallback launcher for the ruby-lsp plugin.
#
#   run-ruby-tool.sh lsp [args...]          -> lsp_bridge.py: the session's end of the
#                                             shared, lazy, idle-stopping ruby-lsp hub
#   run-ruby-tool.sh lsp-backend [args...]  -> ruby-lsp itself (JSON-RPC on stdio);
#                                             only lsp_hub.py calls this
#   run-ruby-tool.sh reek [args...]         -> reek
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
#   RUBY_LSP_PLUGIN_HUB_CONTAINER   set by lsp_hub.py: names the lsp-backend
#                                   container so Reek can reuse it
set -euo pipefail

log() { printf '[ruby-lsp] %s\n' "$*" >&2; }

usage() {
  log "usage: run-ruby-tool.sh <lsp|lsp-backend|reek> [args...]"
  exit 64
}

[ "$#" -ge 1 ] || usage
mode="$1"
shift

project_dir="$PWD"
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

case "$mode" in
  lsp)
    if command -v python3 >/dev/null 2>&1; then
      exec python3 "$plugin_root/scripts/lsp_bridge.py" "$@"
    fi
    log 'python3 not found, running ruby-lsp directly (not shared, no idle stop)'
    tool='ruby-lsp'
    ;;
  lsp-backend) tool='ruby-lsp' ;;
  reek) tool='reek' ;;
  *) usage ;;
esac

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

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d' ' -f1
  else
    python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
  fi
}

# Must match container_name() in lsp_hub.py: the hub's backend runs under this name.
hub_container() {
  printf 'ruby-lsp-%s' "$(printf '%s' "$(pwd -P)" | sha256 | cut -c1-12)"
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

# Reek reuses the hub's warm ruby-lsp container when one runs for this checkout:
# no container start per edit. The same bundle serves both gems. The container
# must also mount this plugin root, where the bundled .reek.yml lives; one
# started by an older plugin version does not.
if [ "$mode" = reek ] && [ "${RUBY_LSP_PLUGIN_FORCE_HOST:-}" != '1' ] &&
  gem_in_lockfile && command -v docker >/dev/null 2>&1; then
  container="$(hub_container)"
  state="$(docker inspect -f '{{.State.Running}} {{range .Mounts}}{{.Destination}} {{end}}' "$container" 2>/dev/null || true)"
  case "$state" in
    "true"*" $plugin_root "*)
      log "running reek in the hub's container '$container'"
      exec docker exec -w "$project_dir" "$container" bundle exec reek "$@"
      ;;
  esac
fi

if service="$(docker_service)"; then
  log "running $tool in compose service '$service'"
  name_args=()
  if [ "$mode" = lsp-backend ] && [ -n "${RUBY_LSP_PLUGIN_HUB_CONTAINER:-}" ]; then
    # A hub that died hard can leave its container behind; free the name first.
    docker rm -f "$RUBY_LSP_PLUGIN_HUB_CONTAINER" >/dev/null 2>&1 || true
    name_args=(--name "$RUBY_LSP_PLUGIN_HUB_CONTAINER")
  fi
  # -T: no TTY, raw stdio stream. stdin stays attached (compose run default).
  # --no-deps: do not boot db/redis just to lint.
  # Identical-path mounts keep LSP file URIs and reek paths valid on the host.
  exec docker compose run --rm --no-deps -T ${name_args[@]+"${name_args[@]}"} \
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
