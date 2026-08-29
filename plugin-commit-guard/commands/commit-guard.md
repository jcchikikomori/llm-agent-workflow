---
description: Guides Claude through the commit-guard approval flow when a git commit is blocked — show staged files and commit message to user, get approval, write one-time token, retry.
---

# commit-guard

When `git commit` is blocked by the commit-guard hook, follow this procedure exactly:

## Approval flow

1. **Gather context** — run both commands in parallel:
   - `git diff --cached --stat`
   - `git diff --cached --name-only`

2. **Extract commit message** — parse the `-m "..."` or `--message="..."` value from the blocked command. If no `-m` flag, note that git will open an editor or use a template.

3. **Show the user**:
   - Staged files (from step 1)
   - Commit message (from step 2)
   - The exact `git commit` command that will be run

4. **Ask for approval**: "Proceed with this commit?"

5. **If YES** — write the one-time approval token, then retry the exact command:

   ```bash
   python3 -c "import hashlib,sys,pathlib,time; cmd=sys.argv[1]; cwd=sys.argv[2]; p=pathlib.Path.home()/'.config'/'opencode'/'.commit-guard-token'; p.parent.mkdir(exist_ok=True); p.write_text(hashlib.sha256((cmd+'|'+cwd+'|'+str(time.time())).encode()).hexdigest())" "<exact-command-here>" "<cwd>"
   ```

   Replace `<exact-command-here>` with the full original command string, character-for-character, and `<cwd>` with the working directory the command runs in. Then run the original `git commit` command unchanged.

6. **If NO** — abort. Do not retry. Do not suggest an alternative.

## GPG signing rules — never violate these

- **Never** add `--no-gpg-sign` or `-c commit.gpgsign=false`
- **Never** strip `-S` or `--gpg-sign` from a command
- **Never** set `GIT_COMMITTER_SIGNING_KEY` or override GPG config
- If the repository requires signed commits, git invokes gpg-agent after the token check passes
- The user enters their GPG passphrase through the normal pinentry dialog — this is expected behavior, not an error

## Token mechanics

- Token file: `~/.config/opencode/.commit-guard-token`
- Content: SHA256 hex digest of the exact command + cwd + timestamp
- Single-use: hook consumes it on the next `git commit` pass-through, then blocks again
- Token grants one approval — a later `git commit` requires a fresh user approval