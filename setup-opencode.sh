#!/usr/bin/env bash
#
# setup-opencode.sh — Install OpenCode-compatible plugins from this
# marketplace into either ~/.config/opencode/ (global) or
# <project>/.opencode/ (per-project).
#
# Auto-discovers plugins by scanning plugin-*/{plugins/*.ts,commands/*.md,
# agents/opencode-*.md} under the repo root. No external manifest.
#
# Usage:
#   setup-opencode.sh [options]
#
# Options:
#   --global                Install to ~/.config/opencode/
#   --project <path>        Install to <path>/.opencode/
#   --plugin <name>         Install only this plugin (repeatable)
#   --list                  List discovered OpenCode-compatible plugins
#   --dry-run               Show what would happen, write nothing
#   --force                 Overwrite existing files without prompting
#   --uninstall             Remove files this script installed
#                           (combine with --global or --project <path>)
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
REPO_ROOT=""
TRACKER_NAME=".opencode-setup-tracker"

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

# Discover OpenCode-compatible plugins.
# Emits: <name>|<kind>|<abs-source-path>   kind ∈ plugins|commands|agents
discover_plugins() {
  local entry name plugins_dir commands_dir agents_dir f
  for entry in "$REPO_ROOT"/plugin-*/; do
    [[ -d "$entry" ]] || continue
    entry="${entry%/}"
    name="${entry##*/}"; name="${name#plugin-}"
    plugins_dir="$entry/plugins"
    commands_dir="$entry/commands"
    agents_dir="$entry/agents"
    if [[ -d "$plugins_dir" ]]; then
      for f in "$plugins_dir"/*.ts; do
        [[ -f "$f" ]] || continue
        printf '%s\n' "${name}|plugins|${f}"
      done
    fi
    if [[ -d "$commands_dir" ]]; then
      for f in "$commands_dir"/*.md; do
        [[ -f "$f" ]] || continue
        printf '%s\n' "${name}|commands|${f}"
      done
    fi
    if [[ -d "$agents_dir" ]]; then
      for f in "$agents_dir"/opencode-*.md; do
        [[ -f "$f" ]] || continue
        printf '%s\n' "${name}|agents|${f}"
      done
    fi
  done | sort -u
}

plugin_selected() {
  local name="$1"
  if [[ ${#SELECTED[@]} -eq 0 ]]; then return 0; fi
  local p; for p in "${SELECTED[@]}"; do [[ "$p" == "$name" ]] && return 0; done
  return 1
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
DISCOVERED="$(discover_plugins)"

# --- --list ------------------------------------------------------------------
if [[ "$ACTION" == "list" ]]; then
  hdr "OpenCode-compatible plugins in $REPO_ROOT"
  if [[ -z "$DISCOVERED" ]]; then
    dim "(none found)"
  else
    printf '%-22s %-10s %s\n' "PLUGIN" "KIND" "SOURCE"
    printf '%-22s %-10s %s\n' "------" "----" "------"
    while IFS='|' read -r n k s; do
      [[ -z "$n" ]] && continue
      rel="${s#"$REPO_ROOT"/}"
      printf '%-22s %-10s %s\n' "$n" "$k" "$rel"
    done <<<"$DISCOVERED"
  fi
  printf '\n'
  dim "Run with --global or --project <path> to install."
  exit 0
fi

# --- Validate --plugin names against discovery --------------------------------
if [[ ${#SELECTED[@]} -gt 0 ]]; then
  available="$(printf '%s\n' "$DISCOVERED" | cut -d'|' -f1 | sort -u)"
  for p in "${SELECTED[@]}"; do
    if ! grep -Fxq "$p" <<<"$available"; then
      pretty="$(printf '%s, ' $available | sed 's/, $//')"
      die "Plugin '$p' is not OpenCode-compatible. Available: $pretty"
    fi
  done
fi

# --- Resolve scope -----------------------------------------------------------
if [[ -z "$SCOPE" ]]; then
  if [[ ! -t 0 ]]; then
    die "No scope given. Use --global, --project <path>, --list, or --help."
  fi
  hdr "Install scope?"
  echo "  1) global  → ~/.config/opencode/"
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

if [[ "$SCOPE" == "project" ]]; then
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
else
  TARGET_ROOT="$HOME/.config/opencode"
fi

TRACKER="$TARGET_ROOT/$TRACKER_NAME"

# --- Uninstall ---------------------------------------------------------------
if [[ "$ACTION" == "uninstall" ]]; then
  hdr "Uninstall from $TARGET_ROOT"
  [[ -f "$TRACKER" ]] || die "No tracker at $TRACKER. Refusing to uninstall (safety)."
  removed=0; skipped=0
  while IFS= read -r rel; do
    [[ -z "$rel" || "$rel" == \#* ]] && continue
    target="$TARGET_ROOT/$rel"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '%s[DRY-RUN]%s %s\n' "$C_SKIP" "$C_RST" "$rel"
    elif [[ -f "$target" ]]; then
      rm -f "$target"
      printf '%s[REMOVED]%s %s\n' "$C_OK" "$C_RST" "$rel"
      removed=$((removed + 1))
    else
      printf '%s[SKIP]%s    %s (not present)\n' "$C_SKIP" "$C_RST" "$rel"
      skipped=$((skipped + 1))
    fi
  done < "$TRACKER"
  # Tidy empty subdirs.
  for sub in plugins commands agents; do
    [[ -d "$TARGET_ROOT/$sub" ]] || continue
    [[ -z "$(ls -A "$TARGET_ROOT/$sub" 2>/dev/null || true)" ]] \
      && rmdir "$TARGET_ROOT/$sub" 2>/dev/null || true
  done
  [[ "$DRY_RUN" -eq 0 ]] && rm -f "$TRACKER"
  hdr "Uninstall summary"
  printf '  removed: %d\n  skipped: %d\n' "$removed" "$skipped"
  exit 0
fi

# --- Install -----------------------------------------------------------------
hdr "Install to $TARGET_ROOT"
[[ "$DRY_RUN" -eq 1 ]] && dim "[dry-run] no files will be written"

copied=(); skipped=(); failed=()
while IFS='|' read -r name kind src; do
  [[ -z "$name" ]] && continue
  plugin_selected "$name" || continue
  target_dir="$TARGET_ROOT/$kind"
  target_file="$target_dir/$(basename "$src")"
  rel_target="$kind/$(basename "$src")"

  if [[ -f "$target_file" ]]; then
    if [[ "$FORCE" -ne 1 && "$DRY_RUN" -ne 1 ]]; then
      if [[ ! -t 0 ]]; then
        printf '%s[SKIP]%s    %s (exists; pass --force to overwrite)\n' \
          "$C_SKIP" "$C_RST" "$rel_target"
        skipped+=("$rel_target")
        continue
      fi
      read -r -p "  $rel_target exists — (s)kip / (o)verwrite / (a)bort: " choice || choice="a"
      case "$choice" in
        o|O) ;;  # fall through to overwrite
        a|A) die "Aborted by user" ;;
        *)   printf '%s[SKIP]%s    %s\n' "$C_SKIP" "$C_RST" "$rel_target"
             skipped+=("$rel_target"); continue ;;
      esac
    fi
    label="[OVERWRITE]"
  else
    label="[OK]"
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '%s[DRY-RUN]%s %s\n' "$C_SKIP" "$C_RST" "$rel_target"
    continue
  fi

  if mkdir -p "$target_dir" && cp "$src" "$target_file"; then
    printf '%s%s%s %s\n' "$C_OK" "$label" "$C_RST" "$rel_target"
    copied+=("$rel_target")
  else
    printf '%s[FAIL]%s   %s\n' "$C_ERR" "$C_RST" "$rel_target"
    failed+=("$rel_target")
  fi
done <<<"$DISCOVERED"

# --- Write tracker -----------------------------------------------------------
if [[ "$DRY_RUN" -ne 1 && ${#copied[@]} -gt 0 ]]; then
  tmp="$(mktemp)"
  {
    printf '# setup-opencode.sh tracker v1\n'
    printf '# installed_at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '# repo_root: %s\n' "$REPO_ROOT"
    printf '# scope: %s\n' "$SCOPE"
    for f in "${copied[@]}"; do printf '%s\n' "$f"; done
  } > "$tmp"
  mv "$tmp" "$TRACKER"
  dim "Tracker: $TRACKER"
fi

# --- Summary -----------------------------------------------------------------
hdr "Summary"
printf '  copied:   %d\n' "${#copied[@]}"
printf '  skipped:  %d\n' "${#skipped[@]}"
printf '  failed:   %d\n' "${#failed[@]}"

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
  dim "Run /wandavision after install — it prints the docker build command"
  dim "and the opencode.json mcp block to add for the mcp-vision server."
fi

log ""
log "Done. Reload OpenCode to pick up the new plugins."
exit 0
