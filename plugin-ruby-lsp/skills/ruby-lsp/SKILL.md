---
name: ruby-lsp
description: Pre-write checklist and diagnostic-handling rules for Ruby and Rails code when the ruby-lsp plugin is active. Use before writing or editing any .rb, .rake, .ru, .gemspec or .erb file, and whenever RuboCop LSP diagnostics or "[ruby-lsp] Reek (advisory)" context appear after an edit.
---

# ruby-lsp — write it right the first time

The ruby-lsp plugin checks every Ruby edit twice:

- **RuboCop diagnostics.** ruby-lsp runs RuboCop with the project's own `.rubocop.yml` and pushes the offenses back to you after the edit.
- **Reek smells.** A PostToolUse hook adds a `[ruby-lsp] Reek (advisory)` block when the edited file has smells.

Every offense costs one more edit round-trip. Following the checklist below before writing avoids most of them.

## Pre-write checklist

1. `# frozen_string_literal: true` goes on line 1 of every Ruby file. Migrations, patches and specs are the exception.
2. Use single quotes. Double quotes only for interpolation or escapes.
3. Use guard clauses (`return unless ...`) instead of nested `if`.
4. Every `case` has an `else` that raises or handles the default explicitly.
5. Keep methods short, 10 statements or fewer. Split them before they grow.
6. Avoid **Feature Envy**. If a method mostly calls `other.x`, `other.y`, move it onto `other`.
7. Controllers stay thin. Business logic goes in `app/services/`, and shared behavior goes in concerns.
8. Avoid N+1 queries. Use `includes`/`preload` whenever a loop touches an association.
9. Use Strong Parameters and never mass-assign `params` directly.
10. No `html_safe` or `raw` on user input, and never interpolate user input into SQL.
11. Use stabby lambdas (`->`) and `SecureRandom` for tokens.

When the repo's own `.rubocop.yml` or `.reek.yml` disagrees with this list, the repo wins.

## Acting on diagnostics

- **Fix RuboCop diagnostics in the same turn** as the edit that caused them. Fix only the lines you touched. Don't reformat the whole legacy file.
- **Don't run `rubocop` or `reek` by hand** to "double-check" when the diagnostics are already present. That burns tokens on output you already have.
- **Reek is advisory.** Fix smells in code you just wrote. Leave pre-existing smells alone unless the user asked for a refactor. If a smell is intentional, say so in one line and move on.
- If a diagnostic is plainly wrong for the project, ask the user before adding `# rubocop:disable` or a Reek `:reek:` comment. Don't silence it on your own.

## When nothing comes back

No diagnostics can mean either of these:

- the code is clean, or
- the server is not running. The usual causes are a missing gem, a Docker daemon that is down, or the first `.ruby-lsp/` bundle build still in progress.

If you expected an offense and got none, say so. Point the user at `claude --debug` and the `[ruby-lsp]` stderr lines instead of assuming the code is clean.

## Conflicts

Install only one Ruby LSP plugin. Both `ruby-lsp@llm-agent-workflow` and the official `ruby-lsp@claude-plugins-official` register `.rb`.
