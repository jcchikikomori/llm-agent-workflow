---
name: markdown-lsp
description: Pre-write checklist and diagnostic-handling rules for Markdown when the markdown-lsp plugin is active. Use before writing or editing any .md, .markdown or .mdx file, and whenever rumdl LSP diagnostics (MD001, MD004, MD013, MD040 and similar) appear after an edit.
---

# markdown-lsp — write it right the first time

The markdown-lsp plugin runs [rumdl](https://github.com/rvben/rumdl) as a language server. After every Markdown edit,
rumdl pushes markdownlint-compatible diagnostics back to you.

Each diagnostic costs one more edit round-trip. The checklist below avoids most of them.

## Pre-write checklist

1. One H1 per file, and no skipped levels (H2, then H3, then H4). Use ATX headings (`#`) only.
1. Bullets use `-`. Every ordered item is numbered `1.`. Nested lists indent 2 spaces.
1. Every code block is fenced with backticks and has a language tag. Use `text` for plain output and `console` for
   terminal sessions.
1. Put a blank line before and after every heading, list, code block and table. Never two blank lines in a row.
1. Prose lines stay at 120 characters or fewer. Code blocks and tables are exempt.
1. Tables have the same column count in every row. Pad cells with a space: `| value |`.
1. No bare URLs in prose. Wrap them as `[descriptive text](url)` or `<url>`.
1. Link text describes the target. "here", "click here", "link", "more" and "this" are flagged.
1. Images always have alt text.
1. Use `_italic_` and `**bold**`.

When the repo has its own `.rumdl.toml`, `.markdownlint.*` or `[tool.rumdl]` config, that config wins over this list.

## Acting on diagnostics

- **Fix diagnostics in the same turn** as the edit that caused them. Fix only the lines you touched. Don't reflow a
  whole legacy file.
- **Don't run `rumdl check` by hand** when the diagnostics are already in context. That spends tokens on output you
  already have.
- **Don't add `<!-- rumdl-disable -->` or `<!-- markdownlint-disable -->` comments** on your own. If a rule is plainly
  wrong for the file, tell the user and ask first.
- **MD013 (line length):** break the sentence at a natural clause boundary. Never split a URL or inline code span.
- **MD057 (missing relative link target):** check the path first. Fix the link, or tell the user the target file does
  not exist yet.

## Out-of-date diagnostics after a formatter fix

The LSP sees your edit **before** the `markdown-format` hook rewrites the file. When that plugin is installed, an
auto-fixable offense (MD004 bullet style, MD040 fence language, MD009 trailing spaces and similar) can still come back
once, after the file on disk is already fixed.

- Before fixing an auto-fixable rule, re-read the flagged line. If it is already correct, skip it and don't mention it.
- Offenses the formatter can't fix (MD059 link text, MD013 line length, MD001 heading jumps) are always current. Fix
  them.

## Standards no lint rule can check

rumdl checks structure, not meaning. For prose quality, follow the `skills-md:markdown` skill:

- descriptive link text beyond the flagged words
- relative links for files in this repo and absolute links for external ones
- `**bold**` for critical terms, `_italic_` for first use, and backticks for paths, flags and env vars

## When nothing comes back

No diagnostics can mean either of these:

- the file is clean, or
- the server is not running. The usual causes are a first `docker pull` still in progress, or a Docker daemon that is
  down with no `rumdl` on the host.

If you expected an offense and got none, say so. Point the user at `claude --debug` and the `[markdown-lsp]` stderr
lines instead of assuming the file is clean.

## Install hints

Docker runs rumdl first. Any one of these puts `rumdl` on the host as the fallback for when Docker is down:

```bash
brew install rumdl
uv tool install rumdl
pip install rumdl
npm install -g rumdl
cargo install rumdl
```
