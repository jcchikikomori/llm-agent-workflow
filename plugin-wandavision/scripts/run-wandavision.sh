#!/usr/bin/env bash
# wandavision plugin redirector (Claude Code marketplace shim).
#
# The actual MCP wrapper lives in the XDG data directory at
# ~/.local/share/com.jcchikikomori.llmworkflow/wandavision/bin/run-wandavision.sh.
# This file just hands off so the marketplace install still spawns the same
# server, regardless of where the plugin was placed.
#
# Run ./setup-wandavision.sh from the repo root before this
# redirector will work — it copies the source files into the XDG location.
set -euo pipefail

XDG_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/com.jcchikikomori.llmworkflow/wandavision"
WRAPPER="$XDG_ROOT/bin/run-wandavision.sh"

if [ ! -x "$WRAPPER" ]; then
  printf '[wandavision] %s\n' "XDG wrapper not found at: $WRAPPER" >&2
  printf '[wandavision] %s\n' "Run this once to populate the XDG install:" >&2
  printf '[wandavision] %s\n' "  $CLAUDE_PLUGIN_ROOT/../../../setup-wandavision.sh   # from repo root" >&2
  printf '[wandavision] %s\n' "Or copy wandavision/* into $XDG_ROOT/ manually." >&2
  exit 1
fi

exec "$WRAPPER" "$@"