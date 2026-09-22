# commit-guard

A Claude Code plugin that hands every git command writing a commit message to
**you**, to run in your own terminal, then watches for the result and lets Claude
pick the thread back up. GPG signing and the pinentry passphrase flow are fully
preserved.

## What it does

- Intercepts commit-message-writing git commands via a `PreToolUse` hook
- Blocks the tool call and tells Claude to:
  1. show you the staged files, the message, and the exact command
  2. hand it over for you to run
  3. arm a bounded background watcher
- The watcher polls the repo and emits exactly one verdict: `DONE`, `ABORTED`,
  `TIMEOUT`, or `ERROR`
- On `DONE`, Claude must verify with `git show --format='%H %G? %an %s'` before
  claiming success — `DONE` means *something* committed, not that your command ran

## Why delegate instead of just asking?

The agent shell genuinely cannot do this work:

- `GIT_EDITOR=true` is exported into the Bash tool, so a bare `git commit` never
  opens an editor — it silently accepts the template
- `tty` reports "not a tty", so GPG pinentry cannot prompt

On a repo with `commit.gpgsign=true`, no signed commit can be produced from that
shell at all. Your terminal can.

A consequence worth knowing: **the handed-over command behaves differently in
your terminal than it would have in Claude's.** `git commit --amend` is a silent
no-op-message amend for the agent and a real editor session for you. The hook's
handoff text says this explicitly.

## Scope

Intercepted:

| Command | Except |
| --- | --- |
| `git commit` (`-m`, `-F`, `-C`, `-c`, `--amend`, `--no-edit`, bare, …) | `--dry-run`, `--short`, `--porcelain`, `--long` |
| `git merge <ref>`, `merge --continue` | `--abort`, `--quit`, `--ff-only`, `--squash`, `--no-commit`/`-n` |
| `git cherry-pick <ref>`, `--continue`, `--skip` | `--abort`, `--quit`, `-n` |
| `git revert <ref>`, `--continue`, `--skip` | `--abort`, `--quit`, `-n` |
| `git rebase`, `--continue`, `--skip` | `--abort`, `--quit`, `--edit-todo`, `--show-current-patch` |
| `git am`, `--continue`, `--skip` | `--abort`, `--quit`, `--show-current-patch` |
| `git tag -a\|-s\|-u` | lightweight `git tag <name>`, `-d`, `-l`, `-v` |
| `git pull --rebase`, `git pull --no-ff` | plain `git pull`, `--ff-only` |

Three exclusions matter more than they look:

- **`--squash` and `--no-commit`/`-n` create no commit and never write
  `MERGE_HEAD`**, so intercepting them would arm a watcher whose predicates can
  never be satisfied — a guaranteed 30-minute false timeout. The follow-up
  `git commit` is the correct gate.
- **`--skip` is a continuation, not a recovery.** `git-cherry-pick(1)`: "Skip the
  current commit and continue with the rest of the sequence." It creates commits.
  Only `--abort` and `--quit` are recoveries, and those stay allowed so Claude can
  unwedge a repo without a handoff round-trip.
- **`rebase --edit-todo`** leaves the rebase in progress, so "no rebase dirs" is
  false by construction.

## Modes

| `COMMIT_GUARD_MODE` | Behavior |
| --- | --- |
| `delegate` | Always hand off, even if the session looks unattended |
| `deny` | Block with an explanation, arm no watcher |
| `off` | Pass through — for CI bots that legitimately commit |
| unset | Attended ⇒ `delegate`; unattended ⇒ `deny` |

In an unattended run (`claude -p`, CI, a subagent) there is nobody to hand the
command to, so a delegate payload would guarantee a 30-minute stall or a retry
loop. The default is therefore `deny`. Attendance is inferred from
`CLAUDE_CODE_SESSION_ATTENDED`, which is **undocumented** — `COMMIT_GUARD_MODE`
is the supported control.

`COMMIT_GUARD_SCHEDULE` overrides the watcher backoff (default
`5 10 15 30 60 120 240 360 480 480` — ten polls, checks at t=5…1800s, a hard
30-minute ceiling). It exists mainly to make the timeout branch testable.

## Install

```bash
/plugin install commit-guard@llm-agent-workflow
/reload-plugins
```

## How it works

| Component | Path | Role |
| --- | --- | --- |
| Hook | `hooks/commit_guard_hook.py` | Classifies the command, snapshots the repo, emits the handoff |
| Watcher | `hooks/await_commit.sh` | Bounded 10-poll watch; one verdict line, then exits |
| Hook config | `hooks/hooks.json` | Registers `PreToolUse` on the `Bash` matcher |
| Skill | `skills/commit-guard/SKILL.md` | Tells Claude how to hand over, watch, and verify |
| OpenCode port | `plugins/opencode-commit-guard.ts` | Shells out to the same Python classifier |
| Token file | `~/.claude/.commit-guard-token` | Explicit override; single-use |

The hook snapshots `HEAD`, the in-progress markers, `rebase-merge/orig-head` and
`sequencer/head` **at block time** and passes them to the watcher as arguments.
That removes a race — if you commit between the block and the watcher arming, the
watcher still compares against the pre-block sha and reports `DONE` correctly.

Two of those snapshots are not optional polish. Without `orig-head`, a finished
rebase and an aborted one are indistinguishable. Without `sequencer/head`, a
multi-commit `cherry-pick --abort` rewinds to *behind* your starting HEAD, which
reads as a successful commit.

## Watcher verdicts

| Line | Exit | Meaning |
| --- | --- | --- |
| `commit-guard: DONE <op> <sha>` | 0 | Completed — verify, then continue |
| `commit-guard: ABORTED <op> <evidence>` | 3 | Inferred abort — evidence token included |
| `commit-guard: TIMEOUT <op> <reason>` | 4 | Ten polls exhausted |
| `commit-guard: ERROR <op> <reason>` | 64/65/66 | usage / env / signal — **no verdict reached** |

`TIMEOUT` reasons are deliberately ambiguous, because the cases are
indistinguishable from outside the repo: `no-activity` covers "never ran",
"aborted before the first poll", "a rebase already up to date", "an `--amend` that
reproduced the identical SHA", and "ran in a different worktree". The plugin
reports the ambiguity rather than guessing.

## Override

A one-time token lets a single command through, for sessions where you genuinely
have no terminal and say so. The hook deliberately **does not print the minting
command to Claude** — it lives only in `SKILL.md`, gated on an explicit
instruction from you. The token is `sha256(command + "\0" + absolute_git_dir)`:
bound to both the exact command and the repo, and consumed on first match.

## Known limits

1. **This hook is advisory, not enforcement.** `git commit-tree`,
   `git update-ref`, `git fast-import`, or writing a shell script and running it
   all evade any Bash-command parser. Real enforcement would need a repo-level
   `pre-commit`/`prepare-commit-msg` hook that refuses when `CLAUDECODE=1` unless
   a human-minted token is present.
2. **Plain `git pull` can still create a merge commit** when it is not a
   fast-forward and merges cleanly. Intercepting every `git pull` would wreck
   routine work, so only `--rebase` and `--no-ff` are gated. The conflicted case
   is still caught, because the follow-up `git commit` / `merge --continue` is.
3. **The watcher only observes repo state.** A `DONE` means something committed in
   that git dir, hence the mandatory `%G?` verification. A commit made in a
   different worktree or clone is invisible and surfaces as `TIMEOUT no-activity`.
4. **The watcher's value is front-loaded.** Its verdict reaches Claude on the next
   invocation, and realistically you will say "done" before that. The first two
   polls plus the persisted result file carry most of the benefit; the 30-minute
   tail is mostly an audit trail.
5. **Chains hand over more than the commit.** `git commit -m x && git push`
   delegates the push too. Intended, but the handoff text says so and the watcher
   predicate covers only the commit.

## Changelog

### 1.1.0

Delegation replaces the approval-and-retry flow. Claude no longer asks to run the
commit — it hands the command to you and watches for the result.

- **New `hooks/await_commit.sh`** — bounded watcher, ten polls with backoff
  `5 10 15 30 60 120 240 360 480 480` (hard 30-minute ceiling), one verdict line
  per run. Handles worktrees via `rev-parse --absolute-git-dir`, unborn HEAD,
  rebase `orig-head`, and multi-commit sequencer rollback.
- **Scope widened** beyond `git commit` to merge, cherry-pick, revert, rebase,
  `am`, annotated/signed tags, and `git pull --rebase`/`--no-ff`.
- **Regex replaced with argv tokenization.** The old `\bgit\s+commit\b` never
  matched `git -c user.name=x commit`, and the quote-stripping pre-pass made
  `bash -c 'git commit -m x'` invisible. The hook now strips heredoc bodies,
  tokenizes with `shlex`, splits command chains, unwraps `env`/`sudo`/`nohup`,
  recurses into nested shells, skips git's global options, and resolves one level
  of alias.
- **Fixed three fail-open paths.** `BLOCKED_MESSAGE.format()` raised `KeyError` on
  a message containing braces (`git commit -m "fix {foo}"`) → traceback → exit 1,
  which the hooks spec treats as *non-blocking*, so the commit ran unguarded. All
  internal errors now block, unbalanced quotes fail closed, and every git
  subprocess has a 2s timeout so an `index.lock` stall cannot burn the hook budget.
- **Fixed the OpenCode token bypass.** The port accepted *any* non-empty token
  file with no hash comparison, so one stale token unlocked the next commit,
  whatever it was. It now shells out to the same Python classifier, so the two
  ports cannot drift.
- **Token hardened and demoted.** Bound to `sha256(command + "\0" + git_dir)` so a
  token minted for one repo cannot be spent in another, and the minting command is
  no longer printed to Claude in the block message.
- Added `COMMIT_GUARD_MODE` (`delegate`/`deny`/`off`, defaulting to `deny` when
  unattended) and `COMMIT_GUARD_SCHEDULE`.
- Added a delegation ledger under `~/.claude/.commit-guard/` so a retry does not
  arm a second watcher.

### 1.0.0

Major release for repository rename to `llm-agent-workflow`.

- Updated install target to `commit-guard@llm-agent-workflow`.
- No behavior changes to the approval gate or token flow.

### 0.2.0

Added OpenCode TypeScript port with SHA256 approval flow (`plugins/opencode-commit-guard.ts`) and `/commit-guard` slash command (`commands/commit-guard.md`). Token file path moved to `~/.config/opencode/.commit-guard-token` on OpenCode. Added OpenCode npm package manifest.

### 0.1.1

Fix: strip quoted strings before `git commit` detection — prevents the approval token-write command from falsely triggering the hook.

### 0.1.0

Initial release. Per-commit approval flow with one-time SHA256 token. GPG signing preserved.
