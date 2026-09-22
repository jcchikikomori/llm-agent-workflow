#!/usr/bin/env python3
"""
commit-guard PreToolUse hook.

Hands every git operation that needs a commit message to the human instead of
running it in the agent's shell, then tells Claude to arm a bounded watcher
(hooks/await_commit.sh) that reports when the human finished, aborted, or ran
out of time.

Why delegate rather than just run it: Claude Code exports GIT_EDITOR=true into
the Bash tool, so a bare `git commit` silently accepts the template instead of
opening an editor, and `tty` reports "not a tty" so GPG pinentry cannot prompt.
On a repo with commit.gpgsign=true the agent cannot produce a signed commit at
all. The user's terminal can.

Exit codes:
  0 - allow the tool call to proceed
  2 - block it; stderr is fed back to Claude as context

Every path is wrapped so an internal error BLOCKS (exit 2) rather than letting
an unguarded commit through: per the hooks spec an exit code other than 0 or 2
is a *non-blocking* error, which is a fail-open.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
AWAIT_SCRIPT = HOOK_DIR / "await_commit.sh"
# Overridable so the OpenCode port can point at its own config dir while
# still running this exact classifier -- one source of truth, no drift.
TOKEN_FILE = Path(
    os.environ.get("COMMIT_GUARD_TOKEN_FILE")
    or Path.home() / ".claude" / ".commit-guard-token"
)

GIT_TIMEOUT = 2

# A `git` word, however it is spelled on the command line.
#
# The lookbehind excludes ONLY word characters and `-`, so `legit` and
# `foo-git` do not match. It must NOT exclude `/` or `.`: doing so made every
# path-qualified invocation invisible to the fast path -- `/usr/bin/git commit`,
# `./git commit` and `"/usr/bin/git" commit` all returned exit 0 on line one and
# never reached the classifier. A loose match here is harmless, because
# basename() resolution in scan() is what actually decides.
GIT_WORD = re.compile(r"(?<![\w-])git(?:\.exe)?(?![\w.-])")

# Shell constructs whose expansion we cannot resolve statically. If one of these
# appears alongside a git word and normal parsing found nothing, we cannot prove
# the command is safe, so we fail CLOSED rather than allow it.
OPAQUE = re.compile(r"\$\(|`|\$\{|\beval\b|\bxargs\b|\|\s*(?:sh|bash|zsh|dash|ksh)\b")

HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# Shell operators that end a simple command.
SEPARATORS = {";", "&", "&&", "||", "|", "|&", "(", ")"}
# Redirection tokens that are never part of argv.
REDIRECTS = {"<", ">", ">>", "<<", "<<<", "2>", "2>>", "&>"}

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash"}

WRAPPERS = {
    "env", "command", "builtin", "exec", "nohup", "nice", "ionice",
    "stdbuf", "time", "timeout", "sudo", "doas",
}
# Wrapper flags that consume the NEXT token, so we do not mistake that value
# for the wrapped command name (`env -u GIT_EDITOR git commit` must still see git).
WRAPPER_VALUE_FLAGS = {
    "env": {"-u", "--unset", "-S", "--split-string", "-C", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "--class", "-n", "--classdata", "-p", "--pid"},
    "stdbuf": {"-i", "--input", "-o", "--output", "-e", "--error"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "sudo": {"-u", "--user", "-g", "--group", "-C", "--close-from", "-h",
             "--host", "-p", "--prompt", "-r", "--role", "-t", "--type",
             "-U", "--other-user"},
    "doas": {"-u", "-C"},
}

# git's own options, which sit BEFORE the subcommand.
GIT_VALUE_OPTS = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace",
    "--config-env", "--super-prefix",
}
GIT_BOOL_OPTS = {
    "-p", "--paginate", "-P", "--no-pager", "--bare", "--no-replace-objects",
    "--no-optional-locks", "--no-advice", "--no-lazy-fetch",
    "--literal-pathspecs", "--glob-pathspecs", "--icase-pathspecs",
    "--noglob-pathspecs", "--exec-path", "--html-path", "--man-path",
    "--info-path", "--version", "--help",
}

# Flags that make a subcommand NOT create a commit, so intercepting it would
# arm a watcher whose predicate can never be satisfied -- a guaranteed
# 30-minute false TIMEOUT. See README "Known limits".
NEVER = {
    # --dry-run and friends are read-only. --short/--porcelain/--long imply it.
    "commit": {"--dry-run", "--short", "--porcelain", "--long"},
    # --squash and --no-commit do not move HEAD or write MERGE_HEAD at all.
    "merge": {"--abort", "--quit", "--ff-only", "--squash", "--no-commit", "-n"},
    "cherry-pick": {"--abort", "--quit", "--no-commit", "-n"},
    "revert": {"--abort", "--quit", "--no-commit", "-n"},
    # --edit-todo leaves the rebase in progress, so "no rebase dirs" is false
    # by construction. --show-current-patch is read-only.
    "rebase": {"--abort", "--quit", "--edit-todo", "--show-current-patch"},
    "am": {"--abort", "--quit", "--show-current-patch"},
    "tag": {"-d", "--delete", "-l", "--list", "-v", "--verify"},
}

# Tag flags that consume the next token, so the tag NAME is found correctly.
TAG_VALUE_FLAGS = {"-m", "--message", "-F", "--file", "-u", "--local-user",
                   "--cleanup", "--sort", "--format", "--points-at",
                   "--contains", "--no-contains", "--merged", "--no-merged"}
TAG_ANNOTATE = {"-a", "--annotate", "-s", "--sign", "-u", "--local-user"}


def run_git(args, cwd=None):
    """Run git, returning stripped stdout or None. Always bounded: a stall on
    index.lock would otherwise burn the 60s hook budget and fail open."""
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True,
            text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def newlines_to_semicolons(text):
    """Turn unquoted newlines into `;` so multi-line commands split correctly.
    shlex with whitespace_split treats a newline as plain whitespace, which
    would glue `git status\\ngit commit -m x` into one simple command and hide
    the commit."""
    out = []
    quote = None
    escaped = False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            out.append(ch)
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        out.append(";" if ch == "\n" else ch)
    return "".join(out)


def strip_heredocs(text):
    """Drop heredoc bodies. Without this, `cat <<'EOF' ... git commit ... EOF`
    arms a 30-minute watcher for text that is never executed."""
    lines = text.split("\n")
    kept = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        match = HEREDOC.search(line)
        index += 1
        if match:
            delimiter = match.group(2)
            while index < len(lines) and lines[index].strip() != delimiter:
                index += 1
            index += 1  # drop the closing delimiter line too
    return "\n".join(kept)


def tokenize(text):
    """Tokenize shell-ish. Raises ValueError on unbalanced quotes; callers must
    treat that as fail-closed, not as 'nothing found'."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def split_commands(tokens):
    """Split a token stream into simple commands on shell operators."""
    commands = []
    current = []
    for token in tokens:
        if token in SEPARATORS:
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def strip_wrappers(argv):
    """Remove leading VAR=value assignments and wrapper programs, so that
    `env FOO=1 sudo -u bob git commit` still resolves to a git invocation."""
    argv = [tok for tok in argv if tok not in REDIRECTS]
    while argv:
        head = os.path.basename(argv[0])
        if "=" in argv[0] and not argv[0].startswith("-") and \
                re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]
            continue
        if head in WRAPPERS:
            value_flags = WRAPPER_VALUE_FLAGS.get(head, set())
            argv = argv[1:]
            while argv and argv[0].startswith("-") and argv[0] != "--":
                token = argv[0]
                flag = token.split("=", 1)[0]
                argv = argv[1:]
                if flag in value_flags and "=" not in token and argv:
                    argv = argv[1:]
            if argv and argv[0] == "--":
                argv = argv[1:]
            # `env VAR=1 git ...` -- assignments come after the wrapper too.
            while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
                argv = argv[1:]
            continue
        break
    return argv


def split_git_options(argv):
    """Consume git's own options and return (subcommand, rest, overrides)."""
    overrides = {}
    index = 1
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            break
        name, _, inline = token.partition("=")
        if name in GIT_VALUE_OPTS:
            if inline:
                value = inline
                index += 1
            else:
                value = argv[index + 1] if index + 1 < len(argv) else ""
                index += 2
            if name == "-C":
                overrides.setdefault("chdir", []).append(value)
            elif name == "--git-dir":
                overrides["git_dir"] = value
            elif name == "--work-tree":
                overrides["work_tree"] = value
            continue
        if name in GIT_BOOL_OPTS or token in GIT_BOOL_OPTS:
            index += 1
            continue
        # Unknown git-level option: skip it rather than mistake it for the
        # subcommand.
        index += 1
    if index >= len(argv):
        return None, [], overrides
    return argv[index], argv[index + 1:], overrides


def expand_flags(args):
    """Flatten args into a set of flag names for presence checks, splitting
    bundled shorts (-an -> -a -n) and dropping =values. Stops at `--`."""
    flags = set()
    for token in args:
        if token == "--":
            break
        if token.startswith("--"):
            flags.add(token.split("=", 1)[0])
        elif token.startswith("-") and len(token) > 1:
            for char in token[1:]:
                if not char.isalnum():
                    break
                flags.add("-" + char)
    return flags


def tag_name_of(args):
    """First positional that is not consumed by a value-taking tag flag."""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            index += 1
            if name in TAG_VALUE_FLAGS and "=" not in token:
                index += 1
            continue
        return token
    return args[index] if index < len(args) else None


def pull_op(flags, args):
    """git pull only needs delegating when a commit is guaranteed: an explicit
    rebase, or an explicit no-ff merge. Plain `git pull` is overwhelmingly a
    fast-forward and intercepting it would wreck routine work."""
    for token in args:
        if token.startswith("--rebase="):
            return "rebase" if token.split("=", 1)[1] not in ("false", "no") else None
    if "--no-rebase" in flags or "--ff-only" in flags:
        return None
    if "--rebase" in flags:
        return "rebase"
    if "--no-ff" in flags:
        return "merge"
    return None


def classify(subcommand, args):
    """Return the watcher op for a subcommand, or None to allow it through."""
    flags = expand_flags(args)
    blocked = NEVER.get(subcommand, set())
    if subcommand == "commit":
        if flags & blocked:
            return None
        return "amend" if "--amend" in flags else "commit"
    if subcommand in ("merge", "cherry-pick", "revert", "rebase", "am"):
        if flags & blocked:
            return None
        return "rebase" if subcommand == "rebase" else subcommand
    if subcommand == "tag":
        if flags & blocked:
            return None
        return "tag" if flags & TAG_ANNOTATE else None
    if subcommand == "pull":
        return pull_op(flags, args)
    return None


def resolve_alias(subcommand, cwd):
    """Resolve one level of `git config alias.X`. A `!`-prefixed shell alias is
    opaque, so it is left alone rather than guessed at."""
    expansion = run_git(["config", "--get", "alias." + subcommand], cwd=cwd)
    if not expansion or expansion.startswith("!"):
        return None
    try:
        return shlex.split(expansion)
    except ValueError:
        return None


def scan(command, base_cwd, depth=0):
    """Walk every simple command. Returns (hits, saw_git).

    Each hit is (op, cwd, git_dir_override, tag_name). `saw_git` is True if a
    real git invocation was resolved -- even a harmless one like `git status`.
    It is what lets an opaque construct fail closed only when the parser
    understood nothing, so `git log --format=$(...)` stays allowed while
    `$(which git) commit` does not.
    """
    hits = []
    saw_git = False
    if depth > 3:
        return hits, saw_git
    # Heredocs must be stripped FIRST: the stripper is line-based, and
    # newlines_to_semicolons would leave it nothing to work with.
    tokens = tokenize(newlines_to_semicolons(strip_heredocs(command)))
    cwd = base_cwd
    for argv in split_commands(tokens):
        argv = strip_wrappers(argv)
        if not argv:
            continue
        head = os.path.basename(argv[0])
        # `cd X && git commit` -- track the directory so pre-state is captured
        # against the repo the command will actually run in.
        if head == "cd" and len(argv) > 1:
            cwd = argv[1] if os.path.isabs(argv[1]) else os.path.join(cwd, argv[1])
            continue
        if head in SHELLS:
            payload = None
            for index, token in enumerate(argv[1:], start=1):
                if token == "-c" and index + 1 < len(argv):
                    payload = argv[index + 1]
                    break
            if payload:
                nested_hits, nested_saw = scan(payload, cwd, depth + 1)
                hits.extend(nested_hits)
                saw_git = saw_git or nested_saw
            continue
        if head not in ("git", "git.exe"):
            continue
        saw_git = True
        subcommand, args, overrides = split_git_options(argv)
        if subcommand is None:
            continue
        effective = cwd
        for chdir in overrides.get("chdir", []):
            effective = chdir if os.path.isabs(chdir) else os.path.join(effective, chdir)
        alias = resolve_alias(subcommand, effective)
        if alias:
            subcommand, args = alias[0], alias[1:] + args
        op = classify(subcommand, args)
        if op:
            hits.append((op, effective, overrides.get("git_dir"),
                         tag_name_of(args) if op == "tag" else None))
    return hits, saw_git


STATE_DIR = Path(
    os.environ.get("COMMIT_GUARD_STATE_DIR")
    or Path.home() / ".claude" / ".commit-guard"
)
LEDGER_DIR = STATE_DIR / "ledger"
LEDGER_TTL = 1800  # seconds; matches the watcher's 30-minute ceiling


def read_file(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def git_state(cwd, git_dir_override, tag_name):
    """Snapshot the repo AT BLOCK TIME.

    Capturing here rather than inside the watcher removes a race: if the user
    commits between the block and the watcher arming, the watcher still
    compares against the pre-block sha and correctly reports DONE.
    """
    prefix = ["--git-dir", git_dir_override] if git_dir_override else []
    work = cwd if cwd and os.path.isdir(cwd) else None
    git_dir = run_git(prefix + ["rev-parse", "--absolute-git-dir"], cwd=work)
    if not git_dir:
        return None
    # NEVER assume "<top>/.git": in a linked worktree .git is a FILE, and
    # MERGE_HEAD / rebase-merge / sequencer live under .git/worktrees/<name>/.
    root = Path(git_dir)
    top = run_git(prefix + ["rev-parse", "--show-toplevel"], cwd=work)
    head = run_git(prefix + ["rev-parse", "--verify", "-q", "HEAD"], cwd=work) or "unborn"

    markers = [
        name for name in (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
            "rebase-merge", "rebase-apply", "sequencer",
        )
        if (root / name).exists()
    ]

    orig_head = ""
    for relative in ("rebase-merge/orig-head", "rebase-apply/orig-head"):
        candidate = root / relative
        if candidate.is_file():
            orig_head = read_file(candidate)
            break
    seq_head = read_file(root / "sequencer" / "head")

    pre_tag = ""
    if tag_name:
        pre_tag = run_git(
            prefix + ["rev-parse", "-q", "--verify", "refs/tags/" + tag_name],
            cwd=work,
        ) or ""

    return {
        "dir": top or work or os.getcwd(),
        "git_dir": git_dir,
        "head": head,
        "markers": ",".join(markers),
        "orig_head": orig_head,
        "seq_head": seq_head,
        "tag": tag_name or "",
        "pre_tag": pre_tag,
    }


def digest(command, git_dir):
    """Bind the approval token to BOTH the command and the repo, so a token
    minted for one repo cannot be spent in another."""
    return hashlib.sha256(
        (command + "\0" + (git_dir or "")).encode("utf-8", "replace")
    ).hexdigest()


def consume_token(command, git_dir):
    """Single-use, command-bound, repo-bound. Returns True if this call was
    explicitly approved by the user via the override in SKILL.md."""
    try:
        stored = TOKEN_FILE.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return False
    if not stored or stored != digest(command, git_dir):
        return False
    try:
        TOKEN_FILE.unlink()
    except OSError:
        pass
    return True


def resolve_mode():
    mode = os.environ.get("COMMIT_GUARD_MODE", "").strip().lower()
    if mode in ("delegate", "deny", "off"):
        return mode
    # CLAUDE_CODE_SESSION_ATTENDED is undocumented, so COMMIT_GUARD_MODE is the
    # supported control; this is only the default when nothing is set.
    return "delegate" if os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "1" else "deny"


def ledger_check(key):
    """Guard against watcher pile-up: a retry after the user says 'done' would
    otherwise block again and arm a second watcher."""
    entry = LEDGER_DIR / (key + ".json")
    try:
        age = int(__import__("time").time() - entry.stat().st_mtime)
    except OSError:
        return None
    return age if age < LEDGER_TTL else None


def ledger_write(key, command, op):
    try:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        (LEDGER_DIR / (key + ".json")).write_text(
            json.dumps({"op": op, "command": command})
        )
    except OSError:
        pass


def watcher_invocation(op, state, result_file):
    parts = [
        "bash", str(AWAIT_SCRIPT),
        "--op", op,
        "--dir", state["dir"],
        "--git-dir", state["git_dir"],
        "--pre-head", state["head"],
        "--pre-markers", state["markers"],
        "--pre-orig-head", state["orig_head"],
        "--pre-seq-head", state["seq_head"],
        "--result-file", result_file,
    ]
    if state["tag"]:
        parts += ["--tag", state["tag"], "--pre-tag", state["pre_tag"]]
    return " ".join(shlex.quote(part) for part in parts)


DELEGATE = """\
[commit-guard] DELEGATED: this command writes a commit message and must be run
by the user, in their own terminal.

COMMAND TO HAND OVER (verbatim, do not reword or split it):
  {command}

WHY: the agent shell cannot do this. GIT_EDITOR=true is exported here, so an
editor never opens and a message template would be silently accepted; and tty
reports "not a tty", so GPG pinentry cannot prompt. On a repo with
commit.gpgsign=true no signed commit can be produced from this shell at all.

Tell the user this command behaves DIFFERENTLY in their terminal than it would
have here -- a real editor opens where the agent would have had GIT_EDITOR=true.

DO THIS, IN ORDER:
  1. Run `git diff --cached --stat` and `git diff --cached --name-only`.
  2. Show the user the staged files, the commit message, and the command above
     in a copy-pasteable block. Say plainly that they run it themselves.
  3. Arm the watcher -- Bash tool, run_in_background: true:

     {watcher}

     If the watcher cannot be armed (sandboxed or --print mode has no background
     shell), that is a NORMAL outcome. Say so and wait for the user instead.
     Never run it in the foreground: that blocks the turn for 30 minutes.
  4. Handle the verdict:
     DONE    -> run `git show --stat --format='%H %G? %an %s' <sha>` and
                reconcile it against what you handed over BEFORE claiming
                success. DONE means SOMETHING committed, not that your command
                ran. %G? catches an unsigned commit in a signed repo.
     ABORTED -> run `git status --short --branch` and `git log --oneline -3`,
                show the user the real state, stop.
     TIMEOUT -> same as ABORTED, then ask the user what happened.
     ERROR   -> no verdict was reached. Ask the user. Never infer repo state
                from a missing verdict.

NEVER: run this command yourself, re-arm the watcher, add --no-gpg-sign or
-c commit.gpgsign=false, strip -S/--gpg-sign, or write the command into a
script and run that script.{signing}"""

DENIED = """\
[commit-guard] DENIED: this command writes a commit message, but the session
looks unattended -- there is no user to hand it to. The commit was NOT made.

COMMAND:
  {command}

Set COMMIT_GUARD_MODE=delegate to force the handoff, or COMMIT_GUARD_MODE=off to
disable the guard for this run. Report this to the user and stop. Do not retry,
and do not route around it with a script or with git plumbing."""

REPEAT = """\
[commit-guard] ALREADY DELEGATED {age}s ago -- not arming a second watcher.

COMMAND:
  {command}

Check the real state with `git log --oneline -3` and `git status --short
--branch` before doing anything else. If the user has not run it yet, wait. Do
not run it yourself."""


OPAQUE_BLOCK = """\
[commit-guard] BLOCKED: this command mentions git and contains a construct whose
expansion cannot be resolved before it runs -- command substitution, backticks,
eval, xargs, or a pipe into a shell.

COMMAND:
  {command}

The guard cannot prove this does not write a commit, so it fails closed.

Rewrite it so the git invocation is literal, e.g. `git commit -m "..."` rather
than `$(which git) commit -m "..."`. If it genuinely does not commit anything,
split the git-mentioning part into its own command. Do not route around this.
"""


def emit(text):
    print(text, file=sys.stderr)
    sys.exit(2)


def main():
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "")
    if not command or not GIT_WORD.search(command):
        sys.exit(0)

    mode = resolve_mode()
    if mode == "off":
        sys.exit(0)

    base_cwd = data.get("cwd") or os.getcwd()
    try:
        hits, saw_git = scan(command, base_cwd)
    except ValueError:
        # Unbalanced quotes -- we cannot prove this is safe, so fail CLOSED.
        hits, saw_git = [("commit", base_cwd, None, None)], True

    if not hits:
        # `$(which git) commit`, `echo '...' | bash`, backticks, eval, xargs --
        # the git word is there but the real argv is only knowable at runtime.
        # Allowing these would be a silent bypass, so block and say why.
        if not saw_git and OPAQUE.search(command):
            emit(OPAQUE_BLOCK.format(command=command) if mode != "deny"
                 else DENIED.format(command=command))
        sys.exit(0)

    # A chain like `git add . && git commit -m x` is handed over whole, and the
    # LAST intercepted operation is the one worth watching.
    op, cwd, git_dir_override, tag_name = hits[-1]

    state = git_state(cwd, git_dir_override, tag_name)
    if state is None:
        emit(DENIED.format(command=command) if mode == "deny" else
             DELEGATE.format(command=command, watcher="(repo not readable -- "
                             "ask the user to run it and tell you when done)",
                             signing=""))

    if consume_token(command, state["git_dir"]):
        sys.exit(0)

    if mode == "deny":
        emit(DENIED.format(command=command))

    key = digest(command, state["git_dir"])
    age = ledger_check(key)
    if age is not None:
        emit(REPEAT.format(age=age, command=command))

    result_file = str(STATE_DIR / ("result-" + key[:16] + ".txt"))
    signing = ""
    if run_git(["config", "--get", "commit.gpgsign"], cwd=state["dir"]) == "true":
        signing = ("\n\nNOTE: commit.gpgsign=true in this repo. The user will be "
                   "prompted by pinentry for their GPG passphrase. That is "
                   "expected, not an error.")

    ledger_write(key, command, op)
    emit(DELEGATE.format(
        command=command,
        watcher=watcher_invocation(op, state, result_file),
        signing=signing,
    ))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 -- fail CLOSED, never fail open
        print(
            "[commit-guard] internal error, blocking to be safe: "
            "{0}: {1}\nHand the command to the user and ask them to run it, "
            "then report this bug.".format(type(error).__name__, error),
            file=sys.stderr,
        )
        sys.exit(2)
