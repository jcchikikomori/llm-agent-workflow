#!/usr/bin/env bash
# headersHelper for .mcp.json.
#
# Claude Code runs this fresh on every connect and reconnect (10 s budget),
# merges the JSON it prints into the HTTP headers, and re-runs it on a 401.
# That makes it the right place to make sure the hub is actually up before
# the first request: an idle-stopped hub gets started here, and we wait a
# few seconds for /healthz so most connects find it ready.
#
# stdout is the JSON object and nothing else; hub.sh already keeps its own
# chatter on stderr, and the token subcommand prints exactly the token.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Best effort only: a cold CUDA start can outlast the helper's budget. The
# header is still printed, Claude Code retries the connection, and a later
# `/mcp` reconnect runs this helper again.
"$HERE/hub.sh" ensure --wait "${MEMPALACE_HUB_HELPER_WAIT_SECONDS:-8}" >&2 || true

token="$("$HERE/hub.sh" token 2>/dev/null || true)"
if [ -z "$token" ]; then
    printf '{}\n'
    exit 0
fi
printf '{"Authorization": "Bearer %s"}\n' "$token"
