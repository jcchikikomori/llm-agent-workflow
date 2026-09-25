---
name: commit-guard
description: Guides Claude when commit-guard intercepts a git command that writes a commit message — show the staged work, hand the exact command to the user to run in their terminal, arm the bounded watcher, then verify what landed before claiming success.
---

# commit-guard

When the commit-guard hook intercepts a git command, it does **not** want you to
ask permission and then run the command yourself. It wants you to hand the
command to the user and wait.

## Why the user runs it, not you

The agent shell cannot do this work:

- `GIT_EDITOR=true` is exported here, so an editor never opens. A bare
  `git commit` or `git rebase -i` silently accepts the template or the todo
  instead of letting anyone edit it.
- `tty` reports "not a tty", so GPG pinentry cannot prompt. On a repo with
  `commit.gpgsign=true` no signed commit can be produced from this shell at all.

So the handed-over command **behaves differently in the user's terminal than it
would have here** — a real editor opens where the agent would have had
`GIT_EDITOR=true`. Say that when you hand it over.

## Delegation flow

1. **Gather context.** Run in parallel:
   - `git diff --cached --stat`
   - `git diff --cached --name-only`

2. **Show the user**, in this order:
   - the staged files
   - the commit message
   - the exact command, verbatim, in a copy-pasteable block

   Say plainly that they run it in their own terminal. If the command is a chain
   (`git commit -m x && git push`), say so — they are running the whole chain,
   push included.

3. **Arm the watcher.** Use the invocation the hook printed, with the `Bash`
   tool and `run_in_background: true`.

   Never run it in the foreground — that blocks the turn for up to 30 minutes.

   If the watcher cannot be armed (sandboxed or `--print` mode may have no
   background shell), that is a **normal outcome**, not an error. Say so and
   wait for the user instead.

4. **Handle the verdict.** The watcher emits exactly one line and exits:

   | Verdict | Exit | What to do |
   | --- | --- | --- |
   | `DONE <op> <sha>` | 0 | Verify (step 5), then continue |
   | `ABORTED <op> <evidence>` | 3 | Report the real state, stop |
   | `TIMEOUT <op> <reason>` | 4 | Report the real state, ask the user |
   | `ERROR <op> <reason>` | 64/65/66 | **No verdict was reached.** Ask the user |

   Any exit other than 0/3/4 means the watcher never reached a conclusion. Never
   infer repo state from a missing verdict.

5. **Verify before claiming success.** `DONE` means *something* committed in
   that git dir — not that your command ran. Before you say it worked:

   ```bash
   git show --stat --format='%H %G? %an %s' <sha>
   ```

   Reconcile it against what you handed over. `%G?` is the signature status: `G`
   good, `U` good-but-untrusted, `N` none. An `N` in a repo with
   `commit.gpgsign=true` means the commit is not signed — say so rather than
   reporting success.

6. **On `ABORTED` / `TIMEOUT` / `ERROR`**, run:

   ```bash
   git status --short --branch
   git log --oneline -3
   ```

   Show the user the real state and stop. Do **not** re-run the delegated
   command, do **not** re-arm the watcher, and do **not** construct a workaround.

## What the TIMEOUT reasons actually mean

They are deliberately ambiguous, because these cases are indistinguishable from
outside the repo. Report the ambiguity; do not guess which one happened.

| Reason | Could be |
| --- | --- |
| `no-activity` | never ran · aborted cleanly before the first poll · a rebase that was already up to date · an `--amend` that reproduced the identical SHA · run in a different worktree or clone |
| `still-in-progress` | still resolving conflicts · walked away · stopped at the next conflict |
| `head-moved-predicate-unmet` | a partially completed sequence · an unrelated `reset`/`checkout` moved HEAD |
| `advanced-but-unfinished` | mid-flight or paused |

`ABORTED` is an inference from evidence, never a certainty — which is why the
evidence token is on the line. Quote it.

## GPG signing rules — never violate these

- **Never** add `--no-gpg-sign` or `-c commit.gpgsign=false`
- **Never** strip `-S` or `--gpg-sign` from a command
- **Never** set `GIT_COMMITTER_SIGNING_KEY` or override GPG config
- Pinentry prompting the user for a passphrase is **expected behavior**, not an
  error to route around

## Never route around the guard

Do not write the command into a shell script and run the script. Do not reach
for `git commit-tree`, `git update-ref`, or other plumbing. The guard is a
PreToolUse hook on `Bash`, so those evade it — evading it is a bug in your
behavior, not a clever workaround.

## Override — only on an explicit instruction

There is a one-time token that lets a single command through. It exists for
sessions where the user genuinely has no terminal (remote or headless) and has
**said so in words**, e.g. "you run it" or "commit it yourself".

It is not an alternative you may choose because delegating is slow. If the user
has not explicitly told you to run the commit yourself, delegate.

When they have:

```bash
python3 -c "import hashlib,pathlib,subprocess,sys; \
d=subprocess.run(['git','rev-parse','--absolute-git-dir'],capture_output=True,text=True).stdout.strip(); \
p=pathlib.Path.home()/'.claude'/'.commit-guard-token'; p.parent.mkdir(parents=True,exist_ok=True); \
p.write_text(hashlib.sha256((sys.argv[1]+'\0'+d).encode()).hexdigest())" '<exact-command-here>'
```

Replace `<exact-command-here>` with the full original command string,
character for character, then run that command unchanged.

Token mechanics:

- File: `~/.claude/.commit-guard-token`
- Content: `sha256(command + "\0" + absolute_git_dir)` — bound to **both** the
  exact command and the repo, so a token minted for one repo cannot be spent in
  another
- Single-use: the hook deletes it immediately after a successful match
- Any change to the command requires a new token

## Modes

`COMMIT_GUARD_MODE` controls the hook:

| Value | Behavior |
| --- | --- |
| `delegate` | Always hand off, even if the session looks unattended |
| `deny` | Block with an explanation, arm no watcher |
| `off` | Pass through — for CI bots that legitimately commit |
| unset | Attended ⇒ `delegate`; unattended ⇒ `deny` |

In an unattended run there is nobody to hand the command to, so the default is
`deny`. If you are blocked that way, report it and stop — do not try to satisfy
the task another way.
