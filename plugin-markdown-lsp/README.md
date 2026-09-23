# markdown-lsp plugin

Markdown diagnostics for Claude Code. It gives Claude two things:

- **rumdl diagnostics.** [rumdl](https://github.com/rvben/rumdl) runs as a language server. It pushes
  markdownlint-compatible offenses back after every `.md`, `.markdown` or `.mdx` edit.
- **A pre-write skill.** A short checklist, so Claude writes lint-clean Markdown on the first pass.

Why it exists: the `skills-md:markdown` skill describes good Markdown, but nothing checked it at edit time. LLM-written
Markdown (mine included, as Claude) tends to skip heading levels, mix `*` and `-` bullets, and leave code fences without
a language tag. The `markdown-format` plugin fixes what `markdownlint-cli2 --fix` can fix, then stays silent. That
silence was the gap. Claude never heard about the offenses that can't be auto-fixed, so it kept repeating them.

## Install

```bash
/plugin install markdown-lsp@llm-agent-workflow
/reload-plugins
```

Docker is the primary runtime, so nothing else is needed while the daemon runs. For machines without Docker, install
`rumdl` on the host as the fallback:

```bash
uv tool install rumdl     # or: brew install rumdl | pip install rumdl | npm install -g rumdl
```

## How it runs: Docker first, native binary fallback

`.lsp.json` launches `scripts/run-rumdl.sh server`. The wrapper picks the first runtime that works:

1. **Docker.** Used when `docker info` answers. The image is `ghcr.io/rvben/rumdl:latest`, a small static image.
   The command is:

   ```bash
   docker run --rm -i --user "$(id -u):$(id -g)" -v "$PWD:$PWD" -w "$PWD" ghcr.io/rvben/rumdl:latest server
   ```

1. **Native binary.** `rumdl` on `PATH`.
1. **`uvx rumdl`**, then **`npx --yes rumdl`**.
1. **Nothing found.** Exit 127 with install hints on stderr.

This is the same order as the ruby-lsp plugin. One runtime, the image, behaves the same on every machine, and the host
binary only matters when Docker is down. The cost is container start-up on every LSP launch, usually under a second
once the image is pulled.

How it behaves:

- **Identical-path mount.** The project and the plugin root are mounted at their host paths. LSP file URIs and the
  bundled config path stay valid on both sides. When the plugin root is inside the project, only the project is
  mounted. A nested read-only mount would make that subtree unwritable for `rumdl fmt`.
- **Logging.** Every fallback reason goes to stderr as a `[markdown-lsp] ...` line. stdout carries JSON-RPC, so nothing
  else is written there.

| Env var | Effect |
| ------- | ------ |
| `MARKDOWN_LSP_PLUGIN_CONFIG` | Explicit rumdl config path. Always wins |
| `MARKDOWN_LSP_PLUGIN_IMAGE` | Docker image, for example to pin `ghcr.io/rvben/rumdl:0.2.76` |
| `MARKDOWN_LSP_PLUGIN_FORCE_HOST=1` | Skip Docker entirely |
| `MARKDOWN_LSP_PLUGIN_FORCE_DOCKER=1` | Exit 1 instead of falling back to the native binary |

## Config: project first, bundled fallback

The wrapper searches upward from the working directory to the `.git` boundary, the same way rumdl does. It looks for:

- `.rumdl.toml`, `rumdl.toml` or `.config/rumdl.toml`
- `pyproject.toml` with a `[tool.rumdl]` section
- `.markdownlint.json`, `.jsonc`, `.yaml` or `.yml`, or `markdownlint.json` / `markdownlint.yaml`

When it finds one, it passes no `--config`, and rumdl uses the project's config. Otherwise it passes the bundled
`config/rumdl.toml`, which mirrors the `skills-md:markdown` standards:

| Standard | Rule |
| -------- | ---- |
| One H1, no skipped levels, ATX only | MD025, MD001, MD003 `atx` |
| `-` bullets, `1.` ordered, 2-space nesting | MD004 `dash`, MD029 `one`, MD007 |
| Fenced code with backticks and a language tag | MD046 `fenced`, MD048 `backtick`, MD040 |
| 120-character prose, code and tables exempt | MD013 |
| Blank lines around blocks, never two in a row | MD022, MD031, MD032, MD058, MD012 |
| Table column counts match | MD056 |
| No bare URLs, descriptive link text, image alt text | MD034, MD059, MD045 |
| `_italic_`, `**bold**` | MD049 `underscore`, MD050 `asterisk` |

MD033 (inline HTML) and MD041 (first line must be an H1) are off, and MD024 checks siblings only. These match the
`markdown-format` plugin's `.markdownlint.json`.

## Using it with markdown-format

The two plugins do different jobs, so install both:

- **markdown-format** runs `markdownlint-cli2 --fix` after each write. It fixes silently and never reports.
- **markdown-lsp** reports what is left, so Claude can fix the rest by hand.

### Out-of-date diagnostics after a formatter fix

Claude Code sends Claude's edit to the LSP **before** the `markdown-format` PostToolUse hook rewrites the file. So an
offense the formatter fixes can still come back once as a diagnostic, even though the file on disk is already correct.

Seen in testing: Claude wrote a `* item` bullet. `markdown-format` changed it to `- item`, but rumdl still reported
`[MD004] List marker '*' does not match expected style '-'` for the pre-hook content.

What to do: before acting on MD004, MD040, MD009 or another auto-fixable rule, check the current line. If it is already
fixed, ignore the diagnostic. The next edit refreshes it. Offenses the formatter can't fix, such as MD059 link text or
MD013 line length, are always current.

### Line length

`markdown-format` disables MD013, so it never wraps long lines. The LSP still flags lines over 120
characters. That is on purpose: line wrapping needs judgment, and a reflow tool should not do it blind.

## Tests

```bash
python3 -m unittest discover -s plugin-markdown-lsp/tests
```

The wrapper tests put stub `rumdl`, `docker`, `uvx` and `npx` binaries on an isolated `PATH`. No real rumdl or Docker
daemon is needed.

## Known limitations

- **`.rumdl_cache/` in the project.** rumdl caches lint results in the working directory. Add `.rumdl_cache/` to
  `.gitignore`.
- **Slow first Docker start.** The first launch pulls the image. Until the pull finishes, no diagnostics arrive.
- **`--config` turns off per-directory configs.** With the bundled fallback, rumdl ignores nested `.rumdl.toml` files.
  That only matters in a repo with no root config, which is when the fallback applies anyway.
- **Unverified config fields.** `.lsp.json` uses `transport` and `maxRestarts`, the same as the ruby-lsp plugin. Run
  `claude --debug` to confirm the server starts.

## Changelog

### 0.2.0

- Runtime order is now **Docker first**, then native `rumdl`, then `uvx`, then `npx`. This matches ruby-lsp.
- `MARKDOWN_LSP_PLUGIN_FORCE_DOCKER=1` now only blocks the fallback. Docker is already tried first.
- Documented the out-of-date diagnostics that appear when `markdown-format` fixes an offense after the LSP has seen
  the edit.

### 0.1.0

Initial release.

- `.lsp.json` registers rumdl for `.md`, `.markdown` and `.mdx`, launched through the wrapper.
- `scripts/run-rumdl.sh` tried host rumdl first, then Docker, then `uvx`, then `npx`. The project config wins over the
  bundled config.
- `config/rumdl.toml` is a fallback config derived from the `skills-md:markdown` standards.
- `skills/markdown-lsp` holds the pre-write checklist and the rules for handling diagnostics.
