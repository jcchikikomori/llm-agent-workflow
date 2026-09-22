#!/bin/sh
# commit-guard :: await_commit.sh -- bounded watcher for a delegated git op.
#
# Armed by the commit-guard PreToolUse hook via the Bash tool with
# run_in_background: true. ALL state arrives in argv: the hook snapshots the
# repo at block time, so there is no race between the block and the arming.
#
# stdout contract: exactly ONE line, then exit.
#   commit-guard: DONE    <op> <new-head-or-ref>   exit 0
#   commit-guard: ABORTED <op> <evidence>          exit 3
#   commit-guard: TIMEOUT <op> <reason>            exit 4
#   commit-guard: ERROR   <op> <reason>            exit 64 usage / 65 env / 66 signal
#
# Any exit other than 0/3/4 means NO VERDICT WAS REACHED. Callers must ask the
# user; they must never infer repo state from a missing verdict.
#
# POSIX sh only: no arrays, no [[ ]], no local.

set -u

# 10 polls. Checks land at t = 5 15 30 60 120 240 480 840 1320 1800 seconds,
# so the tenth is the hard 30-minute ceiling. COMMIT_GUARD_SCHEDULE overrides it
# (the only practical way to exercise the timeout branch in a test).
SCHEDULE="${COMMIT_GUARD_SCHEDULE:-5 10 15 30 60 120 240 360 480 480}"

op=""; dir=""; arg_git_dir=""; pre_head=""; pre_markers=""
pre_orig_head=""; pre_seq_head=""; tag_name=""; pre_tag=""; result_file=""

emit() {
    printf 'commit-guard: %s\n' "$*"
    if [ -n "$result_file" ]; then
        printf 'commit-guard: %s\n' "$*" >>"$result_file" 2>/dev/null || :
    fi
}
debug() {
    if [ -n "${COMMIT_GUARD_DEBUG:-}" ]; then
        printf '[commit-guard] %s\n' "$*" >&2
    fi
}
fail_usage() { emit "ERROR ${op:-unknown} $1"; exit 64; }
fail_env()   { emit "ERROR ${op:-unknown} $1"; exit 65; }

trap 'emit "ERROR ${op:-unknown} watcher-signalled"; exit 66' HUP INT TERM

while [ $# -gt 0 ]; do
    key=$1
    case "$key" in
        --op|--dir|--git-dir|--pre-head|--pre-markers|--pre-orig-head|\
--pre-seq-head|--tag|--pre-tag|--result-file)
            if [ $# -lt 2 ]; then fail_usage "missing-value:${key#--}"; fi
            val=$2
            shift 2
            ;;
        *) fail_usage "unknown-argument:${key#--}" ;;
    esac
    case "$key" in
        --op)            op=$val ;;
        --dir)           dir=$val ;;
        --git-dir)       arg_git_dir=$val ;;
        --pre-head)      pre_head=$val ;;
        --pre-markers)   pre_markers=$val ;;
        --pre-orig-head) pre_orig_head=$val ;;
        --pre-seq-head)  pre_seq_head=$val ;;
        --tag)           tag_name=$val ;;
        --pre-tag)       pre_tag=$val ;;
        --result-file)   result_file=$val ;;
    esac
done

case "$op" in
    commit|amend|merge|cherry-pick|revert|rebase|am|tag) : ;;
    "") fail_usage "missing:op" ;;
    *)  fail_usage "unsupported-op" ;;
esac
if [ -z "$dir" ];      then fail_usage "missing:dir"; fi
if [ -z "$pre_head" ]; then fail_usage "missing:pre-head"; fi
if [ "$op" = tag ] && [ -z "$tag_name" ]; then fail_usage "missing:tag"; fi

if ! command -v git >/dev/null 2>&1; then fail_env "git-not-found"; fi
if [ ! -d "$dir" ]; then fail_env "dir-missing"; fi
if ! git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then fail_env "not-a-git-repo"; fi

if [ -n "$result_file" ]; then
    mkdir -p "$(dirname "$result_file")" 2>/dev/null || :
fi

# --- state readers -----------------------------------------------------------
# NEVER assume "$dir/.git": in a linked worktree or a submodule .git is a FILE,
# and the markers live under .git/worktrees/<name>/.
resolve_git_dir() {
    _d=$(git -C "$dir" rev-parse --absolute-git-dir 2>/dev/null) || _d=""
    if [ -z "$_d" ]; then _d=$arg_git_dir; fi
    printf '%s' "$_d"
}
# Unborn HEAD (fresh repo, no commits): rev-parse fails -> sentinel.
resolve_head() {
    _h=$(git -C "$dir" rev-parse --verify -q HEAD 2>/dev/null) || _h=""
    if [ -z "$_h" ]; then _h="unborn"; fi
    printf '%s' "$_h"
}
resolve_tag() {
    _t=$(git -C "$dir" rev-parse -q --verify "refs/tags/$tag_name" 2>/dev/null) || _t=""
    printf '%s' "$_t"
}
marker_present() {  # <gitdir> <marker>
    case "$2" in
        rebase-merge|rebase-apply|sequencer) [ -d "$1/$2" ] ;;
        *)                                   [ -e "$1/$2" ] ;;
    esac
}
relevant_markers() {
    case "$op" in
        merge)        printf 'MERGE_HEAD' ;;
        cherry-pick)  printf 'CHERRY_PICK_HEAD sequencer' ;;
        revert)       printf 'REVERT_HEAD sequencer' ;;
        rebase|am)    printf 'rebase-merge rebase-apply' ;;
        commit|amend) printf 'MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD' ;;
        tag)          printf '' ;;
    esac
}
any_relevant_marker() {
    for _m in $(relevant_markers); do
        if marker_present "$1" "$_m"; then return 0; fi
    done
    return 1
}
# Fingerprint of "the operation moved forward", so a rebase that stopped at the
# NEXT conflict reports advanced-but-unfinished instead of no-activity.
progress_token() {
    _gd=$1; _out=""
    for _f in rebase-merge/msgnum rebase-apply/next; do
        if [ -f "$_gd/$_f" ]; then _out="$_out $_f=$(cat "$_gd/$_f" 2>/dev/null)"; fi
    done
    for _f in rebase-merge/done sequencer/todo; do
        if [ -f "$_gd/$_f" ]; then
            _out="$_out $_f=$(wc -l <"$_gd/$_f" 2>/dev/null | tr -d ' ')"
        fi
    done
    printf '%s' "$_out"
}

# --- seed sticky state from the hook's block-time snapshot -------------------
saw_marker=0
pre_marker_list=$(printf '%s' "$pre_markers" | tr ',' ' ')
for m in $(relevant_markers); do
    for p in $pre_marker_list; do
        if [ "$m" = "$p" ]; then saw_marker=1; fi
    done
done

head=$pre_head
head_moved=0
progress_moved=0
base_progress=""
base_progress_set=0
miss=0
repo_note=""
poll=0

for wait in $SCHEDULE; do
    poll=$((poll + 1))
    sleep "$wait"

    gd=$(resolve_git_dir)
    if [ -z "$gd" ] || [ ! -d "$gd" ]; then
        miss=$((miss + 1))
        debug "poll $poll: git dir unreadable ($miss)"
        if [ "$miss" -ge 3 ]; then fail_env "repo-unreadable"; fi
        continue
    fi
    miss=0
    if [ -n "$arg_git_dir" ] && [ "$gd" != "$arg_git_dir" ]; then
        repo_note=" git-dir-changed"
    fi

    head=$(resolve_head)
    if [ "$head" != "$pre_head" ]; then head_moved=1; fi
    if any_relevant_marker "$gd"; then saw_marker=1; fi

    prog=$(progress_token "$gd")
    if [ "$base_progress_set" -eq 0 ]; then
        base_progress=$prog
        base_progress_set=1
    elif [ "$prog" != "$base_progress" ]; then
        progress_moved=1
    fi
    debug "poll=$poll head=$head saw_marker=$saw_marker prog=$prog"

    case "$op" in
    commit|amend)
        if [ "$head" != "$pre_head" ]; then emit "DONE $op $head"; exit 0; fi
        # A commit made *during* a merge/pick/revert: if that op's marker was
        # present at block time and is now gone with HEAD unchanged, the user
        # aborted the surrounding operation instead of committing.
        if [ "$saw_marker" -eq 1 ] && ! any_relevant_marker "$gd"; then
            emit "ABORTED $op in-progress-op-cleared-head-unchanged"; exit 3
        fi
        ;;
    merge)
        if [ "$head" != "$pre_head" ] && ! marker_present "$gd" MERGE_HEAD; then
            emit "DONE merge $head"; exit 0
        fi
        if [ "$saw_marker" -eq 1 ] && [ "$head" = "$pre_head" ] &&
           ! marker_present "$gd" MERGE_HEAD; then
            emit "ABORTED merge merge-head-gone-head-unchanged"; exit 3
        fi
        ;;
    cherry-pick|revert)
        case "$op" in
            cherry-pick) mk=CHERRY_PICK_HEAD ;;
            *)           mk=REVERT_HEAD ;;
        esac
        if ! marker_present "$gd" "$mk" && ! marker_present "$gd" sequencer; then
            # Multi-commit sequence: --abort rewinds to the PRE-SEQUENCE head,
            # which can be *behind* pre_head if earlier picks already landed.
            # Without this check that rollback looks like DONE.
            if [ -n "$pre_seq_head" ] && [ "$head" = "$pre_seq_head" ] &&
               [ "$head" != "$pre_head" ]; then
                emit "ABORTED $op rolled-back-to-pre-sequence"; exit 3
            fi
            if [ "$head" != "$pre_head" ]; then emit "DONE $op $head"; exit 0; fi
            if [ "$saw_marker" -eq 1 ]; then
                emit "ABORTED $op sequencer-cleared-head-unchanged"; exit 3
            fi
        fi
        ;;
    rebase|am)
        # HEAD may legitimately be unchanged on a no-op rebase, so absence of
        # the state dirs is the completion signal -- but --abort ALSO removes
        # them, so orig-head is what separates finished from aborted.
        if marker_present "$gd" rebase-merge || marker_present "$gd" rebase-apply; then
            :
        elif [ -n "$pre_orig_head" ]; then
            if [ "$head" = "$pre_orig_head" ]; then
                emit "ABORTED $op reset-to-orig-head"; exit 3
            elif [ "$head" = "$pre_head" ]; then
                emit "ABORTED $op quit-left-partial-state"; exit 3
            else
                emit "DONE $op $head"; exit 0
            fi
        else
            if [ "$head" != "$pre_head" ]; then
                emit "DONE $op $head"; exit 0
            elif [ "$saw_marker" -eq 1 ]; then
                emit "ABORTED $op started-then-cleared-head-unchanged"; exit 3
            fi
        fi
        ;;
    tag)
        now_tag=$(resolve_tag)
        if [ -n "$now_tag" ] && [ "$now_tag" != "$pre_tag" ]; then
            emit "DONE tag refs/tags/$tag_name $now_tag"; exit 0
        fi
        ;;
    esac
done

# --- exhausted: report WHY, never guess -------------------------------------
reason="no-activity"
gd=$(resolve_git_dir)
if [ -n "$gd" ] && [ -d "$gd" ] && any_relevant_marker "$gd"; then
    reason="still-in-progress"
elif [ "$head_moved" -eq 1 ]; then
    reason="head-moved-predicate-unmet"
elif [ "$progress_moved" -eq 1 ]; then
    reason="advanced-but-unfinished"
fi
emit "TIMEOUT $op ${reason}${repo_note}"
exit 4
