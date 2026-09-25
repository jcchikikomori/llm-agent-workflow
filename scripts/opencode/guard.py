"""Repository guard (Design Doc F8): realpaths, git lookups and the G0-G3 verdicts.

Every write, overwrite, backup and delete target is checked before it is touched. The rules run in order and the
first one that fires gives the verdict:

1. resolve the realpath (a path that does not exist yet resolves through its parent directory)
1. G0 containment: under realpath(scope root) or the realpath of a writable scope dir, else `blocked:G0`
1. without git on PATH, or when a git query fails, a target below a `.git` ancestor is `blocked:git-unavailable`
   (fail closed, IP-9)
1. G1: the nearest repo's origin repo name, or any superproject's, is blocked, giving `blocked:G1`
1. G2: under realpath(<scope>/skills) or realpath(<scope>/skill), giving `blocked:G2`
1. G3: the nearest repo is a submodule that is not in the allowed repos, giving `approve:<toplevel>`

`--allow-repo` only answers G3. This module is the only one in scripts/opencode that runs git.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

NO_VALUE = "-"
VERDICT_OK = "ok"
BLOCKED_G0 = "blocked:G0"
BLOCKED_G1 = "blocked:G1"
BLOCKED_G2 = "blocked:G2"
BLOCKED_GIT_UNAVAILABLE = "blocked:git-unavailable"
APPROVE_PREFIX = "approve:"

GIT_TIMEOUT_SECONDS = 30
# Env vars that would point git at a repository other than the one a path lives in.
GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                     "GIT_CEILING_DIRECTORIES", "GIT_NAMESPACE")
# `git remote get-url` exits 2 when the remote does not exist: that answers "no origin", it is not a failure.
NO_SUCH_REMOTE_EXIT = 2
# `scheme://userinfo@host/...`; the greedy userinfo also covers a raw `@` inside a password.
_URL_USERINFO = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/?#]*@")
# scp-like `user@host:path`.
_SCP_USERINFO = re.compile(r"^[^/:@]+@(?=[^/:]+:)")


# -- paths ----------------------------------------------------------------------


def resolve_target(path):
    """The realpath a write or delete of PATH would reach.

    Symlinks and `..` are resolved component by component, the way the kernel walks the path, so a lexical
    `link/..` never hides where it leads. Components that do not exist yet are appended to the realpath of the
    deepest existing ancestor, so a new file resolves through its parent directory.
    """
    return os.path.realpath(os.fspath(path))


def is_within(path, root):
    """True when realpath PATH is ROOT or lies below it."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def scope_realpaths(scope_root, names):
    """realpath(<scope root>/<name>) for each NAME, through any symlink the scope holds."""
    return [os.path.realpath(os.path.join(os.fspath(scope_root), name)) for name in names]


def containment_roots(scope_root, writable_scope_dirs):
    """The G0 roots: realpath(scope root) plus the realpath of each writable scope dir."""
    return [os.path.realpath(os.fspath(scope_root))] + scope_realpaths(scope_root, writable_scope_dirs)


def is_contained(realpath, roots):
    """G0: True when REALPATH lies under one of ROOTS."""
    return any(is_within(realpath, root) for root in roots)


def lookup_dir(realpath):
    """The directory git is asked about: the target itself when it is a dir, else its deepest existing parent."""
    directory = realpath if os.path.isdir(realpath) else os.path.dirname(realpath)
    while not os.path.isdir(directory):
        directory = os.path.dirname(directory)
    return directory


def has_git_ancestor(directory):
    """True when DIRECTORY or any ancestor holds a `.git` entry (dir or gitfile)."""
    current = directory
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


# -- remotes ------------------------------------------------------------------


def strip_credentials(url):
    """URL without any `user:token@` or `user@` userinfo."""
    stripped = _URL_USERINFO.sub(r"\g<scheme>", url, count=1)
    if stripped == url:
        stripped = _SCP_USERINFO.sub("", url, count=1)
    return stripped


def origin_repo_name(url):
    """The repo name an origin URL points at: its last path segment without `.git`."""
    tail = re.split(r"[/:]", url.rstrip("/"))[-1]
    return tail[:-len(".git")] if tail.endswith(".git") else tail


# -- git ----------------------------------------------------------------------


def _git_env(environ):
    env = {key: value for key, value in environ.items() if key not in GIT_LOCATION_VARS}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class GitQueryError(RuntimeError):
    """An origin or superproject query failed, so G1 or G3 cannot be judged; the guard blocks the target."""


class GitLookup:
    """Read-only git queries, cached per (directory, query) for the life of the object.

    A failed toplevel query answers None and keeps git's reason in `failure(directory)`; the guard turns an
    unanswered query below a `.git` ancestor into a block. A failed origin or superproject query raises
    GitQueryError, which the guard also turns into a block. Either way a failure never lets a target through.
    """

    def __init__(self, program=None, environ=None):
        self.program = shutil.which("git") if program is None else program
        self._env = _git_env(os.environ if environ is None else environ)
        self._answers = {}
        self._failures = {}
        self._failed_queries = {}

    @property
    def available(self):
        return self.program is not None

    def toplevel(self, directory):
        """The realpath of the work tree holding DIRECTORY, or None."""
        answer = self._query(directory, "rev-parse", "--show-toplevel")
        return os.path.realpath(answer) if answer else None

    def superproject(self, toplevel):
        """The superproject work tree's realpath when TOPLEVEL is a submodule, or None; GitQueryError on failure."""
        answer = self._required(toplevel, "rev-parse", "--show-superproject-working-tree")
        return os.path.realpath(answer) if answer else None

    def origin(self, toplevel):
        """The origin URL of TOPLEVEL without credentials, or None when there is no origin; GitQueryError on failure."""
        answer = self._required(toplevel, "remote", "get-url", "origin", answer_exits=(NO_SUCH_REMOTE_EXIT,))
        return strip_credentials(answer) if answer else None

    def failure(self, directory):
        """Why the last failed query in DIRECTORY failed, or None."""
        if not self.available:
            return "git is not on PATH"
        return self._failures.get(directory)

    def _required(self, directory, *args, answer_exits=()):
        """Like _query, but a failed query raises GitQueryError instead of answering None."""
        answer = self._query(directory, *args, answer_exits=answer_exits)
        reason = self._failed_queries.get((directory, args))
        if reason is not None:
            raise GitQueryError(reason)
        return answer

    def _query(self, directory, *args, answer_exits=()):
        """git's stdout for ARGS in DIRECTORY, or None; an exit code in ANSWER_EXITS is an empty answer."""
        key = (directory, args)
        if key not in self._answers:
            self._answers[key] = self._run(directory, args, answer_exits)
        return self._answers[key]

    def _run(self, directory, args, answer_exits):
        if not self.available:
            return None
        command = " ".join(("git",) + args)
        try:
            result = subprocess.run([self.program, "-C", directory, *args], env=self._env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=GIT_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            return self._fail(directory, args, f"{command} failed: {error}")
        if result.returncode in answer_exits:
            return None
        if result.returncode != 0:
            reason = (result.stderr.strip().splitlines() or [f"exit {result.returncode}"])[0]
            return self._fail(directory, args, f"{command} failed: {reason}")
        return result.stdout.rstrip("\n") or None

    def _fail(self, directory, args, reason):
        """Record why ARGS failed in DIRECTORY; the query answers None."""
        self._failures[directory] = reason
        self._failed_queries[(directory, args)] = reason
        return None


# -- verdicts -----------------------------------------------------------------


@dataclass(frozen=True)
class GuardPolicy:
    """The F8 inputs: the scope root, the `mapping.json` guard section and the approved repos."""

    scope_root: str
    blocked_origin_repo_names: tuple
    blocked_scope_dirs: tuple
    writable_scope_dirs: tuple
    allowed_repos: tuple = ()

    @classmethod
    def from_mapping(cls, guard_section, scope_root, allowed_repos=()):
        return cls(
            scope_root=os.fspath(scope_root),
            blocked_origin_repo_names=tuple(guard_section["blocked_origin_repo_names"]),
            blocked_scope_dirs=tuple(guard_section["blocked_scope_dirs"]),
            writable_scope_dirs=tuple(guard_section["writable_scope_dirs"]),
            allowed_repos=tuple(os.fspath(repo) for repo in allowed_repos),
        )


@dataclass(frozen=True)
class Verdict:
    """One guard row; `reason` explains a git-unavailable block and goes to stderr, never into the row."""

    path: str
    realpath: str
    repo: str
    origin: str
    verdict: str
    reason: str = ""

    @property
    def ok(self):
        return self.verdict == VERDICT_OK

    @property
    def rule(self):
        """The rule that fired (`G0`, `git-unavailable`, ...), or the verdict itself when nothing was blocked."""
        return self.verdict.partition(":")[2] or self.verdict

    def row(self):
        return "\t".join((self.path, self.realpath, self.repo, self.origin, self.verdict))


class Guard:
    """Evaluates targets against one GuardPolicy, sharing one GitLookup cache."""

    def __init__(self, policy, git=None):
        self.git = GitLookup() if git is None else git
        self._roots = containment_roots(policy.scope_root, policy.writable_scope_dirs)
        self._blocked_dirs = scope_realpaths(policy.scope_root, policy.blocked_scope_dirs)
        self._blocked_names = {name.casefold() for name in policy.blocked_origin_repo_names}
        self._allowed = {os.path.realpath(repo) for repo in policy.allowed_repos}

    def check(self, path):
        """The Verdict for PATH (write and delete use the same rules)."""
        path = os.fspath(path)
        realpath = resolve_target(path)
        directory = lookup_dir(realpath)
        repo = self.git.toplevel(directory)
        try:
            verdict, reason = self._verdict(realpath, directory, repo)
        except GitQueryError as error:
            verdict, reason = BLOCKED_GIT_UNAVAILABLE, str(error)
        return Verdict(path, realpath, repo or NO_VALUE, self._origin_column(repo), verdict, reason)

    def _origin_column(self, repo):
        """The credential-free origin of REPO for the row, or `-` when there is none or git cannot say."""
        try:
            origin = self.git.origin(repo) if repo else None
        except GitQueryError:
            origin = None
        return origin or NO_VALUE

    def _verdict(self, realpath, directory, repo):
        if not is_contained(realpath, self._roots):
            return BLOCKED_G0, ""
        if repo is None and has_git_ancestor(directory):
            return BLOCKED_GIT_UNAVAILABLE, self.git.failure(directory) or "git gave no work tree"
        if repo is not None and self._in_blocked_checkout(repo):
            return BLOCKED_G1, ""
        if is_contained(realpath, self._blocked_dirs):
            return BLOCKED_G2, ""
        if repo is not None and self.git.superproject(repo) and repo not in self._allowed:
            return f"{APPROVE_PREFIX}{repo}", ""
        return VERDICT_OK, ""

    def _in_blocked_checkout(self, toplevel):
        """G1: TOPLEVEL or any superproject above it has a blocked origin repo name."""
        seen = set()
        current = toplevel
        while current is not None and current not in seen:
            seen.add(current)
            origin = self.git.origin(current)
            if origin is not None and origin_repo_name(origin).casefold() in self._blocked_names:
                return True
            current = self.git.superproject(current)
        return False
