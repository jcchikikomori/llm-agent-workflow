"""Shared harness for the opencode port integration and E2E suites.

One Sandbox per test holds a temp HOME, a project dir, an isolated PATH
(stubs in bin/, symlinks to host binaries in tools/) and a private git config,
so no test ever reads the user's real ~/.gitconfig or writes under the real
HOME. Git fixtures use real git (Design Doc "Mock Boundary Decisions": git repo
queries are not mocked); clone and pull go through local file:// remotes.

Stdlib only. This module holds fixtures and runners, no assertions.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP_SCRIPT = REPO_ROOT / "setup-opencode.sh"
CONVERT_SCRIPT = REPO_ROOT / "scripts" / "opencode" / "convert.py"

RUN_TIMEOUT_SECONDS = 120

DOTFILES_ORIGIN = "https://user:token@example.invalid/dotfiles.git"
SKILLS_MD_ORIGIN = "https://example.invalid/jcchikikomori/skills-md.git"
START_ORIGIN = "https://example.invalid/jcchikikomori/skills-md-dev-orchestrator.git"
SKILLS_MD_LAYOUTS = ("nested", "root")

# Host binaries bash, setup-opencode.sh and git need. Only the ones present on
# the host are linked; python3 is linked to the interpreter running the tests.
TOOL_NAMES = (
    "bash", "sh", "python3", "git",
    "cat", "cp", "mv", "rm", "rmdir", "mkdir", "mktemp", "ln", "ls", "readlink", "realpath", "dirname",
    "basename", "date", "sha256sum", "sort", "uniq", "find", "sed", "awk", "grep", "head", "tail", "tr",
    "cut", "wc", "chmod", "env", "touch", "diff", "cmp", "stat", "tee", "xargs", "sleep", "id", "uname",
)

GITCONFIG = """\
[user]
\tname = Sandbox Test
\temail = sandbox@example.invalid
[init]
\tdefaultBranch = main
[commit]
\tgpgsign = false
[tag]
\tgpgsign = false
[protocol "file"]
\tallow = always
"""

RECORD_STUB = """\
#!/usr/bin/env bash
printf '{name} %s\\n' "$*" >> "$STUB_LOG"
"""

# IP-8 contract: argv SRC DEST, exit code. Like the real converter, DEST is the
# skill dir itself and is created with mkdir -p.
CONVERTER_STUB = """\
#!/bin/sh
# Test stub for the skills-md converter: logs "SRC DEST", copies, exits.
set -eu
printf '%s %s\\n' "$1" "$2" >> "${STUB_LOG:?STUB_LOG must be set}"
mkdir -p "$2"
cp -R "$1"/. "$2"/
exit "${STUB_CONVERT_EXIT:-0}"
"""

DEFAULT_SKILLS = {"alpha": "---\nname: alpha\ndescription: 'Alpha fixture skill.'\n---\n\n# Alpha\n"}
START_FILES = {
    "README.md": "skills-md dev-orchestrator fixture\n",
    "agents/orchestrator.md": "---\ndescription: 'Orchestrator fixture agent.'\n---\n\nFixture.\n",
}


class GitError(RuntimeError):
    """A git command inside a sandbox fixture failed."""


class Sandbox:
    """A temp root holding home/, project/, bin/, tools/ and fixtures/.

    Use one per test, as a context manager, or Sandbox() plus addCleanup(sb.cleanup).
    Every path attribute is a realpath, so it compares equal to the realpath
    columns the installer and guard print.
    """

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="opencode-sandbox-")
        self.root = Path(self._tmp.name).resolve()
        if REPO_ROOT in self.root.parents:
            self._tmp.cleanup()
            raise RuntimeError(f"sandbox root {self.root} is inside the repo; set TMPDIR outside {REPO_ROOT}")
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.bin = self.root / "bin"
        self.tools = self.root / "tools"
        self.fixtures = self.root / "fixtures"
        self.stub_log = self.root / "calls.log"
        self.gitconfig = self.root / "gitconfig"
        self.tmpdir = self.root / "tmp"
        for directory in (self.home, self.project, self.bin, self.tools, self.fixtures, self.tmpdir):
            directory.mkdir()
        self.gitconfig.write_text(GITCONFIG)
        _link_tools(self.tools, TOOL_NAMES)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.cleanup()

    def cleanup(self):
        self._tmp.cleanup()

    # -- environment ---------------------------------------------------------

    def env(self, **overrides):
        """Build a fresh environment; an override of None removes that key.

        Nothing is inherited from the caller, so XDG_*, GIT_DIR and every
        value naming the real HOME stay out. GIT_OPTIONAL_LOCKS=0 keeps
        read-only git queries (status) from rewriting an index, so snapshots
        of fixture repos stay stable. GIT_TERMINAL_PROMPT=0 stops git asking
        for credentials on the terminal. PYTHONDONTWRITEBYTECODE=1 keeps
        convert.py runs from writing __pycache__/ into the repo.
        """
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{self.tools}",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "STUB_LOG": str(self.stub_log),
            "TMPDIR": str(self.tmpdir),
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def tools_without(self, *names):
        """Return a PATH value (bin plus tools) that lacks the named tools."""
        unknown = sorted(set(names) - set(TOOL_NAMES))
        if unknown:
            raise ValueError(f"tools_without: not in TOOL_NAMES: {', '.join(unknown)}")
        suffix = "-".join(sorted(names)) or "none"
        directory = self.root / f"tools-without-{suffix}"
        if not directory.exists():
            directory.mkdir()
            _link_tools(directory, [name for name in TOOL_NAMES if name not in names])
        return f"{self.bin}:{directory}"

    # -- stubs ---------------------------------------------------------------

    def stub(self, name, body=None):
        """Write an executable stub to bin/; by default it logs its argv."""
        path = self.bin / name
        path.write_text(body or RECORD_STUB.format(name=name))
        path.chmod(0o755)
        return path

    def calls(self):
        """Lines appended to STUB_LOG so far."""
        return self.stub_log.read_text().splitlines() if self.stub_log.exists() else []

    # -- git fixtures --------------------------------------------------------

    def git(self, cwd, *args, check=True):
        """Run real git with the sandbox env; raise GitError on failure."""
        result = subprocess.run(
            ["git", *map(str, args)], cwd=cwd, env=self.env(), input="",
            capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(map(str, args))} failed in {cwd} "
                           f"(exit {result.returncode}): {result.stderr.strip()}")
        return result

    def make_repo(self, path, origin=None, files=None):
        """git init PATH, write FILES ({relpath: text}), set ORIGIN, commit once."""
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, "init", "--quiet")
        for rel, text in (files if files is not None else {"README.md": "fixture\n"}).items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        if origin:
            self.git(path, "remote", "add", "origin", origin)
        self.git(path, "add", "--all")
        self.git(path, "commit", "--quiet", "--allow-empty", "-m", "fixture: initial commit")
        return path.resolve()

    def add_submodule(self, superproject, source, name, origin):
        """Add SOURCE as submodule NAME of SUPERPROJECT, commit, set its origin."""
        self.git(superproject, "-c", "protocol.file.allow=always", "submodule", "--quiet", "add",
                 Path(source).as_uri(), name)
        self.git(superproject, "commit", "--quiet", "-m", f"fixture: add {name} submodule")
        submodule = superproject / name
        self.git(submodule, "remote", "set-url", "origin", origin)
        return submodule

    def make_dotfiles_submodule(self, origin=DOTFILES_ORIGIN):
        """A "dotfiles" repo added as a submodule of a temp superproject.

        Returns (superproject, submodule). The submodule's origin is ORIGIN;
        .gitmodules keeps the local file:// source, so nothing needs network.
        """
        superproject = _fresh(self.fixtures / "superproject")
        source = self.make_repo(_fresh(self.fixtures / "dotfiles-source"),
                                files={"README.md": "dotfiles fixture\n"})
        self.make_repo(superproject, files={"README.md": "superproject fixture\n"})
        return superproject, self.add_submodule(superproject, source, "dotfiles", origin)

    def make_skills_md(self, layout="nested", start_submodule=True, skills=None):
        """A skills-md checkout with origin .../skills-md.git.

        layout="nested" puts each skill at skills/<name>/SKILL.md (skills-md
        origin/main); layout="root" puts it at <name>/SKILL.md. SKILLS maps
        skill names to SKILL.md text. start_submodule adds the nested "start"
        submodule (origin .../skills-md-dev-orchestrator.git).
        """
        if layout not in SKILLS_MD_LAYOUTS:
            raise ValueError(f"layout must be one of {SKILLS_MD_LAYOUTS}, got {layout!r}")
        prefix = "skills/" if layout == "nested" else ""
        files = {"README.md": "skills-md fixture\n"}
        for name, text in (skills if skills is not None else DEFAULT_SKILLS).items():
            files[f"{prefix}{name}/SKILL.md"] = text
        repo = self.make_repo(_fresh(self.fixtures / "skills-md"), origin=SKILLS_MD_ORIGIN, files=files)
        if start_submodule:
            source = self.make_repo(_fresh(self.fixtures / "skills-md-dev-orchestrator"), origin=START_ORIGIN,
                                    files=START_FILES)
            self.add_submodule(repo, source, "start", START_ORIGIN)
        return repo

    def make_file_remote(self, path, source=None):
        """A bare repo at PATH for clone/pull tests; returns its file:// URL.

        SOURCE (a repo path) is cloned bare; without it, the remote holds one
        commit with a README.md.
        """
        path = _fresh(Path(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        if source is None:
            source = self.make_repo(_fresh(path.parent / f"{path.stem}-seed"))
        self.git(path.parent, "clone", "--quiet", "--bare", source, path)
        return path.resolve().as_uri()


def _fresh(path):
    """Return PATH, refusing to reuse one that already exists."""
    if os.path.lexists(path):
        raise FileExistsError(f"fixture path already exists: {path}")
    return path


def _link_tools(directory, names):
    """Symlink each host binary in NAMES that exists into DIRECTORY."""
    for name in names:
        host = os.path.realpath(sys.executable) if name == "python3" else shutil.which(name)
        if host:
            (directory / name).symlink_to(host)


def link_scope(scope_root, **links):
    """Create SCOPE_ROOT and symlink each named subdir to its (created) target.

    Example: link_scope(home / ".config/opencode",
                        plugins=dotfiles / ".config/opencode/plugins")
    """
    scope_root = Path(scope_root)
    scope_root.mkdir(parents=True, exist_ok=True)
    for name, target in links.items():
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        (scope_root / name).symlink_to(target)
    return scope_root


def write_stub_converter(skills_md_dir):
    """Write the stub scripts/opencode-convert-skill.sh into SKILLS_MD_DIR."""
    stub = Path(skills_md_dir) / "scripts" / "opencode-convert-skill.sh"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(CONVERTER_STUB)
    stub.chmod(0o755)
    return stub


def snapshot(root):
    """Map every entry under ROOT, following symlinks, to its content.

    Keys are realpaths. Values are the file sha256, "dir", "other" (fifo,
    socket, device), or "symlink:<target>" for a link itself (keyed by its
    resolved parent plus its own name), so both a link and what it points at
    are covered. Each directory is walked once, so symlink cycles terminate.
    """
    root = Path(root)
    if not os.path.lexists(root):
        raise FileNotFoundError(f"snapshot root does not exist: {root}")
    entries = {}
    _snapshot_entry(root, entries, set())
    return entries


def _snapshot_entry(path, entries, seen_dirs):
    if path.is_symlink():
        entries[os.path.join(os.path.realpath(path.parent), path.name)] = f"symlink:{os.readlink(path)}"
        if not path.exists():
            return
    real = os.path.realpath(path)
    if os.path.isdir(real):
        if real in seen_dirs:
            return
        seen_dirs.add(real)
        entries[real] = "dir"
        for child in sorted(os.listdir(real)):
            _snapshot_entry(Path(real) / child, entries, seen_dirs)
    elif os.path.isfile(real):
        entries[real] = hashlib.sha256(Path(real).read_bytes()).hexdigest()
    else:
        entries[real] = "other"


def _run(argv, sandbox, env, cwd):
    env = sandbox.env() if env is None else env
    program = shutil.which(argv[0], path=env.get("PATH", ""))
    if program is None:
        raise FileNotFoundError(f"{argv[0]} is not on the sandbox PATH: {env.get('PATH', '')}")
    return subprocess.run(
        [program, *map(str, argv[1:])], cwd=cwd or sandbox.root, env=env, input="",
        capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
    )


def run_setup(*args, sandbox, env=None, cwd=None):
    """Run `bash setup-opencode.sh ARGS` in the sandbox, never on a TTY.

    ENV is the complete environment (default sandbox.env()); CWD defaults to
    the sandbox root, which is in no scope and no repo.
    """
    return _run(["bash", SETUP_SCRIPT, *args], sandbox, env, cwd)


def run_convert(*args, sandbox, env=None, cwd=None):
    """Run `python3 scripts/opencode/convert.py ARGS` like run_setup."""
    return _run(["python3", CONVERT_SCRIPT, *args], sandbox, env, cwd)
