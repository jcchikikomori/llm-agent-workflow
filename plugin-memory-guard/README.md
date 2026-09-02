# memory-guard

A Claude Code plugin that watches `.claude/**`, the project's root
`CLAUDE.md`, and `docs/ticket-tracking/**` for changes. When one of those
paths changes — whether Claude just edited it, or it was already dirty when
a session starts — it has Claude save the substance of the change to memory,
then resolves the file per a **remove-or-stash preference that's asked once,
ever, per project**.

## What it does

- **`SessionStart`** hook: checks `git status` for watched paths that are
  already dirty when a session begins (left over from a previous session,
  or edited outside Claude).
- **`PostToolUse`** hook (matcher `Write|Edit|MultiEdit`): fires live,
  immediately after Claude writes/edits a watched path during the session.
- Either way, Claude is instructed to:
  1. Judge whether the change is memory-worthy (skips pure formatting/typo
     noise).
  2. Save it — via the `mempalace` MCP tools if they're available in the
     session, otherwise the existing file-based auto-memory system.
  3. **First time ever for this project**: ask the user once, via
     `AskUserQuestion`, whether watched changes should be **Removed**
     (deleted from disk — the content is already in memory) or **Stashed**
     (`git stash push -u --`, scoped only to the flagged paths). The answer
     is persisted per-project and reused automatically forever after —
     **every later flagged change applies it directly, with no further
     asking.**
- A per-session state file debounces re-prompts within a session; a
  separate per-project preference file (outside the repo, so writing it
  never itself triggers a watched-path flag) is what makes the remove/stash
  choice one-time rather than per-session.

## Watch scope

Configurable in `config/watched-paths.json`:

```json
{
  "watched_dirs": [".claude", "docs/ticket-tracking"],
  "watched_files": ["CLAUDE.md"]
}
```

`watched_files` matches only at the repo root (a nested `packages/foo/CLAUDE.md`
won't match); `watched_dirs` matches recursively, the same way a bare
directory pathspec does in `git`.

## Install

```bash
/plugin install memory-guard@llm-agent-workflow
/reload-plugins
```

## Changing your remove/stash preference

The first-ever flagged change in a project asks Remove-or-Stash and
remembers the answer. To be asked again (e.g. you want to switch from
Remove to Stash), clear the stored preference:

```bash
python3 <plugin path>/scripts/reset_preference.py --repo-root /path/to/repo
```

## How it works

| Component | Path | Role |
| ----------- | ------ | ------ |
| Hook | `hooks/session_start_hook.py` | Detects stale dirty watched paths at session start |
| Hook | `hooks/post_tool_use_hook.py` | Detects live watched-path writes/edits |
| Shared module | `hooks/memory_guard_common.py` | Repo-root resolution, path matching, session-state + project-preference I/O |
| Hook config | `hooks/hooks.json` | Registers `SessionStart` (no matcher) + `PostToolUse` (`Write\|Edit\|MultiEdit`) |
| Helper | `scripts/apply_action.py` | The one command that actually performs remove/stash + marks paths resolved |
| Helper | `scripts/set_preference.py` | Claude runs this once, right after the user's first-ever answer |
| Helper | `scripts/reset_preference.py` | Manually clears a project's stored preference |
| Helper | `scripts/mark_resolved.py` | Lower-level; `apply_action.py` calls this internally, not normally invoked directly |
| Config | `config/watched-paths.json` | Editable watch-path list |
| Skill | `skills/memory-guard/SKILL.md` | The save-then-resolve procedure Claude follows |
| State | `~/.claude/.memory-guard/session_<id>.json` | Per-session flagged/resolved path tracking |
| State | `~/.claude/.memory-guard/project-prefs/<hash>.json` | Per-project remove/stash preference, persists across sessions |

## Known limitations

- No filesystem watcher: a file edited in another editor mid-session, with
  Claude making no tool calls, is only caught at the *next* `SessionStart`,
  not live.
- Gitignored watched paths (e.g. a gitignored `.claude/settings.local.json`)
  are invisible to `git status`, so stale-change detection has a blind spot
  there.
- If the project isn't inside a git repo, the stash-offering path no-ops;
  the memory-save side can still run. Remove still works either way.
- Removal is a permanent `rm`. The memory save is instructed to happen
  first, but it's Claude following that instruction, not an atomic
  guarantee — if the memory save silently fails and Claude proceeds to
  delete anyway, the content is gone.

## Changelog

### 1.0.0

Major release for repository rename to `llm-agent-workflow`.

- Updated install target to `memory-guard@llm-agent-workflow`.
- No behavior changes to watch/snapshot/remove-or-stash flow.

### 0.3.0

Added OpenCode TypeScript ports using `experimental.chat.system.transform` (SessionStart) and `event(file.edited)` (PostToolUse). Python scripts now detect `~/.config/opencode/` vs `~/.claude/` and write state to whichever exists. Added `/memory-guard` slash command (`commands/memory-guard.md`) and OpenCode npm package manifest.

### 0.2.1

Fixed a real bug found via testing: `git status --porcelain`'s default
untracked-file mode collapses an entirely-untracked directory into one `??
<dir>/` line instead of listing files inside it — so a brand-new
subdirectory under a watched path (e.g. a first-ever
`.claude/agent-memory/<agent>/`) would resolve to a directory path that
can't be `rm`'d or stashed as a file, silently skipped, and left the
individually-flagged file stuck `pending` forever. This is what actually
happened in a real session: paths got recorded `action: "stash"` while
`git status` kept showing them modified afterward. Fixed by scanning with
`--untracked-files=all` (safe here since the scan is always scoped to the
small watched pathspecs, never the whole repo).

Also replaced the two-step "hand-write `git stash`/`rm`, then call
`mark_resolved.py`" flow with a single `scripts/apply_action.py` that does
both atomically — removing the split that let a resolution get recorded as
done without the underlying command ever running.

### 0.2.0

Replaced the per-change Keep/Stash question with a one-time, per-project
Remove/Stash preference: the first flagged change in a project asks once
and persists the answer (`~/.claude/.memory-guard/project-prefs/`); every
later flagged change in that project applies it automatically with no
further `AskUserQuestion`. Added `scripts/set_preference.py` and
`scripts/reset_preference.py`. Dropped "Keep" as an option entirely.

### 0.1.1

Fix: the injected instruction text and SKILL.md now explicitly state the
keep/stash `AskUserQuestion` is mandatory even under an autonomous/"Auto
Mode" no-stopping-to-ask session bias — reported case where the prompt was
silently skipped and the change was left as-is with no user input.

### 0.1.0

Initial release. `SessionStart` + `PostToolUse` detection, per-session
debounce, mempalace-or-fallback memory save, scoped keep/stash prompt.
