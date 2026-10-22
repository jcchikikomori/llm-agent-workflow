# ruby-lsp plugin

Ruby and Rails code intelligence for Claude Code. It gives Claude three things:

- **RuboCop diagnostics.** [ruby-lsp](https://github.com/Shopify/ruby-lsp) runs RuboCop and pushes offenses back after every `.rb` edit, along with go-to-definition, hover and references.
- **Reek smells.** A PostToolUse hook adds advisory [Reek](https://github.com/troessner/reek) smells to Claude's context.
- **A pre-write skill.** A short checklist, so Claude writes RuboCop-clean, smell-free Rails code on the first pass.

Why it exists: LLM-written Ruby (mine included, as Claude) tends to miss `frozen_string_literal`, nest `if`s instead of using guard clauses, and grow Feature Envy methods. Catching that at edit time is cheaper than a lint, fix, re-lint loop later. One diagnostic in context costs fewer tokens than a full `rubocop` run pasted back.

## Install

```bash
/plugin install ruby-lsp@llm-agent-workflow
/reload-plugins
```

**Install only one Ruby LSP plugin.** The official `ruby-lsp@claude-plugins-official` also registers `.rb`, and running both gives you two sets of diagnostics.

## Requirements

Add both gems to the project's `:development` group. That lets the wrapper run them in Docker with the project's own gem versions:

```ruby
group :development do
  gem 'ruby-lsp', require: false
  gem 'reek', require: false
end
```

Other requirements:

- **RuboCop config.** ruby-lsp picks RuboCop up from the bundle and uses the project's `.rubocop.yml`.
- **Python 3** runs the Reek hook. It uses only the standard library.
- **Add `.ruby-lsp/` to `.gitignore`.** ruby-lsp writes a composed bundle there on first launch.

## When ruby-lsp runs: shared, lazy, idle-stopping

A full ruby-lsp (a Rails container plus an index of every gem) is the heaviest thing this plugin owns. Before 0.2.0,
every session that touched Ruby once kept its own copy until the session ended. One RTO session held one for 2 h 16 min
after a single edit. Since 0.2.0, one copy per checkout runs only while Ruby is being written:

```text
session A ─stdio─ lsp_bridge.py ─┐
session B ─stdio─ lsp_bridge.py ─┼─ unix socket ─ lsp_hub.py (one per checkout)
                                 ┘                 └─ run-ruby-tool.sh lsp-backend
                                                      = ruby-lsp in container ruby-lsp-<key>
reek hook ─ run-ruby-tool.sh reek ─ docker exec ruby-lsp-<key> (compose run when it's down)
```

- **Bridge** (`scripts/lsp_bridge.py`, per session). Claude Code spawns it on the first Ruby edit and treats any exit as
  a crash, so it never exits on idle. It only pumps bytes between stdio and the hub's socket, and starts the hub when
  nothing answers.
- **Hub** (`scripts/lsp_hub.py`, per checkout). It speaks LSP to every bridge and to one backend:
  - `initialize` is answered from a capabilities cache. A session that never writes Ruby never starts the backend. The
    very first run has no cache, so it waits for one real start and saves the answer.
  - The backend starts on the first document notification or LSP request. Claude Code sends those only for Write, Edit
    and the LSP tool, never for a plain Read.
  - On start, the hub replays `initialize`, `initialized` and `didOpen` for the documents that triggered it. Other
    documents follow when they are touched.
  - After `RUBY_LSP_PLUGIN_IDLE_MINUTES` (default 15) without client traffic, it sends the backend `shutdown` and
    `exit`, then `docker rm -f ruby-lsp-<key>` as a fallback. The next Ruby write starts it again.
  - It exits 60 s after its last session disconnects.
  - **It turns pull diagnostics into pushes.** ruby-lsp 0.26 only answers `textDocument/diagnostic` requests, and
    Claude Code only listens for `publishDiagnostics`. After every document sync the hub pulls the report itself and
    pushes it to the sessions that hold the same text.
- **Detached.** The hub is double-forked with every `CLAUDE_*` variable removed, so stopping one session never stops the
  shared hub, and resource-guard never freezes it along with whichever session started it.
- **Solo mode.** With `RUBY_LSP_PLUGIN_SHARED=0`, without unix sockets (native Windows), or when the hub doesn't come up
  in 45 s, the bridge runs the same hub code in-process. That keeps it lazy and idle-stopping, but nothing is shared.
- **State.** `~/.claude/.ruby-lsp-plugin/hubs/<key>/` (mode 0700) holds `hub.sock`, `hub.lock` (with the hub's pid),
  `capabilities.json` and `hub.log`. The key is the first 12 hex digits of `sha256(realpath(project dir))`.

Sharing works per checkout. Two clones (say `RTO` and `RTO-qa`) are two checkouts and get two hubs, so the idle stop
does most of the saving there.

## How it runs: Docker first, host fallback

The LSP backend and the Reek hook go through `scripts/run-ruby-tool.sh`. It picks the first option that works:

1. **Docker.** Used when a compose file exists, the gem is in `Gemfile.lock`, `docker compose` works, the daemon answers, and a service is found. The service is `RUBY_LSP_PLUGIN_SERVICE` if set, otherwise the first of `web app rails api backend`. The command is:

   ```bash
   docker compose run --rm --no-deps -T -v "$PWD:$PWD" -w "$PWD" <service> bundle exec <tool>
   ```

2. **Host bundle.** `bundle exec <tool>`, used when the gem is in `Gemfile.lock`.
3. **Global binary.** `ruby-lsp` or `reek` from `PATH`.
4. **Nothing found.** Exit 127. The Reek hook then prints an install hint once per session.

How it behaves:

- **Identical-path mount.** The project is mounted at the same path inside the container as on the host. That keeps LSP file URIs and Reek paths valid on both sides, with no path translation.
- **No dependencies started.** `--no-deps` stops Postgres and Redis from booting just to lint a file.
- **Logging.** Every fallback reason goes to stderr as a `[ruby-lsp] ...` line. stdout carries JSON-RPC, so nothing else is written there.

| Env var | Effect |
| ------- | ------ |
| `RUBY_LSP_PLUGIN_SERVICE` | Compose service to run in |
| `RUBY_LSP_PLUGIN_FORCE_HOST=1` | Skip Docker entirely |
| `RUBY_LSP_PLUGIN_FORCE_DOCKER=1` | Exit 1 instead of falling back to the host |
| `RUBY_LSP_PLUGIN_IDLE_MINUTES` | Minutes without client traffic before the backend stops (default `15`, `0` never stops) |
| `RUBY_LSP_PLUGIN_SHARED=0` | Solo mode: one in-process hub per session, nothing shared |
| `RUBY_LSP_PLUGIN_HUB_LINGER` | Seconds the hub waits after its last session before it exits (default `60`) |
| `RUBY_LSP_PLUGIN_BACKEND` | Replace the backend command (the tests use a fake LSP server) |

The hub reads these once, from the session that started it. A change takes effect when the hub restarts, after every
session in that checkout has closed. `RUBY_LSP_PLUGIN_HUB_CONTAINER` is internal: the hub sets it to name the backend
container.

## Reek hook

- **Trigger.** Runs after `Write`, `Edit` or `MultiEdit` on `.rb` and `.rake` files.
- **Skipped paths.** `db/schema.rb`, `db/migrate/`, `spec/`, `test/`, `vendor/` and `node_modules/`.
- **Output.** Whole-file results, capped at 10 smells plus `(+N more)`. When there are no smells it prints nothing, so it costs zero tokens.
- **Advisory only.** It always exits 0. A timeout (60 s), a missing gem or bad output logs one stderr line and never blocks the edit.
- **Warm container.** When the hub's `ruby-lsp-<key>` container is running, Reek runs inside it with `docker exec`
  instead of starting a new container per edit.
- **Config.** The project's `.reek.yml` wins. Without one, the hook uses the bundled Rails-tuned `config/.reek.yml`:
  - `IrresponsibleModule` is off.
  - `InstanceVariableAssumption` is off for controllers and mailers.
  - `UtilityFunction` is off for helpers and jobs.
  - Migrations are muted.
  - `TooManyStatements` allows at most 10.

Example context Claude receives:

```text
[ruby-lsp] Reek (advisory) found 2 smell(s) in app/models/order.rb. Fix the ones in code you just wrote; leave pre-existing ones unless asked.
- app/models/order.rb:12 FeatureEnvy: Order#total refers to 'item' more than self (maybe move it to another class?)
- app/models/order.rb:20 TooManyStatements: Order#import has approx 14 statements
```

## Tests

```bash
python3 -m unittest discover -s plugin-ruby-lsp/tests
```

The wrapper tests put stub `docker`, `bundle` and `reek` binaries first on `PATH`. The hub and bridge tests run real hub
processes against `tests/fake_lsp_server.py` through `RUBY_LSP_PLUGIN_BACKEND`. No real daemon or Ruby is needed.

## Known limitations

- **Docker needs the gems in `Gemfile.lock`.** A container can't `bundle exec` a gem the bundle doesn't have. Without them, the wrapper falls back to the host.
- **Slow first start.** On first launch, ruby-lsp builds `.ruby-lsp/` and may run `bundle install`. Inside Docker that takes longer, and the files may be owned by the container user.
- **Unverified config fields.** `.lsp.json` uses `transport` and `maxRestarts`, copied from a working installed plugin rather than the docs. Run `claude --debug` to confirm the server starts.
- **Cold start after an idle stop.** The first Ruby write after an idle stop boots the container again (about 10 to 30 s
  on a Rails app), so its diagnostics can land a turn late. Raise `RUBY_LSP_PLUGIN_IDLE_MINUTES` if that happens too
  often.
- **Last writer wins.** Two sessions editing the same file share one backend copy of it. Each session gets diagnostics
  only for the text it last sent, so session B stays quiet about session A's edits until B touches the file again.
- **Hub logs.** The shared hub has no terminal. Its log, and the backend's stderr, go to
  `~/.claude/.ruby-lsp-plugin/hubs/<key>/hub.log`.

## Changelog

### 0.2.0

One shared ruby-lsp per checkout that runs only while Ruby is being written, modeled on mempalace-docker 2.0.0's shared
hub.

- **Fix: RuboCop diagnostics now reach Claude.** ruby-lsp 0.26 serves diagnostics only on request
  (`textDocument/diagnostic`), and Claude Code 2.1.292 never sends that request, so 0.1.0 delivered none. The hub now
  pulls after every sync and pushes the result as `publishDiagnostics`. A report for text that changed since is
  dropped, and the hub advertises `refreshSupport`, so a `.rubocop.yml` change re-pulls every open document.

- `scripts/lsp_bridge.py` is the new per-session LSP process. `run-ruby-tool.sh lsp` now execs it. `.lsp.json` is
  unchanged.
- `scripts/lsp_hub.py` is a per-checkout, detached, flock-singleton hub. It answers `initialize` from a capabilities
  cache, starts the backend on the first Ruby write or LSP request, rewrites request ids per client, routes diagnostics,
  and answers server-to-client requests itself.
- The backend stops after `RUBY_LSP_PLUGIN_IDLE_MINUTES` (default 15) without client traffic, and the hub exits 60 s
  after its last session.
- Claude Code sends each `didChange` as the whole text with no range. The hub forwards it as one ranged edit that
  replaces the old text, since ruby-lsp applies ranged edits only.
- A backend crash fails in-flight requests with `RequestFailed` (`-32803`). Three crashes in 5 min keep the backend down
  until a new session connects.
- New `run-ruby-tool.sh lsp-backend` mode runs ruby-lsp itself in the named container `ruby-lsp-<key>`. The Reek hook
  `docker exec`s into that container when it's up.
- New env vars: `RUBY_LSP_PLUGIN_IDLE_MINUTES`, `RUBY_LSP_PLUGIN_SHARED`, `RUBY_LSP_PLUGIN_HUB_LINGER`,
  `RUBY_LSP_PLUGIN_BACKEND`.
- `scripts/lsp_jsonrpc.py` holds the shared framing and UTF-16 position math. New tests cover the hub, the bridge and
  the framing against a fake LSP server.

### 0.1.0

Initial release.

- `.lsp.json` registers ruby-lsp for `.rb`, `.rake`, `.gemspec`, `.ru` and `.erb`, launched through the wrapper.
- `scripts/run-ruby-tool.sh` picks Docker first, then host `bundle exec`, then the global binary. It mounts the project at its identical path.
- `hooks/reek_hook.py` is an advisory PostToolUse Reek hook. It caps output at 10 smells, stays silent on clean files and fails open.
- `config/.reek.yml` is a Rails-tuned fallback config.
- `skills/ruby-lsp` holds the pre-write checklist and the rules for handling diagnostics.
