---
description: Guides the agent through the commit-guard delegation flow when a git command that writes a commit message is intercepted — show the staged work, hand the exact command to the user, arm the bounded watcher, verify what landed.
---

# commit-guard

When commit-guard intercepts a git command, it does **not** want you to ask
permission and then run the command yourself. Hand the command to the user and
wait.

## Why the user runs it, not you

The agent shell cannot do this work: `GIT_EDITOR=true` is exported, so an editor
never opens and a template is silently accepted; and `tty` reports "not a tty",
so GPG pinentry cannot prompt. On a repo with `commit.gpgsign=true` no signed
commit can be produced from this shell at all.

The handed-over command therefore **behaves differently in the user's terminal**
— a real editor opens where the agent would have had `GIT_EDITOR=true`. Say so.

## Delegation flow

1. **Gather context** — run both in parallel:
   - `git diff --cached --stat`
   - `git diff --cached --name-only`

2. **Show the user**: staged files, the commit message, and the exact command
   verbatim in a copy-pasteable block. Say plainly that they run it themselves.
   If it is a chain (`git commit -m x && git push`), say the push is included.

3. **Arm the watcher** using the invocation the hook printed, as a background
   process. Never in the foreground — that blocks for up to 30 minutes. If it
   cannot be armed, that is a normal outcome: say so and wait for the user.

4. **Handle the verdict** — the watcher emits one line and exits:

   | Verdict | Exit | Action |
   | --- | --- | --- |
   | `DONE <op> <sha>` | 0 | Verify (step 5), then continue |
   | `ABORTED <op> <evidence>` | 3 | Report real state, stop |
   | `TIMEOUT <op> <reason>` | 4 | Report real state, ask the user |
   | `ERROR <op> <reason>` | 64/65/66 | No verdict reached — ask the user |

5. **Verify before claiming success.** `DONE` means *something* committed, not
   that your command ran:

   ```bash
   git show --stat --format='%H %G? %an %s' <sha>
   ```

   `%G?` is the signature status — `G` good, `U` untrusted, `N` none. An `N` in a
   repo with `commit.gpgsign=true` means it is unsigned. Say so instead of
   reporting success.

6. **On anything other than `DONE`** — run `git status --short --branch` and
   `git log --oneline -3`, show the user the real state, and stop. Do not re-run
   the command, do not re-arm the watcher, do not build a workaround.

## GPG signing rules — never violate these

- **Never** add `--no-gpg-sign` or `-c commit.gpgsign=false`
- **Never** strip `-S` or `--gpg-sign` from a command
- **Never** set `GIT_COMMITTER_SIGNING_KEY` or override GPG config
- Pinentry prompting for a passphrase is expected behavior, not an error

## Never route around the guard

Do not write the command into a script and run the script, and do not reach for
`git commit-tree` or `git update-ref`. The guard hooks the bash tool, so those
evade it — that is a bug in your behavior, not a workaround.

## Override — only on an explicit instruction

A one-time token lets a single command through. It exists for sessions where the
user genuinely has no terminal and has **said so in words**. It is not an
alternative you may pick because delegating is slow.

```bash
python3 -c "import hashlib,pathlib,subprocess,sys; \
d=subprocess.run(['git','rev-parse','--absolute-git-dir'],capture_output=True,text=True).stdout.strip(); \
p=pathlib.Path.home()/'.config'/'opencode'/'.commit-guard-token'; p.parent.mkdir(parents=True,exist_ok=True); \
p.write_text(hashlib.sha256((sys.argv[1]+'\0'+d).encode()).hexdigest())" '<exact-command-here>'
```

Replace `<exact-command-here>` with the full original command string, character
for character, then run that command unchanged.

- Token file: `~/.config/opencode/.commit-guard-token`
- Content: `sha256(command + "\0" + absolute_git_dir)` — bound to both the exact
  command and the repo, so a token minted for one repo cannot be spent in another
- Single-use: consumed immediately on a successful match

## Modes

`COMMIT_GUARD_MODE` controls the guard: `delegate` always hands off, `deny`
blocks with an explanation, `off` passes through. Unset means delegate when
attended and deny when not — in an unattended run there is nobody to hand the
command to.
