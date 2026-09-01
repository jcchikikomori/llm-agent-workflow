# Vendored upstream hooks

Verbatim copies of three MemPalace hook scripts, MIT licensed, copyright (c)
2026 MemPalace Contributors. See this plugin's `NOTICE` for the full license
text and provenance.

| File | Lines | Fires on |
| ---- | ----- | -------- |
| `mempal_save_hook.sh` | 298 | `Stop` — asks Claude to save structured diary + palace entries |
| `mempal_precompact_hook.sh` | 198 | `PreCompact` — mines the transcript synchronously before compaction |
| `mempal_session_end_hook.sh` | 41 | `SessionEnd` — backgrounds `mempalace hook run --hook session-end` |

## Why vendored

The plugin's whole point is that the official mempalace plugin becomes
removable. These hooks live inside its checkout
(`~/.claude/plugins/marketplaces/mempalace/hooks/`), so uninstalling it breaks
any `settings.json` entry pointing there. Copying them in makes this plugin
self-contained.

## Why unmodified

The scripts already resolve their interpreter as
`$MEMPAL_PYTHON` → `command -v python3` → `python3`, and
`mempal_session_end_hook.sh` prefers a `mempalace` found on `PATH`. That is
enough to redirect them into the container without editing a line, so
`hooks.json` injects:

```text
PATH="${CLAUDE_PLUGIN_ROOT}/scripts/bin:$PATH"
MEMPAL_PYTHON="${CLAUDE_PLUGIN_ROOT}/scripts/bin/mempalace-python3"
```

Keeping them byte-identical means `diff` is a clean equality check, so drift
against upstream is trivially visible — which is also why there is no
provenance header prepended to the scripts themselves. That metadata lives here
and in `NOTICE` instead.

## Verifying they are unmodified

```bash
for f in mempal_save_hook.sh mempal_precompact_hook.sh mempal_session_end_hook.sh; do
  diff -q "$HOME/.claude/plugins/marketplaces/mempalace/hooks/$f" "$f"
done
```

## Refreshing from upstream

```bash
git clone --depth 1 https://github.com/MemPalace/mempalace.git /tmp/mempalace-src
cp /tmp/mempalace-src/hooks/mempal_*.sh .
```

Then re-read them: the plugin depends on three upstream behaviours that are not
contractual — the `MEMPAL_PYTHON`/`PATH` resolution order; the fact that state
and the `hooks.auto_save` toggle live under host `$HOME/.mempalace/` rather
than inside the palace; and **which argv shapes read stdin** (see below).
Update `NOTICE` with the new version and commit hash.

## The stdin trap

`docker run -i` streams the host's stdin into the container and drains it,
whether or not the container ever reads it. These hooks probe
`hooks.auto_save` with a `python3 -c ...` call *before* they run
`INPUT=$(cat)`. Attaching stdin to that probe swallows the hook payload, and
the hook then logs `Session unknown` with an empty `INPUT` — no error, no
traceback, nothing in `last_python_err.log`.

So `scripts/bin/mempalace-python3` attaches stdin only for non-`-c`
invocations. The current call sites:

| Call site | Reads stdin |
| --------- | ----------- |
| `-c "import json, ..."` (auto_save probe, save + precompact) | no |
| `-c "import mempalace"` (session_end probe) | no |
| `-m mempalace.hook_shell parse-stop` | **yes** |
| `-m mempalace.hook_shell parse-precompact` | **yes** |
| `-m mempalace.hook_shell count-human-messages <path>` | no |
| `-m mempalace hook run --hook session-end` | **yes** |

Re-audit that table on every refresh. The regression test:

```bash
CLAUDE_PLUGIN_ROOT=$(cd ../.. && pwd)
TP=$(ls -t ~/.claude/projects/*/*.jsonl | head -1)
printf '{"session_id":"stdin-test","transcript_path":"%s","stop_hook_active":false}' "$TP" \
  | env PATH="$CLAUDE_PLUGIN_ROOT/scripts/bin:$PATH" \
        MEMPAL_PYTHON="$CLAUDE_PLUGIN_ROOT/scripts/bin/mempalace-python3" \
        bash -x mempal_save_hook.sh 2>&1 | grep 'SESSION_ID='
```

Must print `SESSION_ID=stdin-test`, never `SESSION_ID=unknown`.

## Known host/container split

These hooks read `$HOME/.mempalace/config.json` for the `hooks.auto_save`
toggle and write logs to `$HOME/.mempalace/hook_state/` — both on the **host**,
because they run on the host and only shell out to the container. Palace
content, meanwhile, lives in the `mempalace-data` volume at
`/data/.mempalace`. So keep a host `~/.mempalace/config.json` if you want to
turn auto-save off; it is a config file, not a second palace.
