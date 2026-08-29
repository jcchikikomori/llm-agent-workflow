# gh-issue-to-pr

A Claude Code plugin that ships a single agent driving one GitHub issue from "reported" to
"merged and closed" — in small, reversible steps, stopping hard at anything shared-state or
hard-to-reverse.

## What it does

The `gh-issue-to-pr` agent runs the full lifecycle:

1. **Investigate** — fetch the issue, read the code paths it touches, form a root-cause
   hypothesis (not just the reporter's proposed fix)
2. **Plan** — root cause, fix, files to touch, risk/blast-radius; asks before committing to a
   path with a real tradeoff
3. **Branch** — detects the repo's branch model and base branch (git-flow aware), creates
   `feature/`/`bugfix/`/`hotfix/<issue#>-<slug>`
4. **Scout** — searches for existing utilities/patterns to reuse before writing new code
5. **Implement** — matches surrounding style, no unrelated refactors or scope creep
6. **Test locally** — whatever the repo defines (test suite, linter, Docker Compose)
7. **Stage** — adds only the changed files, reviews the staged diff for anything that
   shouldn't be there
8. **Draft commit message, then stop** — never runs `git commit`/`git push` itself; the user
   commits and pushes
9. **Open the PR** — once pushed, `gh pr create` with a Summary + Test Plan checklist
10. **Review the PR** — re-reads the diff fresh against correctness/scope/convention
11. **Update the checklist** — ticks off verified Test Plan items
12. **Merge, with confirmation** — checks CI/review threads, never merges on its own initiative
13. **Close the issue, with confirmation** — only if merge didn't already auto-close it

It always reads the target repo's own `CLAUDE.md`/`AGENTS.md`/`CONTRIBUTING.md` first and
defers to those conventions over its own defaults — it's designed to be portable across
repos, not tied to this one.

## Hard rules

- Never runs `git commit`, `git push`, `gh pr merge`, or `gh issue close` without the user
  explicitly telling it to do that specific action, in that turn — an earlier "sounds good"
  on the plan is not standing approval for these.
- Never force-pushes, never `--no-verify`, never skips hooks.
- Asks rather than guesses when it's unclear whether it's resuming an in-flight issue or
  starting fresh.

## Install

```bash
/plugin install gh-issue-to-pr@claude-workflow
/reload-plugins
```

## Usage

Slash command:

```text
/gh-issue-to-pr #42
/gh-issue-to-pr https://github.com/owner/repo/issues/9
```

Or natural language — both route to the same agent:

```text
pick up issue #42
work ticket #17 end to end
implement https://github.com/owner/repo/issues/9
```

Mid-flow resumption works too — e.g. "I've pushed, open the PR" or "checks are green, merge
it." (also via `/gh-issue-to-pr I've pushed, open the PR`).

## Changelog

### 0.3.0

Added `opencode-gh-issue-to-pr.md` subagent (`permission:` replaces the deprecated `tools:` field) and `/gh-issue-to-pr` slash command (`commands/gh-issue-to-pr.md`). Added OpenCode npm package manifest.

### 0.2.0

Added a `/gh-issue-to-pr` slash command (`skills/gh-issue-to-pr/SKILL.md`) as an explicit entry
point — previously the plugin was agent-only with no slash command, so typing `/gh-issue-to-pr`
errored with "Unknown command". The skill routes straight to the existing agent; no change to
the workflow itself.

### 0.1.0

Initial release. Imported from the user's personal `~/.claude/agents/gh-issue-to-pr.md` agent
as a distributable plugin.
