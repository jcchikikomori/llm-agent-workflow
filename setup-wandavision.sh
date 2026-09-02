#!/usr/bin/env bash
# Install wandavision runtime files into the XDG data directory.
#
# Copies source files from ./wandavision/ (this repo) to
# ${XDG_DATA_HOME:-$HOME/.local/share}/com.jcchikikomori.llmworkflow/wandavision/
# so Claude Code and OpenCode share one canonical home.
#
# Idempotent: re-running overwrites the XDG copy with whatever is in ./wandavision/.
# Run from the repo root: ./setup-wandavision.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO_ROOT/wandavision"
DST="${XDG_DATA_HOME:-$HOME/.local/share}/com.jcchikikomori.llmworkflow/wandavision"

if [ ! -d "$SRC" ]; then
  printf '[wandavision install] source not found: %s\n' "$SRC" >&2
  printf '[wandavision install] run this from the llm-agent-workflow repo root.\n' >&2
  exit 1
fi

printf '[wandavision install] source: %s\n' "$SRC"
printf '[wandavision install] target: %s\n' "$DST"

mkdir -p "$DST/bin" "$DST/skill/wandavision" "$DST/opencode-plugin"

# Copy each subtree with -v so the user sees what moved.
cp -v "$SRC"/bin/run-wandavision.sh  "$DST/bin/run-wandavision.sh"
cp -v "$SRC"/bin/warm-cache.sh       "$DST/bin/warm-cache.sh"
cp -v "$SRC"/skill/wandavision/SKILL.md "$DST/skill/wandavision/SKILL.md"
cp -v "$SRC"/opencode-plugin/opencode-wandavision.ts "$DST/opencode-plugin/opencode-wandavision.ts"

chmod +x "$DST/bin/run-wandavision.sh" "$DST/bin/warm-cache.sh"

cat <<NEXT

[wandavision install] done.

Next steps:
  1. Warm the model cache (one-time, per machine):
       $DST/bin/warm-cache.sh
  2. OpenCode users: ensure ~/.config/opencode/opencode.jsonc has the
     'wandavision' MCP entry pointing at $DST/bin/run-wandavision.sh.
     The dotfiles project should already ship this entry.
  3. Restart opencode (or Claude Code) so the new entry / files take effect.
  4. Verify: list MCP resources - should show 'wandavision'.

Re-running this script is safe; it overwrites the XDG copy with whatever
is currently in ./wandavision/. Treat this repo as the source of truth.
NEXT