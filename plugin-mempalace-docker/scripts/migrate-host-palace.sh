#!/usr/bin/env bash
# Consolidate a split-brain MemPalace onto the shared docker volume.
#
# The problem this solves: a hand-rolled CLI shim that set `-e HOME=$HOME` and
# `--palace $HOME/.mempalace/palace` wrote the HOST palace, while the MCP
# server wrote the named volume. Two palaces, neither aware of the other.
#
# The volume is canonical here, because that is what the MCP server reads.
#
# Why re-mining rather than merging: the CLI has mine/sweep/sync/repair/
# migrate/status and NO export, import or merge verb (`migrate` is
# ChromaDB-version migration only). A palace is a Chroma collection plus
# knowledge_graph.sqlite3 — those do not union by copying files. Re-mining the
# original sources into the canonical palace is the only correct merge.
#
#   migrate-host-palace.sh                            # pre-flight report only
#   migrate-host-palace.sh --yes                      # re-mine (default)
#   migrate-host-palace.sh --yes --project ~/code/app # ...plus project dirs
#   migrate-host-palace.sh --yes --strategy replace   # overwrite volume palace
#
# ~/.mempalace is NEVER deleted. It stays a cold backup.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/common.sh"

STRATEGY="remine"
CONFIRMED=0
PROJECTS=()
HOST_PALACE="$HOME/.mempalace/palace"
BACKUP_DIR="${MEMPALACE_BACKUP_DIR:-$HOME/.mempalace-backups}"

die() { printf 'migrate-host-palace.sh: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
rule() { printf -- '---------------------------------------------------------------\n'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --yes) CONFIRMED=1; shift ;;
        --strategy) STRATEGY="${2:-}"; shift 2 ;;
        --strategy=*) STRATEGY="${1#*=}"; shift ;;
        --project) PROJECTS+=("${2:-}"); shift 2 ;;
        --project=*) PROJECTS+=("${1#*=}"); shift ;;
        -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument '$1'" ;;
    esac
done

case "$STRATEGY" in
    remine|replace) ;;
    *) die "unknown strategy '$STRATEGY' (expected 'remine' or 'replace')" ;;
esac

command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
docker volume inspect "$MP_VOLUME" >/dev/null 2>&1 \
    || die "docker volume '$MP_VOLUME' does not exist; nothing to migrate into"

mp_select_image
mp_add_mounts

# Both palaces need to be readable in one container for the pre-flight, so
# mount the host palace's parent at a dedicated path.
HOST_MOUNT=()
if [ -d "$HOME/.mempalace" ]; then
    HOST_MOUNT=(-v "$HOME/.mempalace:/host-mempalace:ro")
fi

# ---------------------------------------------------------------- pre-flight

rule
say "Pre-flight: comparing both palaces"
rule
say "image:         $MP_IMAGE"
say "volume:        $MP_VOLUME  (canonical -> /data/.mempalace/palace)"
say "host palace:   $HOST_PALACE"
say "strategy:      $STRATEGY"
say ""

say "== volume palace =="
docker run --rm "${MP_RUN_ARGS[@]}" "$MP_IMAGE" cli status 2>&1 | sed 's/^/   /' || say "   (status failed)"
say ""

if [ -d "$HOST_PALACE" ]; then
    say "== host palace =="
    docker run --rm "${MP_RUN_ARGS[@]}" "${HOST_MOUNT[@]}" "$MP_IMAGE" \
        cli --palace /host-mempalace/palace status 2>&1 | sed 's/^/   /' || say "   (status failed)"
else
    say "== host palace =="
    say "   not present at $HOST_PALACE -- nothing to consolidate"
fi
say ""

if [ "$CONFIRMED" != "1" ]; then
    rule
    say "Report only. Compare the drawer counts above, then re-run with --yes."
    say ""
    say "  --strategy remine   (default) keep the volume palace and re-mine"
    say "                      sources into it. Additive, non-destructive."
    say "  --strategy replace  overwrite /data/.mempalace with the host copy."
    say "                      Destructive. Only if the host palace is"
    say "                      strictly better."
    rule
    exit 0
fi

# ------------------------------------------------------------------- backup

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/${MP_VOLUME}-${STAMP}.tar.gz"

rule
say "Backing up the volume before touching anything"
rule
# tar via a minimal image rather than the mempalace one: the entrypoint there
# dispatches to the CLI, so a raw tar needs an override anyway.
docker run --rm -v "${MP_VOLUME}:/data:ro" -v "$BACKUP_DIR:/backup" \
    --entrypoint /bin/sh "$MP_IMAGE" \
    -c "tar czf /backup/$(basename "$BACKUP_FILE") -C /data ." \
    || die "backup failed; refusing to continue"
say "backup: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"
say ""

# ----------------------------------------------------------------- migrate

if [ "$STRATEGY" = "remine" ]; then
    rule
    say "Re-mining sources into the volume palace"
    rule

    if [ -d "$HOME/.claude/projects" ]; then
        say "-> transcripts: /transcripts (--mode convos)"
        docker run -i --rm "${MP_RUN_ARGS[@]}" "$MP_IMAGE" \
            cli mine /transcripts --mode convos 2>&1 | sed 's/^/   /'
    else
        say "-> transcripts: skipped, $HOME/.claude/projects not found"
    fi

    for proj in ${PROJECTS+"${PROJECTS[@]}"}; do
        [ -d "$proj" ] || { say "-> project: skipped, not a directory: $proj"; continue; }
        abs="$(cd "$proj" && pwd)"
        say "-> project: $abs"
        docker run -i --rm "${MP_RUN_ARGS[@]}" -v "$abs:/mine-src:ro" "$MP_IMAGE" \
            cli mine /mine-src 2>&1 | sed 's/^/   /'
    done

    if [ ${#PROJECTS[@]} -eq 0 ]; then
        say ""
        say "No --project given. Transcripts carry most of the history, but"
        say "project files do not migrate themselves -- pass --project for each"
        say "repo you want in the palace, or just open it and let the"
        say "SessionStart auto-mine handle it."
    fi
else
    [ -d "$HOST_PALACE" ] || die "--strategy replace needs a host palace at $HOST_PALACE"
    rule
    say "Overwriting the volume palace from the host copy"
    rule
    docker run --rm -v "${MP_VOLUME}:/data" "${HOST_MOUNT[@]}" \
        --entrypoint /bin/sh "$MP_IMAGE" -c '
            set -e
            rm -rf /data/.mempalace.replaced
            if [ -d /data/.mempalace ]; then mv /data/.mempalace /data/.mempalace.replaced; fi
            mkdir -p /data/.mempalace
            cp -a /host-mempalace/. /data/.mempalace/
        ' || die "replace failed; restore from $BACKUP_FILE"
    say "old volume palace kept in-volume at /data/.mempalace.replaced"
    say ""
    say "== volume palace after replace =="
    docker run --rm "${MP_RUN_ARGS[@]}" "$MP_IMAGE" cli status 2>&1 | sed 's/^/   /' || true
fi

# ----------------------------------------------------------------- epilogue

say ""
rule
say "Done. Follow-ups, none of them automatic:"
rule
say "1. $HOST_PALACE is untouched and still a full cold backup."
say "   Leave it until you have used the consolidated palace for a while."
say ""
say "2. Empty leftover volumes can go, once you have confirmed they are yours:"
say "     docker volume rm mempalace-data-windows mempalace_mempalace-data"
say "   Check first -- this script will not run it for you:"
say "     docker run --rm -v mempalace-data-windows:/d alpine du -sh /d"
say ""
say "3. Restore path, if the palace looks wrong:"
say "     docker run --rm -v ${MP_VOLUME}:/data -v ${BACKUP_DIR}:/backup \\"
say "       --entrypoint /bin/sh ${MP_IMAGE} \\"
say "       -c 'rm -rf /data/* /data/.[!.]* && tar xzf /backup/$(basename "$BACKUP_FILE") -C /data'"
