#!/usr/bin/env bash
# hub.sh -- the one shared MemPalace hub container for this machine.
#
# Every Claude Code session used to start its own `mempalace` MCP container,
# each loading chromadb, the embedding model and (on the CUDA image) its own
# CUDA context -- and each save hook started two or three more. Upstream's
# own answer is a long-lived HTTP "hub" (`mempalace serve`): it holds the
# per-palace writer lease, records itself in /data/.mempalace/server/, and the
# CLI forwards `mine` to it instead of being refused with "palace ... is held
# by PID 1". This script owns that container.
#
#   hub.sh ensure [--wait [SECONDS]] [--check]   start it if needed (idempotent)
#   hub.sh start | stop | restart | rm | status | logs [docker logs args]
#   hub.sh register <dir>                        make a project mineable
#   hub.sh fingerprint | print-run | token       inspection helpers
#
# Lifecycle: no restart policy; upstream's idle watchdog exits the server after
# MEMPALACE_HUB_IDLE_HOURS (default 2) idle hours and the next hook, shim or
# MCP connect starts it again. MEMPALACE_HUB_IDLE_HOURS=0 means always-on,
# with --restart unless-stopped so it also survives a reboot.
#
# Config changes (registry, image, port, idle hours) change the fingerprint
# label. A stopped hub with a stale fingerprint is recreated; a running one is
# left alone and reported -- the idle exit applies the change on its own.
#
# stdout discipline: `token` and `fingerprint` print exactly one value; every
# other message goes to stderr so hub-headers.sh can wrap this script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/common.sh"

FINGERPRINT_LABEL="dev.mempalace-docker.fingerprint"
# The image has no curl; python is always there. /healthz needs no token.
HEALTH_CMD="python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${MP_HUB_INNER_PORT}/healthz', timeout=3).read().strip()==b'ok' else 1)\""

die() { mp_log "$*"; exit 1; }

usage() {
    sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit "${1:-0}"
}

# stdin -> sha256 hex. sha256sum is Linux, shasum is macOS; python3 is the
# floor this plugin already requires.
mp_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 | cut -d' ' -f1
    else
        python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
    fi
}

# ---------------------------------------------------------------- secrets

# A port-published server must bind 0.0.0.0 inside the container, and
# upstream refuses a non-loopback bind without MEMPALACE_MCP_HTTP_TOKEN (its
# Host-header pinning only exists on loopback binds). So the plugin mints one
# token per machine, keeps it 0600 outside any repo, and hands it to the
# container through an env file -- never on argv, never in `docker inspect`
# output beyond what the daemon already shows root.
ensure_token() {
    mkdir -p "$MP_HUB_STATE_DIR"
    chmod 700 "$MP_HUB_STATE_DIR" 2>/dev/null || true
    if [ ! -s "$MP_HUB_TOKEN_FILE" ]; then
        ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$MP_HUB_TOKEN_FILE" )
    fi
    chmod 600 "$MP_HUB_TOKEN_FILE" 2>/dev/null || true
}

write_env_file() {
    ensure_token
    local token
    token="$(cat "$MP_HUB_TOKEN_FILE")"
    ( umask 077; printf 'MEMPALACE_MCP_HTTP_TOKEN=%s\nMEMPALACE_MCP_IDLE_HOURS=%s\n' \
        "$token" "$MP_HUB_IDLE_HOURS" > "$MP_HUB_ENV_FILE" )
    chmod 600 "$MP_HUB_ENV_FILE" 2>/dev/null || true
}

# ------------------------------------------------------------ run argv

always_on() {
    case "$MP_HUB_IDLE_HOURS" in
        0|0.0|0.00|00) return 0 ;;
    esac
    return 1
}

# Sets RUN_OPTS[] (everything between `run` and the image), RUN_IMAGE,
# RUN_CMD[] and FINGERPRINT. The fingerprint covers everything that would
# need a recreate to take effect -- the token file is deliberately not part
# of it, it never changes.
build_run() {
    mp_select_image
    mp_add_mounts

    RUN_OPTS=(-d --name "$MP_HUB_NAME")
    if always_on; then
        RUN_OPTS+=(--restart unless-stopped)
    fi
    RUN_OPTS+=(-p "127.0.0.1:${MP_HUB_PORT}:${MP_HUB_INNER_PORT}")
    RUN_OPTS+=(--env-file "$MP_HUB_ENV_FILE")
    # --health-start-interval (Docker 25+) probes every second while booting,
    # so `ensure --wait` sees "healthy" within a second of /healthz answering
    # instead of at the next 10 s tick.
    RUN_OPTS+=(--health-cmd "$HEALTH_CMD" --health-interval 10s --health-timeout 5s
               --health-retries 3 --health-start-period 120s --health-start-interval 1s)
    # No cap by default: with resource-guard's shim on PATH its server_caps
    # (`*mempalace*` -> 3g) applies at creation; an explicit value here wins.
    if [ -n "${MEMPALACE_HUB_MEMORY:-}" ]; then
        RUN_OPTS+=(--memory "$MEMPALACE_HUB_MEMORY")
    fi
    RUN_OPTS+=("${MP_RUN_ARGS[@]}")
    RUN_IMAGE="$MP_IMAGE"
    # `serve` is upstream's turnkey HTTP mode: it reads the token from the
    # environment and execs `mempalace.mcp_server --transport http`.
    RUN_CMD=(serve --host 0.0.0.0 --port "$MP_HUB_INNER_PORT")

    FINGERPRINT="$(printf '%s\0' "${RUN_OPTS[@]}" "$RUN_IMAGE" "${RUN_CMD[@]}" "idle=$MP_HUB_IDLE_HOURS" | mp_sha256)"
}

full_run_argv() {
    FULL_ARGV=(run "${RUN_OPTS[@]}" --label "${FINGERPRINT_LABEL}=${FINGERPRINT}" "$RUN_IMAGE" "${RUN_CMD[@]}")
}

write_state() {
    mkdir -p "$MP_HUB_STATE_DIR"
    python3 - "$MP_HUB_STATE_FILE" "$MP_HUB_NAME" "$MP_HUB_PORT" "$FINGERPRINT" "$RUN_IMAGE" \
        "$MP_HUB_IDLE_HOURS" ${MP_MOUNTED_TARGETS[@]+"${MP_MOUNTED_TARGETS[@]}"} <<'PY'
import json
import os
import sys
import time

path, name, port, fingerprint, image, idle_hours, *targets = sys.argv[1:]
payload = {
    "container": name,
    "port": int(port),
    "fingerprint": fingerprint,
    "image": image,
    "idle_hours": idle_hours,
    "mounted_targets": targets,
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
PY
}

# ------------------------------------------------------------ inspection

# Sets HUB_STATUS (missing|created|running|paused|restarting|exited|dead),
# HUB_FP (fingerprint label, may be empty) and HUB_HEALTH
# (none|starting|healthy|unhealthy).
inspect_hub() {
    local line
    if ! line="$(docker inspect --type container --format \
        "{{.State.Status}}|{{index .Config.Labels \"${FINGERPRINT_LABEL}\"}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" \
        "$MP_HUB_NAME" 2>/dev/null)"; then
        HUB_STATUS=missing
        HUB_FP=""
        HUB_HEALTH=none
        return 0
    fi
    IFS='|' read -r HUB_STATUS HUB_FP HUB_HEALTH <<< "$line"
}

create_hub() {
    write_env_file
    build_run
    full_run_argv
    docker "${FULL_ARGV[@]}" >/dev/null
    write_state
    mp_log "started hub ${MP_HUB_NAME} (${RUN_IMAGE}) on 127.0.0.1:${MP_HUB_PORT}; ${#MP_MOUNTED_TARGETS[@]} registered project path(s) mounted"
}

wait_healthy() {
    local limit="$1" waited=0
    while :; do
        inspect_hub
        case "$HUB_STATUS" in
            running)
                [ "$HUB_HEALTH" = healthy ] && return 0 ;;
            missing)
                mp_log "hub ${MP_HUB_NAME} does not exist"
                return 1 ;;
            exited|dead)
                # The usual cause on a first start: an old per-session
                # mempalace server (plugin 1.x, still running in another
                # session) holds the palace writer lease, and a writable hub
                # refuses to start without it.
                mp_log "hub ${MP_HUB_NAME} exited; last log lines:"
                docker logs --tail 5 "$MP_HUB_NAME" 2>&1 | sed 's/^/    /' >&2 || true
                mp_log "if it says the writer lease is held, close or /reload-plugins every session still on mempalace-docker 1.x, then: ${HERE}/hub.sh start"
                return 1 ;;
        esac
        if [ "$waited" -ge "$limit" ]; then
            mp_log "hub ${MP_HUB_NAME} not healthy after ${limit}s (status ${HUB_STATUS}, health ${HUB_HEALTH}); a cold start on the CUDA image can take longer -- retry, or see: docker logs ${MP_HUB_NAME}"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

# ------------------------------------------------------------- commands

cmd_ensure() {
    local wait_for="" check=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --wait)
                wait_for="$MP_HUB_WAIT_SECONDS"
                if [ $# -gt 1 ] && [[ "$2" =~ ^[0-9]+$ ]]; then
                    wait_for="$2"
                    shift
                fi ;;
            --check) check=1 ;;
            *) die "ensure: unknown argument '$1'" ;;
        esac
        shift
    done

    command -v docker >/dev/null 2>&1 || die "docker not found on PATH; the hub cannot start"
    inspect_hub
    case "$HUB_STATUS" in
        missing)
            create_hub ;;
        running|restarting)
            # Fast path for the shims: a running hub needs no fingerprint,
            # which saves the image/runtime probes on every hook fire.
            if [ "$check" = 1 ]; then
                build_run
                if [ "$HUB_FP" != "$FINGERPRINT" ]; then
                    mp_log "hub ${MP_HUB_NAME} runs an older config (registry, image, port or idle hours changed). It keeps serving; apply the new config with: ${HERE}/hub.sh restart -- or wait, the idle exit picks it up on its own."
                fi
            fi ;;
        paused)
            mp_log "hub ${MP_HUB_NAME} is paused; resume it with: docker unpause ${MP_HUB_NAME}" ;;
        *)
            build_run
            if [ "$HUB_FP" = "$FINGERPRINT" ]; then
                docker start "$MP_HUB_NAME" >/dev/null
                mp_log "started hub ${MP_HUB_NAME} (was ${HUB_STATUS})"
            else
                mp_log "hub ${MP_HUB_NAME} is ${HUB_STATUS} and its config changed; recreating it"
                docker rm -f "$MP_HUB_NAME" >/dev/null
                create_hub
            fi ;;
    esac

    if [ -n "$wait_for" ]; then
        wait_healthy "$wait_for"
    fi
}

cmd_stop() {
    inspect_hub
    case "$HUB_STATUS" in
        missing) mp_log "hub ${MP_HUB_NAME} does not exist" ;;
        running|paused|restarting)
            docker stop "$MP_HUB_NAME" >/dev/null
            mp_log "stopped hub ${MP_HUB_NAME}" ;;
        *) mp_log "hub ${MP_HUB_NAME} is already ${HUB_STATUS}" ;;
    esac
}

cmd_rm() {
    inspect_hub
    if [ "$HUB_STATUS" = missing ]; then
        mp_log "hub ${MP_HUB_NAME} does not exist"
        return 0
    fi
    docker rm -f "$MP_HUB_NAME" >/dev/null
    mp_log "removed hub ${MP_HUB_NAME} (the palace volume ${MP_VOLUME} is untouched)"
}

cmd_restart() {
    inspect_hub
    if [ "$HUB_STATUS" != missing ]; then
        docker rm -f "$MP_HUB_NAME" >/dev/null
        mp_log "removed hub ${MP_HUB_NAME} to apply the current config"
    fi
    cmd_ensure --wait "$@"
}

cmd_status() {
    inspect_hub
    printf 'container: %s\nstatus:    %s\nhealth:    %s\nendpoint:  http://127.0.0.1:%s/mcp\nregistry:  %s\n' \
        "$MP_HUB_NAME" "$HUB_STATUS" "$HUB_HEALTH" "$MP_HUB_PORT" "$MP_REGISTRY_DIR" >&2
    mp_registry_targets
    local target
    for target in ${MP_REGISTRY_TARGETS[@]+"${MP_REGISTRY_TARGETS[@]}"}; do
        printf '  registered: %s\n' "$target" >&2
    done
    if [ -f "$MP_HUB_STATE_FILE" ]; then
        printf 'last create: %s\n' "$MP_HUB_STATE_FILE" >&2
        sed 's/^/  /' "$MP_HUB_STATE_FILE" >&2
    fi
    [ "$HUB_STATUS" = running ]
}

cmd_register() {
    local target="${1:-}"
    [ -n "$target" ] || die "register: a directory is required"
    [ -d "$target" ] || die "register: not a directory: $target"
    local resolved
    resolved="$(mp_resolve_dir "$target")" || die "register: cannot resolve $target"
    if mp_reserved_target "$resolved"; then
        die "register: refusing $resolved -- mounting it would overlay a system or container path"
    fi

    mp_registry_targets
    local existing
    for existing in ${MP_REGISTRY_TARGETS[@]+"${MP_REGISTRY_TARGETS[@]}"}; do
        case "$resolved/" in
            "$existing/"*)
                mp_log "already covered: ${resolved} is under registered ${existing}"
                return 0 ;;
        esac
    done

    mkdir -p "$MP_REGISTRY_DIR"
    local name link current
    name="$(basename "$resolved")"
    link="$MP_REGISTRY_DIR/$name"
    if [ -e "$link" ] || [ -L "$link" ]; then
        current="$(mp_resolve_dir "$link" 2>/dev/null || true)"
        if [ "$current" = "$resolved" ]; then
            mp_log "already registered: ${link} -> ${resolved}"
            return 0
        fi
        name="${name}-$(printf '%s' "$resolved" | mp_sha256 | cut -c1-8)"
        link="$MP_REGISTRY_DIR/$name"
    fi
    ln -s "$resolved" "$link"
    mp_log "registered ${resolved} as ${link}. The hub mounts it read-only at that same path on its next start; apply now with: ${HERE}/hub.sh restart"
}

cmd_fingerprint() {
    build_run
    printf '%s\n' "$FINGERPRINT"
}

cmd_print_run() {
    write_env_file
    build_run
    full_run_argv
    printf 'docker'
    printf ' %q' "${FULL_ARGV[@]}"
    printf '\n'
}

cmd_token() {
    ensure_token
    cat "$MP_HUB_TOKEN_FILE"
}

main() {
    local cmd="${1:-}"
    [ -n "$cmd" ] || usage 1
    shift
    case "$cmd" in
        ensure) cmd_ensure "$@" ;;
        start) cmd_ensure --wait "$@" ;;
        stop) cmd_stop ;;
        rm) cmd_rm ;;
        restart) cmd_restart "$@" ;;
        status) cmd_status ;;
        logs) docker logs "$@" "$MP_HUB_NAME" ;;
        register) cmd_register "$@" ;;
        fingerprint) cmd_fingerprint ;;
        print-run) cmd_print_run ;;
        token) cmd_token ;;
        -h|--help|help) usage 0 ;;
        *) die "unknown command '$cmd' (try: hub.sh help)" ;;
    esac
}

main "$@"
