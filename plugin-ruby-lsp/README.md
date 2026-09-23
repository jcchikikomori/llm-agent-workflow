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

## How it runs: Docker first, host fallback

Both the LSP server and the Reek hook go through `scripts/run-ruby-tool.sh`. It picks the first option that works:

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

## Reek hook

- **Trigger.** Runs after `Write`, `Edit` or `MultiEdit` on `.rb` and `.rake` files.
- **Skipped paths.** `db/schema.rb`, `db/migrate/`, `spec/`, `test/`, `vendor/` and `node_modules/`.
- **Output.** Whole-file results, capped at 10 smells plus `(+N more)`. When there are no smells it prints nothing, so it costs zero tokens.
- **Advisory only.** It always exits 0. A timeout (60 s), a missing gem or bad output logs one stderr line and never blocks the edit.
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

The wrapper tests put stub `docker`, `bundle` and `reek` binaries first on `PATH`. No real daemon or Ruby is needed.

## Known limitations

- **Docker needs the gems in `Gemfile.lock`.** A container can't `bundle exec` a gem the bundle doesn't have. Without them, the wrapper falls back to the host.
- **Slow first start.** On first launch, ruby-lsp builds `.ruby-lsp/` and may run `bundle install`. Inside Docker that takes longer, and the files may be owned by the container user.
- **Unverified config fields.** `.lsp.json` uses `transport` and `maxRestarts`, copied from a working installed plugin rather than the docs. Run `claude --debug` to confirm the server starts.

## Changelog

### 0.1.0

Initial release.

- `.lsp.json` registers ruby-lsp for `.rb`, `.rake`, `.gemspec`, `.ru` and `.erb`, launched through the wrapper.
- `scripts/run-ruby-tool.sh` picks Docker first, then host `bundle exec`, then the global binary. It mounts the project at its identical path.
- `hooks/reek_hook.py` is an advisory PostToolUse Reek hook. It caps output at 10 smells, stays silent on clean files and fails open.
- `config/.reek.yml` is a Rails-tuned fallback config.
- `skills/ruby-lsp` holds the pre-write checklist and the rules for handling diagnostics.
