#!/bin/sh
# resource-guard docker shim.
#
# SessionStart puts plugin-resource-guard/shims first on the Bash tool's PATH
# (through CLAUDE_ENV_FILE), so `docker run`, `docker create`,
# `docker container run|create` and `docker compose run` started from a Claude
# Code session land here. The shim adds labels naming the owning session and
# the client process, then execs the real binary under the same PID. The
# watchdog reads those labels to decide which containers belong to which
# session when it has to pause them; without labels it can only guess.
#
# Usage: docker-shim.sh <docker|docker-compose> [args...]

tool=$1
shift

# The real binary is the first one on PATH outside any resource-guard shim
# directory. The marker file (not a path comparison) is what keeps two
# installed plugin versions from exec'ing each other forever.
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

# Outside a Claude Code Bash tool there is nothing to label.
if [ -z "${CLAUDE_PID:-}" ]; then
    exec "$real" "$@"
fi

session_id=${CLAUDE_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}

# Field 22 of /proc/<pid>/stat is the start time in clock ticks. Paired with
# a PID it identifies a process even after the PID is reused: ours (kept
# across exec) names the docker client, CLAUDE_PID's names the session.
_rg_field22() {
    shift 19
    field22=${1:-}
}
start_of() {
    field22=""
    if [ -r "/proc/$1/stat" ]; then
        read -r stat_line < "/proc/$1/stat"
        set -f
        # shellcheck disable=SC2086 # word splitting is the point here
        _rg_field22 ${stat_line##*) }
        set +f
    fi
}
start_of "$$"
client_start=$field22
start_of "$CLAUDE_PID"
session_start=$field22

# Rebuild "$@" in place, inserting the labels right after the subcommand that
# creates a container. Rotation: each pass drops the first original argument
# and re-appends it, so labels appended mid-loop end up in the right spot.
mode=global
[ "$tool" = "docker-compose" ] && mode=cglobal
for arg do
    shift
    set -- "$@" "$arg"
    add_labels=0
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
                run) add_labels=1 ;;
                *) mode=copy ;;
            esac ;;
        cvalue) mode=cglobal ;;
    esac
    if [ "$add_labels" = 1 ]; then
        set -- "$@" --label "dev.claude.pid=$CLAUDE_PID" --label "dev.claude.client=$$"
        [ -n "$session_id" ] && set -- "$@" --label "dev.claude.session=$session_id"
        [ -n "$client_start" ] && set -- "$@" --label "dev.claude.client_start=$client_start"
        [ -n "$session_start" ] && set -- "$@" --label "dev.claude.pid_start=$session_start"
        mode=copy
    fi
done

exec "$real" "$@"
