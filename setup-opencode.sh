#!/usr/bin/env bash
#
# setup-opencode.sh — Install OpenCode-compatible plugins from this
# marketplace into either ${XDG_CONFIG_HOME:-~/.config}/opencode/ (global)
# or <project>/.opencode/ (per-project).
#
# Discovery, staging, the repository guard and the tracker all go through
# scripts/opencode/convert.py (list, plan, guard, hash, tracker). Every
# write, backup and delete passes the guard first, and a write into a git
# submodule needs one approval per repo.
#
# Usage:
#   setup-opencode.sh [options]
#
# Options:
#   --global                Install to ${XDG_CONFIG_HOME:-~/.config}/opencode/
#   --project <path>        Install to <path>/.opencode/
#   --plugin <id>           Install (or uninstall) only this plugin.json id
#                           (repeatable; the plugin-<name> dir name works too,
#                           with a WARN)
#   --list                  List PLUGIN KIND SOURCE per install source (only
#                           the --plugin ones when given)
#   --dry-run               Show what would happen, write nothing
#   --force                 Overwrite conflicting files after a backup
#                           (never overrides a BLOCKED unit)
#   --allow-repo <path>     Approve writes (and uninstall deletes) in this
#                           git submodule (repeatable; remembered in the
#                           tracker)
#   --uninstall             Remove the files this script installed
#                           (combine with --global or --project <path>; with
#                           --plugin, only those plugins' files and rows)
#   -h, --help              Show this help text

set -euo pipefail

# --- Output colors (TTY only) ------------------------------------------------
if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_SKIP=$'\033[33m'; C_ERR=$'\033[31m'
  C_HDR=$'\033[1;36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_OK=''; C_SKIP=''; C_ERR=''; C_HDR=''; C_DIM=''; C_RST=''
fi

# --- Defaults ----------------------------------------------------------------
SCOPE=""                # "global" | "project"
PROJECT_PATH=""
ACTION="install"        # "install" | "list" | "uninstall"
DRY_RUN=0
FORCE=0
SELECTED=()
CLI_ALLOW_REPOS=()
REPO_ROOT=""
TRACKER_NAME=".opencode-setup-tracker"
PAYLOAD_NAMESPACE="llm-agent-workflow"
PAYLOAD_FAILED_REASON="its payload unit was not installed"
TAB=$'\t'

# Run state (bash 3.2 compatible: plain arrays, no associative arrays).
APPROVED_REPOS=()       # realpaths approved for G3: --allow-repo, tracker header, TTY "y"
DECLINED_REPOS=()       # realpaths answered "n" on the TTY in this run
FAILED_PAYLOADS=()      # plugin ids whose payload dir unit FAILED or was BLOCKED
ORIGIN_CACHE=""         # "<toplevel><TAB><origin>" lines
REPO_CHANGES=""         # "<toplevel><TAB><written|removed><TAB><realpath>" lines
WRITTEN=0; SAME=0; SKIPPED=0; BLOCKED=0; FAILED=0; REMOVED=0; KEPT=0
ABORTED=0

# --- Helpers -----------------------------------------------------------------
die() { printf '%serror:%s %s\n' "$C_ERR" "$C_RST" "$*" >&2; exit 1; }
log() { printf '%s\n' "$*"; }
hdr() { printf '\n%s%s%s\n' "$C_HDR" "$*" "$C_RST"; }
dim() { printf '%s%s%s\n' "$C_DIM"  "$*" "$C_RST"; }

yn_prompt() {
  local prompt="$1" ans
  [[ -t 0 ]] || return 1
  read -r -p "$prompt" ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

usage() {
  sed -n '2,/^set -euo/p' "$0" \
    | sed 's/^# \{0,1\}//; /^$/d; /^set -euo/d'
  exit 0
}

# Walk up from $0 looking for .claude-plugin/marketplace.json.
find_repo_root() {
  local dir
  dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
  [[ -z "$dir" ]] && dir="$(pwd)"
  while [[ "$dir" != "/" ]]; do
    if [[ -f "$dir/.claude-plugin/marketplace.json" ]]; then
      printf '%s\n' "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  printf '%s\n' "$(pwd)"
}

# The converter CLI; bytecode stays out of the checkout.
convert() { PYTHONDONTWRITEBYTECODE=1 python3 "$CONVERT" "$@"; }

# in_list NEEDLE ITEM... succeeds when NEEDLE is one of the ITEMs.
in_list() {
  local needle="$1" item
  shift
  for item in "$@"; do
    if [[ "$item" == "$needle" ]]; then return 0; fi
  done
  return 1
}

# The physical path of an existing dir, or nothing.
canonical_dir() { (CDPATH='' cd -- "$1" 2>/dev/null && pwd -P); }

# Every approved repo joined by ":" (the tracker header value).
allowed_repos_value() {
  local IFS=':'
  printf '%s' "${APPROVED_REPOS[*]+"${APPROVED_REPOS[*]}"}"
}

superproject_of() {
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR -u GIT_INDEX_FILE \
    git -C "$1" rev-parse --show-superproject-working-tree 2>/dev/null || true
}

# --- Guard -------------------------------------------------------------------
# guard_path OP PATH asks `convert.py guard` about PATH and sets GUARD_REALPATH,
# GUARD_REPO, GUARD_ORIGIN and GUARD_VERDICT. It succeeds only for an `ok`
# verdict; a guard failure reads as blocked (fail closed).
guard_path() {
  local op="$1" path="$2" out status=0 approved allow=()
  for approved in ${APPROVED_REPOS[@]+"${APPROVED_REPOS[@]}"}; do allow+=(--allow-repo "$approved"); done
  GUARD_REALPATH=""; GUARD_REPO="-"; GUARD_ORIGIN="-"; GUARD_VERDICT="blocked:guard failed"
  out="$(convert guard --scope-root "$TARGET_ROOT" --op "$op" ${allow[@]+"${allow[@]}"} "$path")" || status=$?
  if [[ -n "$out" ]]; then
    IFS="$TAB" read -r _ GUARD_REALPATH GUARD_REPO GUARD_ORIGIN GUARD_VERDICT <<<"$out"
  fi
  [[ "$status" -eq 0 && "$GUARD_VERDICT" == "ok" ]]
}

# approve_repo TOPLEVEL: approved by --allow-repo, the tracker header or a
# TTY "y" earlier in this run; otherwise ask once per repo on a TTY.
approve_repo() {
  local top="$1" super
  if in_list "$top" ${APPROVED_REPOS[@]+"${APPROVED_REPOS[@]}"}; then return 0; fi
  if in_list "$top" ${DECLINED_REPOS[@]+"${DECLINED_REPOS[@]}"}; then return 1; fi
  if [[ ! -t 0 ]]; then return 1; fi
  super="$(superproject_of "$top")"
  if yn_prompt "Write into $top (submodule of ${super:-its superproject})? You commit these. [y/N] "; then
    APPROVED_REPOS+=("$top")
    return 0
  fi
  DECLINED_REPOS+=("$top")
  return 1
}

# guard_approved OP PATH: guard_path, asking for approval when the verdict is G3.
guard_approved() {
  if guard_path "$1" "$2"; then return 0; fi
  if [[ "$GUARD_VERDICT" == approve:* ]] && approve_repo "${GUARD_VERDICT#approve:}"; then
    guard_path "$1" "$2"
    return
  fi
  return 1
}

# The reason text for a non-ok verdict.
blocked_reason() {
  case "$1" in
    approve:*) printf 'G3 needs approval; re-run with --allow-repo %s' "${1#approve:}" ;;
    blocked:*) printf '%s' "${1#blocked:}" ;;
    *)         printf '%s' "$1" ;;
  esac
}

# Stop before any write when the tracker itself may not be written.
preflight_tracker() {
  if guard_approved write "$TRACKER"; then return 0; fi
  die "$TRACKER_NAME: $(blocked_reason "$GUARD_VERDICT"); nothing was changed"
}

# --- Reporting ---------------------------------------------------------------
# remember_origin TOPLEVEL ORIGIN caches a credential-free origin.
remember_origin() {
  if [[ "$1" != "-" ]]; then ORIGIN_CACHE+="$1$TAB$2"$'\n'; fi
}

# repo_note sets NOTE to " (repo: <toplevel> <origin>)" for the current unit,
# or to nothing when its realpath is in no repo. Origins come from the guard.
repo_note() {
  local top origin found=""
  NOTE=""
  if [[ "$repo" == "-" ]]; then return 0; fi
  while IFS="$TAB" read -r top origin; do
    if [[ "$top" == "$repo" ]]; then found="$origin"; break; fi
  done <<<"$ORIGIN_CACHE"
  if [[ -z "$found" ]]; then
    guard_path write "$realpath" || true
    found="$GUARD_ORIGIN"
    remember_origin "$repo" "$found"
  fi
  if [[ "$found" == "-" ]]; then NOTE=" (repo: $repo)"; else NOTE=" (repo: $repo $found)"; fi
}

say() {  # say COLOR LABEL TEXT, with the current unit's repo note
  repo_note
  printf '%s%s%s %s%s\n' "$1" "$2" "$C_RST" "$3" "$NOTE"
}

mark_payload_failed() {
  if [[ "$unit" == "dir" ]]; then FAILED_PAYLOADS+=("$plugin"); fi
}

report_blocked() {  # report_blocked VERDICT [PREFIX]
  say "$C_ERR" "[BLOCKED]" "$target_rel: ${2:-}$(blocked_reason "$1")"
  BLOCKED=$((BLOCKED + 1))
  mark_payload_failed
}

report_failed() {  # report_failed REASON
  say "$C_ERR" "[FAIL]" "$target_rel ($1)"
  FAILED=$((FAILED + 1))
  mark_payload_failed
}

record_change() {  # record_change written|removed
  if [[ "$repo" != "-" ]]; then REPO_CHANGES+="$repo$TAB$1$TAB$realpath"$'\n'; fi
}

# One block per repo that had writes or removals.
repo_summary() {
  local tops top entry action path super
  tops="$(printf '%s' "$REPO_CHANGES" | cut -f1 | awk 'NF && !seen[$0]++')"
  if [[ -z "$tops" ]]; then return 0; fi
  while IFS= read -r top; do
    hdr "Repo $top"
    while IFS="$TAB" read -r entry action path; do
      if [[ "$entry" == "$top" ]]; then printf '  %s %s\n' "$action" "$path"; fi
    done <<<"$REPO_CHANGES"
    printf '  commit these yourself in %s\n' "$top"
    super="$(superproject_of "$top")"
    if [[ -n "$super" ]]; then printf '  then update the submodule pointer in %s\n' "$super"; fi
  done <<<"$tops"
}

# --- Tracker -----------------------------------------------------------------
# The tracked hash of (TYPE, PATH): sha256:<hex>, "-" for a v1 row, or nothing.
tracked_hash() {
  T="$1" P="$2" awk -F'\t' '$1 == ENVIRON["T"] && $3 == ENVIRON["P"] { print $5; exit }' <<<"$TRACKER_ROWS"
}

add_row() {  # the current unit, as a tracker row with the staged hash
  printf '%s\n' "$unit$TAB$plugin$TAB$target_rel$TAB$realpath$TAB$hash$TAB$repo" >>"$ADD_ROWS"
}

# write_tracker MERGE-ARGS...: guard the tracker path again, then merge once.
write_tracker() {
  if ! guard_path write "$TRACKER"; then
    printf '%s[BLOCKED]%s %s: %s\n' "$C_ERR" "$C_RST" "$TRACKER_NAME" "$(blocked_reason "$GUARD_VERDICT")"
    BLOCKED=$((BLOCKED + 1))
    return 0
  fi
  convert tracker merge --tracker "$TRACKER" "$@" || die "Tracker update failed: $TRACKER"
  dim "Tracker: $TRACKER"
}

# --- Install units -----------------------------------------------------------
# unit_state TRACKED sets STATE (NEW, SAME, UPDATE, CONFLICT or FAIL) for the
# current unit's realpath; FAIL_REASON explains FAIL. Reads only.
unit_state() {
  local tracked="$1" digest
  FAIL_REASON=""
  if [[ ! -e "$realpath" && ! -L "$realpath" ]]; then STATE="NEW"; return 0; fi
  if [[ -L "$realpath" ]] \
    || { [[ "$unit" == "dir" ]] && [[ ! -d "$realpath" ]]; } \
    || { [[ "$unit" == "file" ]] && [[ ! -f "$realpath" ]]; }; then
    STATE="FAIL"; FAIL_REASON="target is not a $unit"
    return 0
  fi
  if ! digest="$(convert hash "$realpath")"; then
    STATE="FAIL"; FAIL_REASON="cannot hash the target"
    return 0
  fi
  digest="${digest%%"$TAB"*}"
  if [[ "$digest" == "$hash" ]]; then
    STATE="SAME"
  elif [[ -n "$tracked" && "$tracked" != "-" && "$tracked" == "$digest" ]]; then
    STATE="UPDATE"
  else
    STATE="CONFLICT"
  fi
}

# resolve_conflict: 0 overwrite, 1 skip, 2 abort.
resolve_conflict() {
  local choice
  if [[ "$FORCE" -eq 1 ]]; then return 0; fi
  if [[ ! -t 0 ]]; then
    say "$C_SKIP" "[SKIP]" "$target_rel (conflict; pass --force to overwrite)"
    SKIPPED=$((SKIPPED + 1))
    return 1
  fi
  read -r -p "  $target_rel differs — (s)kip / (o)verwrite / (a)bort: " choice || choice="a"
  case "$choice" in
    o|O) return 0 ;;
    a|A) return 2 ;;
    *)   say "$C_SKIP" "[SKIP]" "$target_rel"
         SKIPPED=$((SKIPPED + 1))
         return 1 ;;
  esac
}

# copy_to SOURCE DEST: a file or tree copy with modes kept.
copy_to() {
  mkdir -p "$(dirname "$2")" && cp -Rp "$1" "$2"
}

# place_unit writes the staged unit at its realpath: files with cp -p, dirs
# through a temp sibling that then replaces the target. The old tree moves
# aside into a reserved dir first and comes back when the new one cannot be
# moved in; PLACE_NOTE then names it if even that move fails. A leftover old
# tree after a good swap is a WARN, not a failure.
place_unit() {
  local staged="$STAGE_DIR/$stage_rel" parent tmp old=""
  PLACE_NOTE=""
  parent="$(dirname "$realpath")"
  mkdir -p "$parent" || return 1
  if [[ "$unit" == "file" ]]; then
    cp -p "$staged" "$realpath"
    return
  fi
  tmp="$(mktemp -d "$parent/.${realpath##*/}.XXXXXX")" || return 1
  if ! { rmdir "$tmp" && cp -Rp "$staged" "$tmp"; }; then
    rm -rf "$tmp"
    return 1
  fi
  if [[ -e "$realpath" ]]; then
    old="$(mktemp -d "$parent/.${realpath##*/}.old.XXXXXX")" || { rm -rf "$tmp"; return 1; }
    if ! mv "$realpath" "$old/tree"; then rm -rf "$tmp" "$old"; return 1; fi
  fi
  if ! mv "$tmp" "$realpath"; then
    rm -rf "$tmp"
    if [[ -n "$old" ]]; then
      if mv "$old/tree" "$realpath"; then rmdir "$old"; else PLACE_NOTE="; the old tree is at $old/tree"; fi
    fi
    return 1
  fi
  if [[ -n "$old" ]] && ! rm -rf "$old"; then
    printf 'WARN %s: leftover: the old tree of %s could not be removed; remove it yourself\n' "$old" "$target_rel" >&2
  fi
  return 0
}

# commit_unit LABEL: guard the target (and any backup) again right before
# touching it, then back up, write and record the row.
commit_unit() {
  local label="$1" backup
  if ! guard_path write "$realpath"; then report_blocked "$GUARD_VERDICT"; return 0; fi
  if [[ "$GUARD_REALPATH" != "$realpath" ]]; then report_blocked "blocked:realpath changed since plan"; return 0; fi
  remember_origin "$GUARD_REPO" "$GUARD_ORIGIN"
  if [[ "$label" == "[OVERWRITE]" ]]; then
    if ! guard_path write "$TARGET_ROOT/$PAYLOAD_NAMESPACE/.backup/$BACKUP_TS/$target_rel"; then
      report_blocked "$GUARD_VERDICT" "backup: "
      return 0
    fi
    backup="$GUARD_REALPATH"
    if ! copy_to "$realpath" "$backup"; then report_failed "backup failed"; return 0; fi
    dim "  backup: $backup"
  fi
  if ! place_unit; then report_failed "copy failed$PLACE_NOTE"; return 0; fi
  say "$C_OK" "$label" "$target_rel"
  WRITTEN=$((WRITTEN + 1))
  record_change written
  add_row
}

# payload_failed: the current unit is a plugin file whose payload unit FAILED
# or was BLOCKED, so it would run without its payload.
payload_failed() {
  [[ "$target_rel" == plugins/* ]] && in_list "$plugin" ${FAILED_PAYLOADS[@]+"${FAILED_PAYLOADS[@]}"}
}

# dry_run_unit prints the current plan row's one [DRY-RUN] line: the state an
# install would reach (BLOCKED, FAIL, NEW, SAME, UPDATE or CONFLICT), the
# verdict, a FAIL reason and the repo note. It writes nothing; an approve:
# verdict keeps its target state, since the approval is asked at install time.
dry_run_unit() {
  local reason=""
  if [[ "$verdict" == blocked:* ]]; then
    STATE="BLOCKED"
  elif payload_failed; then
    STATE="FAIL"; FAIL_REASON="$PAYLOAD_FAILED_REASON"
  else
    unit_state "$(tracked_hash "$unit" "$target_rel")"
  fi
  if [[ "$STATE" == "FAIL" ]]; then reason=" ($FAIL_REASON)"; fi
  if [[ "$STATE" == "BLOCKED" || "$STATE" == "FAIL" ]]; then mark_payload_failed; fi
  say "$C_SKIP" "[DRY-RUN]" "$STATE $target_rel $verdict$reason"
}

# install_unit handles the current plan row; it returns 1 only on (a)bort.
install_unit() {
  local tracked
  if [[ "$DRY_RUN" -eq 1 ]]; then dry_run_unit; return 0; fi
  case "$verdict" in
    ok) ;;
    approve:*) if ! approve_repo "${verdict#approve:}"; then report_blocked "$verdict"; return 0; fi ;;
    *)         report_blocked "$verdict"; return 0 ;;
  esac
  if payload_failed; then
    report_failed "$PAYLOAD_FAILED_REASON"
    return 0
  fi
  tracked="$(tracked_hash "$unit" "$target_rel")"
  unit_state "$tracked"
  case "$STATE" in
    NEW)    commit_unit "[OK]" ;;
    UPDATE) commit_unit "[UPDATE]" ;;
    SAME)   say "$C_OK" "[SAME]" "$target_rel"
            SAME=$((SAME + 1))
            # A tracked row (v2, or v1 upgraded) is kept with a fresh hash; a foreign file gets no row.
            if [[ -n "$tracked" ]]; then add_row; fi ;;
    FAIL)   report_failed "$FAIL_REASON" ;;
    CONFLICT)
      local choice=0
      say "$C_SKIP" "[CONFLICT]" "$target_rel"
      resolve_conflict || choice=$?
      case "$choice" in
        0) commit_unit "[OVERWRITE]" ;;
        2) return 1 ;;
      esac ;;
  esac
  return 0
}

# --- Notices (report only) ---------------------------------------------------
# double_load_notices warns once per planned plugin file that the other
# scope's tracker (OTHER_TRACKER) also records: opencode loads both copies.
# An invalid other tracker is a WARN too; neither ever changes anything.
double_load_notices() {
  local other_rows target
  if ! other_rows="$(convert tracker read --tracker "$OTHER_TRACKER")"; then
    printf 'WARN %s: double-load: the %s scope tracker is invalid; its plugin files were not checked\n' \
      "$OTHER_TRACKER" "$OTHER_SCOPE" >&2
    return 0
  fi
  while IFS="$TAB" read -r _ _ _ target _; do
    if [[ "$target" == plugins/* ]] \
      && T="$target" awk -F'\t' '$3 == ENVIRON["T"] { found = 1 } END { exit !found }' <<<"$other_rows"; then
      printf 'WARN: %s is also installed in the %s scope; opencode loads both copies; hooks run twice\n' \
        "$target" "$OTHER_SCOPE" >&2
    fi
  done <<<"$PLAN"
}

# --- Uninstall rows ----------------------------------------------------------
remove_row() {
  printf '%s\n' "$unit$TAB$plugin$TAB$target_rel$TAB$realpath$TAB$hash$TAB$repo" >>"$REMOVE_ROWS"
}

# selected_rows: the tracker rows on stdin whose plugin column is a --plugin
# id, or every row when no --plugin was given.
selected_rows() {
  if [[ ${#SELECTED[@]} -eq 0 ]]; then cat; return 0; fi
  IDS="$(printf '%s\n' "${SELECTED[@]}")" awk -F'\t' '
    BEGIN { count = split(ENVIRON["IDS"], ids, "\n"); for (i = 1; i <= count; i++) wanted[ids[i]] = 1 }
    $2 in wanted'
}

# locate_row sets PATH_REALPATH to the realpath <scope>/<path> resolves to now,
# as the guard reports it whatever its verdict; it fails when the guard gives
# no realpath.
locate_row() {
  guard_path delete "$TARGET_ROOT/$target_rel" || true
  PATH_REALPATH="$GUARD_REALPATH"
  [[ -n "$PATH_REALPATH" ]]
}

# row_state sets STATE (KEPT, ABSENT, FAIL or REMOVE) for the current row, and
# KEPT_REASON or FAIL_REASON; GUARD_REALPATH is the guard's realpath of the
# recorded realpath. Reads only. F4: a recorded realpath that no longer
# resolves to itself is KEPT. Once the row's path resolves elsewhere (a
# retargeted scope symlink), the recorded realpath is removed only when its
# hash still matches the row, else KEPT. An unmoved row is removed whatever its
# hash (v1 rows have none).
row_state() {
  local digest
  KEPT_REASON=""
  if [[ "$GUARD_REALPATH" != "$realpath" ]]; then
    STATE="KEPT"; KEPT_REASON="moved; the recorded realpath now resolves elsewhere"
  elif [[ ! -e "$realpath" ]]; then
    STATE="ABSENT"
  elif { [[ "$unit" == "dir" ]] && [[ ! -d "$realpath" ]]; } \
    || { [[ "$unit" == "file" ]] && [[ -d "$realpath" ]]; }; then
    STATE="FAIL"; FAIL_REASON="target is not a $unit"
  elif [[ "$PATH_REALPATH" == "$realpath" ]]; then
    STATE="REMOVE"
  elif digest="$(convert hash "$realpath")" && [[ "${digest%%"$TAB"*}" == "$hash" ]]; then
    STATE="REMOVE"
  else
    STATE="KEPT"; KEPT_REASON="moved; the recorded hash does not match"
  fi
}

# dry_run_row prints the current row's one [DRY-RUN] line with the delete
# verdict for the recorded realpath: BLOCKED where the real run blocks (the
# guard fails while locating the row, or blocks the recorded realpath), else
# KEPT (with its reason) or REMOVE. An approve: verdict keeps its state, since
# the approval is asked at uninstall time.
dry_run_row() {
  local verdict
  if ! locate_row; then
    say "$C_SKIP" "[DRY-RUN]" "BLOCKED $target_rel $GUARD_VERDICT"
    return 0
  fi
  guard_path delete "$realpath" || true
  verdict="$GUARD_VERDICT"
  if [[ "$verdict" == blocked:* ]]; then
    say "$C_SKIP" "[DRY-RUN]" "BLOCKED $target_rel $verdict"
    return 0
  fi
  row_state
  if [[ "$STATE" == "KEPT" ]]; then
    say "$C_SKIP" "[DRY-RUN]" "KEPT $target_rel $verdict ($KEPT_REASON)"
  else
    say "$C_SKIP" "[DRY-RUN]" "REMOVE $target_rel $verdict"
  fi
}

# uninstall_row deletes the current tracker row's recorded realpath once the
# guard allows it and row_state says REMOVE. A KEPT row and its file stay.
uninstall_row() {
  if [[ "$unit" == "config" ]]; then
    say "$C_SKIP" "[SKIP]" "$target_rel (config leaves are not removed by this version)"
    SKIPPED=$((SKIPPED + 1))
    return 0
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then dry_run_row; return 0; fi
  if ! locate_row; then report_blocked "$GUARD_VERDICT"; return 0; fi
  if ! guard_approved delete "$realpath"; then report_blocked "$GUARD_VERDICT"; return 0; fi
  row_state
  case "$STATE" in
    KEPT)   say "$C_SKIP" "[KEPT]" "$target_rel ($KEPT_REASON)"
            KEPT=$((KEPT + 1)) ;;
    ABSENT) say "$C_SKIP" "[SKIP]" "$target_rel (not present)"
            SKIPPED=$((SKIPPED + 1))
            remove_row ;;
    FAIL)   report_failed "$FAIL_REASON" ;;
    REMOVE) if ! rm -rf -- "$realpath"; then report_failed "delete failed"; return 0; fi
            say "$C_OK" "[REMOVED]" "$target_rel"
            REMOVED=$((REMOVED + 1))
            record_change removed
            remove_row ;;
  esac
}

# --- Argument parsing --------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --global)    SCOPE="global"; shift ;;
    --project)   SCOPE="project"
                 PROJECT_PATH="${2:-}"
                 [[ -n "$PROJECT_PATH" ]] || die "--project requires a path"
                 shift 2 ;;
    --plugin)    [[ -n "${2:-}" ]] || die "--plugin requires a name"
                 SELECTED+=("$2"); shift 2 ;;
    --allow-repo) [[ -n "${2:-}" ]] || die "--allow-repo requires a path"
                 CLI_ALLOW_REPOS+=("$2"); shift 2 ;;
    --list)      ACTION="list"; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --force)     FORCE=1; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    -h|--help)   usage ;;
    --)          shift; break ;;
    -*)          die "Unknown option: $1 (try --help)" ;;
    *)           die "Unexpected positional arg: $1" ;;
  esac
done

# --- Pre-flight --------------------------------------------------------------
REPO_ROOT="$(find_repo_root)"
[[ -d "$REPO_ROOT" ]] || die "Repo root not found"
CONVERT="$REPO_ROOT/scripts/opencode/convert.py"

# `convert.py list --plugin` is the one place a --plugin name is resolved: a
# plugin.json id as is, a dir-derived name to its id with a WARN, anything
# else exit 2 with the valid ids. It prints only the selected plugins' rows.
list_args=(list --repo "$REPO_ROOT")
for p in ${SELECTED[@]+"${SELECTED[@]}"}; do list_args+=("--plugin=$p"); done
list_status=0
DISCOVERED="$(convert "${list_args[@]}")" || list_status=$?
case "$list_status" in
  0) ;;
  2) die "Unknown --plugin value; nothing was changed" ;;
  *) die "convert.py list failed for $REPO_ROOT" ;;
esac

# --- --list ------------------------------------------------------------------
if [[ "$ACTION" == "list" ]]; then
  printf '%-22s %-10s %s\n' "PLUGIN" "KIND" "SOURCE"
  while IFS='|' read -r n k s; do
    if [[ -n "$n" ]]; then printf '%-22s %-10s %s\n' "$n" "$k" "$s"; fi
  done <<<"$DISCOVERED"
  exit 0
fi

# --- Selected plugins: from here on, the plugin.json ids of the listed rows ---
if [[ ${#SELECTED[@]} -gt 0 ]]; then
  SELECTED=()
  while IFS= read -r p; do SELECTED+=("$p"); done < <(awk -F'|' 'NF && !seen[$1]++ { print $1 }' <<<"$DISCOVERED")
  [[ ${#SELECTED[@]} -gt 0 ]] || die "convert.py list printed no rows for the --plugin selection; nothing was changed"
fi

# --- Validate --allow-repo paths ---------------------------------------------
for r in ${CLI_ALLOW_REPOS[@]+"${CLI_ALLOW_REPOS[@]}"}; do
  real="$(canonical_dir "$r")" || die "--allow-repo $r: not a directory"
  if [[ "$real" == *:* || "$real" =~ [[:cntrl:]] ]]; then
    die "--allow-repo $r: a path with ':' or a control character cannot be recorded"
  fi
  APPROVED_REPOS+=("$real")
done

# --- Resolve scope -----------------------------------------------------------
if [[ -z "$SCOPE" ]]; then
  if [[ ! -t 0 ]]; then
    die "No scope given. Use --global, --project <path>, --list, or --help."
  fi
  hdr "Install scope?"
  echo "  1) global  → \${XDG_CONFIG_HOME:-~/.config}/opencode/"
  echo "  2) project → <path>/.opencode/"
  read -r -p "Choose [1/2]: " choice
  case "$choice" in
    1|g|G) SCOPE="global" ;;
    2|p|P) SCOPE="project"
           read -r -p "Project path: " PROJECT_PATH
           [[ -n "$PROJECT_PATH" ]] || die "Project path required" ;;
    *) die "Invalid choice" ;;
  esac
fi

GLOBAL_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
if [[ "$SCOPE" == "project" ]]; then
  [[ "$PROJECT_PATH" == /* ]] || PROJECT_PATH="$PWD/$PROJECT_PATH"
  if [[ ! -d "$PROJECT_PATH" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      dim "[dry-run] would mkdir -p $PROJECT_PATH"
    elif yn_prompt "Path '$PROJECT_PATH' does not exist. Create it? [y/N] "; then
      mkdir -p "$PROJECT_PATH"
    else
      die "Aborted"
    fi
  fi
  TARGET_ROOT="$PROJECT_PATH/.opencode"
  OTHER_SCOPE="global"; OTHER_TRACKER="$GLOBAL_ROOT/$TRACKER_NAME"
else
  TARGET_ROOT="$GLOBAL_ROOT"
  # A global run's other scope is the project in the current dir.
  OTHER_SCOPE="project"; OTHER_TRACKER="$PWD/.opencode/$TRACKER_NAME"
fi

TRACKER="$TARGET_ROOT/$TRACKER_NAME"

# --- Tracker (validated before anything else touches the scope) --------------
TRACKER_ROWS="$(convert tracker read --tracker "$TRACKER")" \
  || die "Tracker $TRACKER is invalid; nothing was changed"
if [[ -f "$TRACKER" ]]; then
  header_repos="$(sed -n 's/^# allowed_repos: //p' "$TRACKER" | head -n 1)"
  IFS=':' read -r -a header_repo_list <<<"$header_repos"
  for r in ${header_repo_list[@]+"${header_repo_list[@]}"}; do
    if [[ -n "$r" ]] && ! in_list "$r" ${APPROVED_REPOS[@]+"${APPROVED_REPOS[@]}"}; then APPROVED_REPOS+=("$r"); fi
  done
fi

# Stage, rows files and backups-in-flight live outside the scope and the repo.
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/opencode-setup.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT
STAGE_DIR="$WORK_DIR/stage"
ADD_ROWS="$WORK_DIR/add.rows"
REMOVE_ROWS="$WORK_DIR/remove.rows"
mkdir "$STAGE_DIR"
: >"$ADD_ROWS"
: >"$REMOVE_ROWS"

# --- Uninstall ---------------------------------------------------------------
if [[ "$ACTION" == "uninstall" ]]; then
  hdr "Uninstall from $TARGET_ROOT"
  [[ -f "$TRACKER" ]] || die "No tracker at $TRACKER. Refusing to uninstall (safety)."
  [[ "$DRY_RUN" -eq 1 ]] || preflight_tracker
  # Rows whose realpath lies outside the current roots never reach this loop:
  # `tracker read` keeps them in the file and warns (F4 [KEPT]).
  UNINSTALL_ROWS="$(selected_rows <<<"$TRACKER_ROWS")"
  while IFS="$TAB" read -r unit plugin target_rel realpath hash repo <&3; do
    [[ -n "$unit" ]] || continue
    uninstall_row
  done 3<<<"$UNINSTALL_ROWS"
  if [[ "$DRY_RUN" -eq 0 && -s "$REMOVE_ROWS" ]]; then
    write_tracker --add "$ADD_ROWS" --remove "$REMOVE_ROWS"
  fi
  hdr "Uninstall summary"
  printf '  removed: %d\n  kept:    %d\n  skipped: %d\n  blocked: %d\n  failed:  %d\n' \
    "$REMOVED" "$KEPT" "$SKIPPED" "$BLOCKED" "$FAILED"
  repo_summary
  if [[ "$BLOCKED" -gt 0 || "$FAILED" -gt 0 ]]; then exit 1; fi
  exit 0
fi

# --- Install -----------------------------------------------------------------
hdr "Install to $TARGET_ROOT"
[[ "$DRY_RUN" -eq 1 ]] && dim "[dry-run] no files will be written"
[[ "$DRY_RUN" -eq 1 ]] || preflight_tracker

plan_args=(plan --repo "$REPO_ROOT" --scope-root "$TARGET_ROOT" --scope "$SCOPE" --stage "$STAGE_DIR")
if [[ "$SCOPE" == "project" ]]; then plan_args+=(--project-dir "$PROJECT_PATH"); fi
for p in ${SELECTED[@]+"${SELECTED[@]}"}; do plan_args+=(--plugin "$p"); done
for r in ${APPROVED_REPOS[@]+"${APPROVED_REPOS[@]}"}; do plan_args+=(--allow-repo "$r"); done
plan_status=0
PLAN="$(convert "${plan_args[@]}")" || plan_status=$?
case "$plan_status" in
  0) ;;
  3) FAILED=$((FAILED + 1)) ;;  # rejected units were reported by plan; the others install
  *) die "convert.py plan failed (exit $plan_status); nothing was written" ;;
esac
double_load_notices

BACKUP_TS="$(date -u +%Y%m%dT%H%M%SZ)"
while IFS="$TAB" read -r plugin unit stage_rel target_rel hash realpath repo verdict <&3; do
  [[ -n "$plugin" ]] || continue
  if ! install_unit; then ABORTED=1; break; fi
done 3<<<"$PLAN"

# --- Write tracker -----------------------------------------------------------
if [[ "$DRY_RUN" -eq 0 && -s "$ADD_ROWS" ]]; then
  write_tracker --add "$ADD_ROWS" --header \
    "installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" "repo_root=$REPO_ROOT" "scope=$SCOPE" \
    "scope_root=$TARGET_ROOT" "payload_root=$PAYLOAD_NAMESPACE" "allowed_repos=$(allowed_repos_value)"
fi
[[ "$ABORTED" -eq 0 ]] || die "Aborted by user"

# --- Summary -----------------------------------------------------------------
hdr "Summary"
printf '  written:  %d\n' "$WRITTEN"
printf '  same:     %d\n' "$SAME"
printf '  skipped:  %d\n' "$SKIPPED"
printf '  blocked:  %d\n' "$BLOCKED"
printf '  failed:   %d\n' "$FAILED"
repo_summary
if [[ "$BLOCKED" -gt 0 || "$FAILED" -gt 0 ]]; then EXIT_STATUS=1; else EXIT_STATUS=0; fi

# --- Hints for plugins that need extra config -------------------------------
plugin_in_scope() {
  local name="$1"
  if [[ ${#SELECTED[@]} -gt 0 ]]; then
    printf '%s\n' "${SELECTED[@]}" | grep -Fxq "$name"
  else
    grep -q "^${name}|" <<<"$DISCOVERED"
  fi
}

if plugin_in_scope "gh-issue-to-pr" && [[ "$DRY_RUN" -ne 1 ]]; then
  hdr "Add this to your opencode.json (manual)"
  cat <<'EOF'
{
  "agent": {
    "gh-issue-to-pr": {
      "description": "Drives a single GitHub issue end-to-end to a merged PR",
      "mode": "subagent",
      "permission": { "edit": "allow", "bash": "allow", "webfetch": "allow" }
    }
  }
}
EOF
fi

if plugin_in_scope "wandavision" && [[ "$DRY_RUN" -ne 1 ]]; then
  hdr "wandavision follow-up"
  dim "The reminder hook is now copied to plugins/, but the MCP wrapper still"
  dim "needs to be installed into the XDG data directory. Two steps:"
  dim ""
  dim "  1. ./setup-wandavision.sh   # copies wandavision/{bin,skill,opencode-plugin}/ ->"
  dim "                    #   ~/.local/share/com.jcchikikomori.llmworkflow/wandavision/"
  dim ""
  dim "  2. Add this MCP entry to opencode.json:"
  cat <<'EOF'
{
  "mcp": {
    "wandavision": {
      "type": "local",
      "command": ["{env:HOME}/.local/share/com.jcchikikomori.llmworkflow/wandavision/bin/run-wandavision.sh"],
      "enabled": true
    }
  }
}
EOF
  dim ""
  dim "The /wandavision slash command lives in the dotfiles project (or copy"
  dim "wandavision/skill/wandavision/SKILL.md's command-file equivalent into"
  dim "~/.config/opencode/commands/ yourself). See plugin-wandavision/README.md."
fi

log ""
log "Done. Reload OpenCode to pick up the new plugins."
exit "$EXIT_STATUS"
