#!/bin/sh
# resource-guard docker shim.
#
# Two kinds of callers get labels; everything else passes through untouched:
#
# - Bash-tool work. SessionStart puts plugin-resource-guard/shims first on
#   the Bash tool's PATH (through CLAUDE_ENV_FILE), so `docker run`,
#   `docker create`, `docker container run|create` and `docker compose run`
#   started from a session land here with CLAUDE_PID set. The labels name the
#   owning session and the client process; the watchdog reads them to pause a
#   session's containers along with the rest of its work.
#
# - Session servers. With ~/.claude/.resource-guard/shims on Claude Code's own
#   PATH (one line in the shell rc; `resource-guard doctor` prints it), the
#   MCP and LSP servers a session starts through docker land here too. They
#   carry no CLAUDE_PID; their nearest claude ancestor is the session. They
#   get `dev.claude.role=server` (never paused) and a `--memory` cap from
#   shim.conf, so N sessions each running the same server can't each grow to
#   a quarter of RAM. The cap is set at creation on purpose: a JVM sizes its
#   heap from the limit it sees at start, so a cap added later would only get
#   it OOM-killed.
#
# - Hooks. They carry CLAUDE_PID like Bash work, plus CLAUDE_PLUGIN_ROOT or
#   CLAUDE_PROJECT_DIR, which Bash shells never get. A container a hook runs
#   (mempalace-docker's save hooks) is session machinery, so it is labeled
#   and capped like a server: paused, it would hang the hook.
#
# Usage: docker-shim.sh <docker|docker-compose> [args...]

tool=$1
shift

# The real binary is the first one on PATH outside any resource-guard shim
# directory. The marker file (not a path comparison) is what keeps two
# installed plugin versions, or the plugin and its stable link, from exec'ing
# each other forever.
real=""
old_ifs=$IFS
IFS=:
set -f
for dir in $PATH; do
    [ -n "$dir" ] || continue
    [ -e "$dir/.resource-guard-shim" ] && continue
    if [ -f "$dir/$tool" ] && [ -x "$dir/$tool" ]; then
        real="$dir/$tool"
        break
    fi
done
set +f
IFS=$old_ifs

if [ -z "$real" ]; then
    echo "$tool: command not found (resource-guard shim found no real binary on PATH)" >&2
    exit 127
fi

# Only run/create make a container. Everything else (`docker ps` in a
# terminal included) goes straight through, before any /proc reading.
creates=0
for arg do
    case $arg in
        run | create) creates=1; break ;;
    esac
done
[ "$creates" = 1 ] || exec "$real" "$@"

proc=${RESOURCE_GUARD_PROC_ROOT:-/proc}
state=${RESOURCE_GUARD_STATE_DIR:-${RESOURCE_GUARD_CLAUDE_HOME:-$HOME/.claude}/.resource-guard}

ppid_of() {
    ppid=0
    [ -r "$proc/$1/status" ] || return 0
    while read -r key value; do
        if [ "$key" = "PPid:" ]; then
            ppid=$value
            return 0
        fi
    done < "$proc/$1/status"
}

# Native installs run .../claude/versions/<ver>; npm installs run node with
# the CLI script as the first argument.
is_claude() {
    case $(readlink "$proc/$1/exe" 2>/dev/null) in
        */claude/versions/*) return 0 ;;
    esac
    [ -r "$proc/$1/cmdline" ] || return 1
    argv=$(tr '\0' '\n' < "$proc/$1/cmdline" | sed -n '1,2p' | tr '\n' ' ')
    argv0=${argv%% *}
    [ "${argv0##*/}" = claude ] && return 0
    case $argv in
        node*claude-code/cli*) return 0 ;;
    esac
    return 1
}

has_claude_pid() {
    [ -r "$proc/$1/environ" ] && tr '\0' '\n' < "$proc/$1/environ" | grep -q '^CLAUDE_PID='
}

# A session server: no CLAUDE_PID here, a claude process a few levels up
# (MCP wrappers like run-mempalace.sh sit in between), and nothing on the way
# that carries CLAUDE_PID (that would be Bash work that dropped the variable).
server_pid=""
if [ -n "${CLAUDE_PID:-}" ]; then
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}${CLAUDE_PROJECT_DIR:-}" ]; then
        server_pid=$CLAUDE_PID
    fi
else
    chain=""
    pid=$PPID
    depth=0
    while [ "$depth" -lt 4 ] && [ "$pid" -gt 1 ] 2>/dev/null; do
        if is_claude "$pid"; then
            server_pid=$pid
            break
        fi
        chain="$chain $pid"
        ppid_of "$pid"
        pid=$ppid
        depth=$((depth + 1))
    done
    # shellcheck disable=SC2086 # a space-separated list of PIDs
    for pid in $chain; do
        if has_claude_pid "$pid"; then
            server_pid=""
            break
        fi
    done
    if [ -z "$server_pid" ]; then
        exec "$real" "$@"
    fi
fi

owner=${CLAUDE_PID:-$server_pid}
session_id=""
[ -n "${CLAUDE_PID:-}" ] && session_id=${CLAUDE_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}

# Field 22 of /proc/<pid>/stat is the start time in clock ticks. Paired with
# a PID it identifies a process even after the PID is reused: ours (kept
# across exec) names the docker client, the owner's names the session.
_rg_field22() {
    shift 19
    field22=${1:-}
}
start_of() {
    field22=""
    if [ -r "$proc/$1/stat" ]; then
        read -r stat_line < "$proc/$1/stat"
        set -f
        # shellcheck disable=SC2086 # word splitting is the point here
        _rg_field22 ${stat_line##*) }
        set +f
    fi
}
start_of "$$"
client_start=$field22
start_of "$owner"
session_start=$field22

# shim.conf (written by SessionStart from the plugin config):
#   default <size>
#   image <glob> <size|none>     first matching glob wins
cap_for() {
    cap=""
    default_cap=""
    if [ -r "$state/shim.conf" ]; then
        while read -r kind pattern value; do
            case $kind in
                default) default_cap=$pattern ;;
                image)
                    # shellcheck disable=SC2254 # the pattern is a glob on purpose
                    case $1 in
                        $pattern) cap=$value; break ;;
                    esac ;;
            esac
        done < "$state/shim.conf"
    fi
    [ -n "$cap" ] || cap=$default_cap
    # Digits with an optional unit at the end (`none` lands here too). The
    # SessionStart writer also enforces docker's 6 MiB floor.
    case $cap in
        '' | *[!0-9bkmgBKMG]* | *[bkmgBKMG]?* | [!1-9]*) cap="" ;;
    esac
}

# Rebuild "$@" in place. Rotation: each pass drops the first original
# argument and re-appends it, so arguments added mid-loop land in the right
# spot. Labels go right after the subcommand; a server's --memory goes just
# before the image, and only when the command sets no memory limit itself.
mode=global
[ "$tool" = "docker-compose" ] && mode=cglobal
user_memory=0
image=""
# Original arguments not yet visited: while it is above 0, $1 is the next one.
left=$#
for arg do
    shift
    left=$((left - 1))
    add_labels=0
    insert_cap=0
    case $mode in
        global)
            case $arg in
                --config | -c | --context | -H | --host | -l | --log-level | --tlscacert | --tlscert | --tlskey)
                    mode=value ;;
                -*) ;;
                run | create) add_labels=1 ;;
                container) mode=container ;;
                compose) mode=cglobal ;;
                *) mode=copy ;;
            esac ;;
        value) mode=global ;;
        container)
            case $arg in
                run | create) add_labels=1 ;;
                *) mode=copy ;;
            esac ;;
        cglobal)
            case $arg in
                -f | --file | -p | --project-name | --project-directory | --profile | --env-file | --ansi | --progress | --parallel)
                    mode=cvalue ;;
                -*) ;;
                run) add_labels=1; mode=compose ;;
                *) mode=copy ;;
            esac ;;
        cvalue) mode=cglobal ;;
        # `docker run` options, read only to find where the image starts
        # (pflag: every flag but the boolean ones takes a value). A limit
        # the server's config sets wins: a reservation or swap limit above
        # the cap would make the daemon refuse the container.
        ropts)
            case $arg in
                --)
                    # The image comes next; the cap has to stay before `--`.
                    if [ "$left" -gt 0 ]; then
                        image=$1
                        insert_cap=1
                    fi
                    mode=copy ;;
                --memory | --memory-reservation | --memory-swap) user_memory=1; mode=rvalue ;;
                --memory=* | --memory-reservation=* | --memory-swap=*) user_memory=1 ;;
                --*=*) ;;
                --rm | --detach | --interactive | --tty | --init | --privileged | --read-only | --publish-all | \
                    --no-healthcheck | --oom-kill-disable | --sig-proxy | --disable-content-trust | --quiet | --help | \
                    --use-api-socket) ;;
                --*) mode=rvalue ;;
                -?*)
                    # Boolean letters bundle (-it). The first other letter
                    # takes the rest of the word as its value (-w/data), or
                    # the next argument when nothing is left (-dm 512m).
                    rest=${arg#-}
                    while [ -n "$rest" ]; do
                        letter=${rest%"${rest#?}"}
                        rest=${rest#?}
                        case $letter in
                            [dtiPq]) continue ;;
                            m) user_memory=1 ;;
                        esac
                        [ -n "$rest" ] || mode=rvalue
                        break
                    done ;;
                *) image=$arg; insert_cap=1; mode=copy ;;
            esac ;;
        rvalue) mode=ropts ;;
    esac
    if [ "$insert_cap" = 1 ] && [ "$user_memory" = 0 ]; then
        cap_for "$image"
        [ -n "$cap" ] && set -- "$@" --memory "$cap"
    fi
    set -- "$@" "$arg"
    if [ "$add_labels" = 1 ]; then
        set -- "$@" --label "dev.claude.pid=$owner" --label "dev.claude.client=$$"
        [ -n "$session_id" ] && set -- "$@" --label "dev.claude.session=$session_id"
        [ -n "$client_start" ] && set -- "$@" --label "dev.claude.client_start=$client_start"
        [ -n "$session_start" ] && set -- "$@" --label "dev.claude.pid_start=$session_start"
        if [ -n "$server_pid" ]; then
            set -- "$@" --label "dev.claude.role=server"
            # compose run has no --memory flag: label only.
            [ "$mode" = compose ] || mode=ropts
        fi
        [ "$mode" = ropts ] || mode=copy
    fi
done

exec "$real" "$@"
