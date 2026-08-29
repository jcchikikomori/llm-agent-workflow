# commit-guard

A Claude Code plugin that requires explicit user approval before every `git commit` command. GPG signing and pinentry passphrase flow are fully preserved.

## What it does

- Intercepts every `git commit` Bash call via a `PreToolUse` hook
- Blocks the commit and instructs Claude to show the user:
  - Staged files (`git diff --cached --stat`)
  - Commit message
  - Exact command about to run
- User answers **yes** or **no**
- On approval: Claude writes a one-time SHA256 token and retries the original command unchanged
- On rejection: Claude aborts

## GPG / signed commits

The hook never modifies the commit command. If your repository requires signed commits (`commit.gpgsign=true`) or you pass `-S`, git invokes gpg-agent after approval. You enter your passphrase through the normal pinentry dialog — nothing changes.

## Install

```bash
/plugin install commit-guard@claude-workflow
/reload-plugins
```

## How it works

| Component | Path | Role |
|-----------|------|------|
| Hook | `hooks/commit_guard_hook.py` | Detects `git commit`, checks token, blocks or allows |
| Hook config | `hooks/hooks.json` | Registers `PreToolUse` on `Bash` matcher |
| Skill | `skills/commit-guard/SKILL.md` | Tells Claude how to ask user, write token, retry |
| Token file | `~/.claude/.commit-guard-token` | SHA256 of approved command; single-use |

## Changelog

### 0.2.0

Added OpenCode TypeScript port with SHA256 approval flow (`plugins/opencode-commit-guard.ts`) and `/commit-guard` slash command (`commands/commit-guard.md`). Token file path moved to `~/.config/opencode/.commit-guard-token` on OpenCode. Added OpenCode npm package manifest.

### 0.1.1

Fix: strip quoted strings before `git commit` detection — prevents the approval token-write command from falsely triggering the hook.

### 0.1.0

Initial release. Per-commit approval flow with one-time SHA256 token. GPG signing preserved.
