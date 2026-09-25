#!/usr/bin/env python3
# memory-guard runtime env Integration Test - Design Doc: docs/design/opencode-port-design.md (revision 1.5.4)
# Generated: 2026-09-25 | Budget Used: 2/3 integration (memory-guard slice), 0/2 E2E
"""Contract tests for MEMORY_GUARD_RUNTIME (Design Doc decision k; N26, N27; IP-20; AC-053).

`opencode` selects ~/.config/opencode/.memory-guard and watches AGENTS.md where the config lists CLAUDE.md. Unset,
or any other value, keeps the Claude Code defaults (~/.claude/.memory-guard, CLAUDE.md), even when ~/.config/opencode
exists (the N27 bug: the old `.exists()` switch picked the opencode dir under Claude Code too).

Harness:
- Every script runs by subprocess, from this plugin's own hooks/ and scripts/, under the running interpreter.
- Each test builds a fresh env from scratch: a temp HOME, TMPDIR and XDG_CONFIG_HOME as siblings of HOME (so a state
  dir that followed XDG_CONFIG_HOME would land outside HOME), PATH (to find git), LC_ALL=C,
  PYTHONDONTWRITEBYTECODE=1, and git settings: GIT_CONFIG_GLOBAL at a temp file, GIT_CONFIG_NOSYSTEM=1,
  GIT_TERMINAL_PROMPT=0 and an identity through GIT_AUTHOR_*/GIT_COMMITTER_*. Nothing is inherited but PATH, so no
  GIT_* from the caller reaches a script. MEMORY_GUARD_RUNTIME is set only where a test passes a value;
  runtime=None leaves it out.
- Every git repo is a temp repo the test creates; the stashes happen there.

  python3 -m unittest discover -s plugin-memory-guard/tests
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parent
SCRIPTS = PLUGIN_ROOT / "scripts"
SESSION_START_HOOK = PLUGIN_ROOT / "hooks" / "session_start_hook.py"
CONFIG = PLUGIN_ROOT / "config" / "watched-paths.json"

SESSION = "ses_runtime"
TIMEOUT_SECONDS = 60

OPENCODE_STATE = Path(".config") / "opencode" / ".memory-guard"
CLAUDE_STATE = Path(".claude") / ".memory-guard"

NOTHING_DIRTY = "[memory-guard] nothing currently dirty under watched paths -- nothing to do\n"

GITCONFIG = """\
[init]
\tdefaultBranch = main
[commit]
\tgpgsign = false
"""


def session_state_files(state_dir):
    """The two files a session write leaves in STATE_DIR (relative to HOME), with their parent dirs."""
    return state_tree(state_dir, [state_dir / f"session_{SESSION}.json", state_dir / f"session_{SESSION}.lock"])


def state_tree(state_dir, files):
    """STATE_DIR's ancestors below HOME, STATE_DIR itself and FILES, as sorted POSIX strings."""
    dirs = [state_dir, *state_dir.parents][:-1]
    return sorted([*(d.as_posix() for d in dirs), *(f.as_posix() for f in files)])


def project_key(repo):
    """The project-prefs file stem for REPO: the first 16 hex digits of sha256(realpath)."""
    return hashlib.sha256(os.path.realpath(repo).encode()).hexdigest()[:16]


def resolved(action):
    return {"status": "resolved", "action": action}


class Sandbox:
    """A temp root holding home/, tmp/ (TMPDIR, a sibling of HOME), gitconfig and a committed git repo."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-guard-runtime-")
        self.root = Path(self._tmp.name).resolve()
        if REPO_ROOT in self.root.parents:
            self._tmp.cleanup()
            raise RuntimeError(f"sandbox root {self.root} is inside the repo; set TMPDIR outside {REPO_ROOT}")
        self.home = self.root / "home"
        self.tmpdir = self.root / "tmp"
        self.xdg_config = self.root / "xdg-config"
        self.gitconfig = self.root / "gitconfig"
        self.repo = self.root / "repo"
        for directory in (self.home, self.tmpdir, self.xdg_config, self.repo):
            directory.mkdir()
        self.gitconfig.write_text(GITCONFIG)
        (self.repo / "README.md").write_text("fixture\n")
        self.git("init", "-q")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")

    def cleanup(self):
        self._tmp.cleanup()

    def env(self, runtime, **extra):
        """A fresh env; RUNTIME None leaves MEMORY_GUARD_RUNTIME out, anything else sets it to that value."""
        env = {
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "XDG_CONFIG_HOME": str(self.xdg_config),
            "PATH": os.environ.get("PATH", os.defpath),
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Runtime Test",
            "GIT_AUTHOR_EMAIL": "runtime@example.invalid",
            "GIT_COMMITTER_NAME": "Runtime Test",
            "GIT_COMMITTER_EMAIL": "runtime@example.invalid",
            **extra,
        }
        if runtime is not None:
            env["MEMORY_GUARD_RUNTIME"] = runtime
        return env

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], env=self.env(None), capture_output=True,
                                text=True, timeout=TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout

    def commit(self, name, content="committed\n"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git("add", name)
        self.git("commit", "-q", "-m", f"add {name}")

    def dirty(self, name, content="dirty\n"):
        """Makes NAME dirty: overwrites a committed file, or creates an untracked one."""
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def plugin_root(self, config_text):
        """A plugin root (for CLAUDE_PLUGIN_ROOT) whose config/watched-paths.json holds CONFIG_TEXT."""
        root = self.root / "custom-plugin"
        (root / "config").mkdir(parents=True)
        (root / "config" / "watched-paths.json").write_text(config_text)
        return root

    def opencode_dir(self):
        """Creates <HOME>/.config/opencode with an opencode.json, as an opencode install leaves it."""
        config = self.home / ".config" / "opencode"
        config.mkdir(parents=True)
        (config / "opencode.json").write_text("{}\n")

    def run(self, script, *args, runtime, stdin="", **extra):
        return subprocess.run([sys.executable, str(script), *args], env=self.env(runtime, **extra), cwd=self.root,
                              input=stdin, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)

    def apply_action(self, action, runtime, **extra):
        return self.run(SCRIPTS / "apply_action.py", "--repo-root", str(self.repo), "--action", action,
                        "--session-id", SESSION, runtime=runtime, **extra)

    def home_tree(self):
        """Every entry under HOME, relative, as sorted POSIX strings."""
        return sorted(p.relative_to(self.home).as_posix() for p in self.home.rglob("*"))

    def read_json(self, relative):
        return json.loads((self.home / relative).read_text())

    def session_paths(self, state_dir):
        """The session file's paths map, each entry without its timestamp (checked to be a number)."""
        paths = self.read_json(state_dir / f"session_{SESSION}.json")["paths"]
        for entry in paths.values():
            if not isinstance(entry.pop("ts"), float):
                raise AssertionError(f"ts is not a float in {paths}")
        return paths

    def stash_names(self):
        """The file names in each stash entry, newest first."""
        count = len(self.git("stash", "list").splitlines())
        return [self.git("stash", "show", "--name-only", f"stash@{{{index}}}").split() for index in range(count)]

    def pref_file(self, state_dir):
        return state_dir / "project-prefs" / f"{project_key(self.repo)}.json"


class RuntimeEnvTestCase(unittest.TestCase):
    def sandbox(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        return sandbox

    def assertRan(self, result, stdout):
        self.assertEqual((result.returncode, result.stderr, result.stdout), (0, "", stdout))


# Design Doc decision k, N26, IP-20: `opencode` selects the opencode state dir and maps CLAUDE.md to AGENTS.md.
class OpencodeRuntimeTests(RuntimeEnvTestCase):
    # AC-053 (first bullet): "With MEMORY_GUARD_RUNTIME=opencode, apply_action.py treats a dirty AGENTS.md as watched
    #   and writes under ~/.config/opencode/.memory-guard."
    def test_apply_action_stashes_a_dirty_agents_md_and_writes_state_under_the_opencode_dir(self):
        sb = self.sandbox()
        sb.commit("AGENTS.md")
        sb.dirty("AGENTS.md")

        result = sb.apply_action("stash", runtime="opencode")

        self.assertRan(result, "[memory-guard] stashed 1 path(s):\n  - AGENTS.md\n")
        self.assertEqual((sb.repo / "AGENTS.md").read_text(), "committed\n")
        self.assertEqual(sb.git("status", "--porcelain"), "")
        self.assertEqual(sb.stash_names(), [["AGENTS.md"]])
        self.assertEqual(sb.home_tree(), session_state_files(OPENCODE_STATE))
        self.assertEqual(sb.session_paths(OPENCODE_STATE), {"AGENTS.md": resolved("stash")})

    def test_apply_action_removes_an_untracked_agents_md_and_writes_state_under_the_opencode_dir(self):
        sb = self.sandbox()
        sb.dirty("AGENTS.md")

        result = sb.apply_action("remove", runtime="opencode")

        self.assertRan(result, "[memory-guard] removed 1 path(s):\n  - AGENTS.md\n")
        self.assertFalse((sb.repo / "AGENTS.md").exists())
        self.assertEqual(sb.home_tree(), session_state_files(OPENCODE_STATE))
        self.assertEqual(sb.session_paths(OPENCODE_STATE), {"AGENTS.md": resolved("remove")})

    # The mapping replaces the doc name, as the opencode port's watched list does: CLAUDE.md is not watched there.
    def test_apply_action_leaves_a_dirty_claude_md_alone_and_writes_nothing(self):
        sb = self.sandbox()
        sb.commit("CLAUDE.md")
        sb.dirty("CLAUDE.md")

        result = sb.apply_action("stash", runtime="opencode")

        self.assertRan(result, NOTHING_DIRTY)
        self.assertEqual((sb.repo / "CLAUDE.md").read_text(), "dirty\n")
        self.assertEqual(sb.stash_names(), [])
        self.assertEqual(sb.home_tree(), [])


# N27: without the env the Claude Code defaults hold, whatever config dirs exist.
class ClaudeDefaultTests(RuntimeEnvTestCase):
    # AC-053 (second bullet): "With it unset and ~/.config/opencode present, state stays in ~/.claude/.memory-guard."
    def test_unset_env_with_an_opencode_dir_stashes_claude_md_and_writes_state_under_claude_only(self):
        sb = self.sandbox()
        sb.opencode_dir()
        sb.commit("CLAUDE.md")
        sb.dirty("CLAUDE.md")

        result = sb.apply_action("stash", runtime=None)

        self.assertRan(result, "[memory-guard] stashed 1 path(s):\n  - CLAUDE.md\n")
        self.assertEqual(sb.stash_names(), [["CLAUDE.md"]])
        opencode = [".config", ".config/opencode", ".config/opencode/opencode.json"]
        self.assertEqual(sb.home_tree(), sorted(session_state_files(CLAUDE_STATE) + opencode))
        self.assertEqual(sb.session_paths(CLAUDE_STATE), {"CLAUDE.md": resolved("stash")})

    # Task 1.7 negative: AGENTS.md is not watched on Claude Code.
    def test_unset_env_does_not_watch_agents_md_and_writes_nothing(self):
        sb = self.sandbox()
        sb.commit("AGENTS.md")
        sb.dirty("AGENTS.md")

        result = sb.apply_action("stash", runtime=None)

        self.assertRan(result, NOTHING_DIRTY)
        self.assertEqual((sb.repo / "AGENTS.md").read_text(), "dirty\n")
        self.assertEqual(sb.stash_names(), [])
        self.assertEqual(sb.home_tree(), [])

    def test_empty_claude_and_uppercase_values_stash_only_claude_md_and_write_state_under_claude(self):
        for value in ("", "claude", "OPENCODE"):
            with self.subTest(runtime=value):
                sb = self.sandbox()
                sb.opencode_dir()
                sb.commit("CLAUDE.md")
                sb.commit("AGENTS.md")
                sb.dirty("CLAUDE.md")
                sb.dirty("AGENTS.md")

                result = sb.apply_action("stash", runtime=value)

                self.assertRan(result, "[memory-guard] stashed 1 path(s):\n  - CLAUDE.md\n")
                self.assertEqual(sb.stash_names(), [["CLAUDE.md"]])
                self.assertEqual(sb.git("status", "--porcelain"), " M AGENTS.md\n")
                opencode = [".config", ".config/opencode", ".config/opencode/opencode.json"]
                self.assertEqual(sb.home_tree(), sorted(session_state_files(CLAUDE_STATE) + opencode))

    # Decision k: only the exact value `opencode` selects the opencode runtime; nothing is stripped or searched.
    def test_near_miss_values_stash_only_claude_md_and_write_state_under_claude(self):
        for value in (" opencode", "opencode ", "xopencode", "opencode1"):
            with self.subTest(runtime=value):
                sb = self.sandbox()
                sb.commit("CLAUDE.md")
                sb.commit("AGENTS.md")
                sb.dirty("CLAUDE.md")
                sb.dirty("AGENTS.md")

                result = sb.apply_action("stash", runtime=value)

                self.assertRan(result, "[memory-guard] stashed 1 path(s):\n  - CLAUDE.md\n")
                self.assertEqual(sb.home_tree(), session_state_files(CLAUDE_STATE))

    # The N27 bug end to end: the Claude hook marked a path pending under ~/.claude, and apply_action resolved it in
    # the opencode dir instead, so the Claude entry stayed pending. Both must now use one session file.
    def test_session_start_hook_pending_entry_is_resolved_by_apply_action_in_the_same_claude_file(self):
        sb = self.sandbox()
        sb.opencode_dir()
        sb.commit("CLAUDE.md")
        sb.dirty("CLAUDE.md")
        stdin = json.dumps({"session_id": SESSION, "cwd": str(sb.repo)})

        hook = sb.run(SESSION_START_HOOK, runtime=None, stdin=stdin)
        pending = sb.session_paths(CLAUDE_STATE)
        result = sb.apply_action("stash", runtime=None)

        self.assertEqual((hook.returncode, hook.stderr), (0, ""))
        self.assertEqual(hook.stdout.splitlines()[:2], [
            "[memory-guard] Watched files were already dirty before this session started:", "  - CLAUDE.md",
        ])
        self.assertEqual(pending, {"CLAUDE.md": {"status": "pending", "action": None}})
        self.assertRan(result, "[memory-guard] stashed 1 path(s):\n  - CLAUDE.md\n")
        self.assertEqual(sb.session_paths(CLAUDE_STATE), {"CLAUDE.md": resolved("stash")})
        self.assertFalse((sb.home / OPENCODE_STATE).exists())


# Both runtimes share the watched dirs, and the built-in defaults follow the runtime like the config does.
class WatchedPatternTests(RuntimeEnvTestCase):
    def test_watched_dirs_are_stashed_on_both_runtimes_with_state_in_the_runtime_dir(self):
        for runtime, state_dir in (("opencode", OPENCODE_STATE), (None, CLAUDE_STATE)):
            with self.subTest(runtime=runtime):
                sb = self.sandbox()
                sb.dirty(".claude/settings.json")
                sb.dirty("docs/ticket-tracking/T-1.md")

                result = sb.apply_action("stash", runtime=runtime)

                self.assertRan(result, "[memory-guard] stashed 2 path(s):\n  - .claude/settings.json\n"
                                       "  - docs/ticket-tracking/T-1.md\n")
                self.assertEqual(sb.git("status", "--porcelain"), "")
                self.assertEqual(sb.home_tree(), session_state_files(state_dir))

    # CLAUDE_PLUGIN_ROOT at a dir without config/ makes load_watched_patterns fall back to its defaults.
    def test_without_a_config_the_default_doc_name_follows_the_runtime(self):
        for runtime, doc, other in (("opencode", "AGENTS.md", "CLAUDE.md"), (None, "CLAUDE.md", "AGENTS.md")):
            with self.subTest(runtime=runtime):
                sb = self.sandbox()
                no_config = sb.root / "no-config"
                no_config.mkdir()
                sb.dirty("CLAUDE.md")
                sb.dirty("AGENTS.md")

                result = sb.apply_action("remove", runtime=runtime, CLAUDE_PLUGIN_ROOT=str(no_config))

                self.assertRan(result, f"[memory-guard] removed 1 path(s):\n  - {doc}\n")
                self.assertEqual([(sb.repo / doc).exists(), (sb.repo / other).exists()], [False, True])

    # IP-20: like the port, the Python reads the payload config and maps only its CLAUDE.md entry.
    def test_a_custom_config_is_read_and_only_its_claude_md_entry_is_mapped(self):
        config = json.dumps({"watched_dirs": ["notes"], "watched_files": ["CLAUDE.md", "NOTES.md"]})
        for runtime, removed, kept in (
            ("opencode", ["AGENTS.md", "NOTES.md", "notes/n.md"], ["CLAUDE.md", ".claude/x.md"]),
            (None, ["CLAUDE.md", "NOTES.md", "notes/n.md"], ["AGENTS.md", ".claude/x.md"]),
        ):
            with self.subTest(runtime=runtime):
                sb = self.sandbox()
                plugin = sb.plugin_root(config)
                for name in (*removed, *kept):
                    sb.dirty(name)

                result = sb.apply_action("remove", runtime=runtime, CLAUDE_PLUGIN_ROOT=str(plugin))

                listing = "".join(f"  - {name}\n" for name in removed)
                self.assertRan(result, f"[memory-guard] removed 3 path(s):\n{listing}")
                self.assertEqual([name for name in (*removed, *kept) if (sb.repo / name).exists()], kept)

    # Parity with the port: valid JSON that is not an object falls back to the defaults instead of crashing.
    def test_a_config_that_is_not_a_json_object_falls_back_to_the_runtime_defaults(self):
        for config in ("[]", "null", '"x"'):
            for runtime, doc, other in (("opencode", "AGENTS.md", "CLAUDE.md"), (None, "CLAUDE.md", "AGENTS.md")):
                with self.subTest(config=config, runtime=runtime):
                    sb = self.sandbox()
                    plugin = sb.plugin_root(config)
                    sb.dirty("CLAUDE.md")
                    sb.dirty("AGENTS.md")

                    result = sb.apply_action("remove", runtime=runtime, CLAUDE_PLUGIN_ROOT=str(plugin))

                    self.assertRan(result, f"[memory-guard] removed 1 path(s):\n  - {doc}\n")
                    self.assertEqual([(sb.repo / doc).exists(), (sb.repo / other).exists()], [False, True])

    # Scope boundary: the payload stays verbatim (AC-022); only the loaded list is mapped.
    def test_the_config_file_still_lists_claude_md_and_not_agents_md(self):
        text = CONFIG.read_text()

        self.assertEqual(json.loads(text), {"watched_dirs": [".claude", "docs/ticket-tracking"],
                                            "watched_files": ["CLAUDE.md"]})
        self.assertEqual((text.count("CLAUDE.md"), text.count("AGENTS.md")), (1, 0))


# All four scripts honour the common state dir: the positive under `opencode`, and N27 with the env unset.
class OtherScriptsTests(RuntimeEnvTestCase):
    def test_set_preference_writes_the_preference_under_the_runtime_state_dir(self):
        for runtime, state_dir in (("opencode", OPENCODE_STATE), (None, CLAUDE_STATE)):
            with self.subTest(runtime=runtime):
                sb = self.sandbox()
                sb.opencode_dir()

                result = sb.run(SCRIPTS / "set_preference.py", "--repo-root", str(sb.repo), "--action", "stash",
                                runtime=runtime)

                self.assertRan(result, f"[memory-guard] project preference set to 'stash' for {sb.repo}\n")
                opencode = [".config", ".config/opencode", ".config/opencode/opencode.json"]
                expected = state_tree(state_dir, [state_dir / "project-prefs", sb.pref_file(state_dir)])
                self.assertEqual(sb.home_tree(), sorted(set(expected + opencode)))
                preference = sb.read_json(sb.pref_file(state_dir))
                self.assertIsInstance(preference.pop("set_at"), float)
                self.assertEqual(preference, {"repo_root": str(sb.repo), "action": "stash"})

    def test_reset_preference_clears_only_the_preference_under_the_runtime_state_dir(self):
        for runtime, cleared, kept in (("opencode", OPENCODE_STATE, CLAUDE_STATE),
                                       (None, CLAUDE_STATE, OPENCODE_STATE)):
            with self.subTest(runtime=runtime):
                sb = self.sandbox()
                for state_dir in (OPENCODE_STATE, CLAUDE_STATE):
                    (sb.home / sb.pref_file(state_dir)).parent.mkdir(parents=True)
                    (sb.home / sb.pref_file(state_dir)).write_text('{"action": "remove"}')

                result = sb.run(SCRIPTS / "reset_preference.py", "--repo-root", str(sb.repo), runtime=runtime)

                self.assertRan(result, f"[memory-guard] project preference cleared for {sb.repo}\n")
                self.assertEqual([(sb.home / sb.pref_file(cleared)).exists(), (sb.home / sb.pref_file(kept)).exists()],
                                 [False, True])

    def test_mark_resolved_writes_the_session_state_under_the_runtime_state_dir(self):
        for runtime, state_dir in (("opencode", OPENCODE_STATE), (None, CLAUDE_STATE)):
            with self.subTest(runtime=runtime):
                sb = self.sandbox()
                sb.opencode_dir()

                result = sb.run(SCRIPTS / "mark_resolved.py", "--session-id", SESSION, "--path", "AGENTS.md",
                                "--action", "stash", runtime=runtime)

                self.assertRan(result, f"[memory-guard] marked 'AGENTS.md' resolved (stash) for session {SESSION}\n")
                opencode = [".config", ".config/opencode", ".config/opencode/opencode.json"]
                self.assertEqual(sb.home_tree(), sorted(set(session_state_files(state_dir) + opencode)))
                self.assertEqual(sb.session_paths(state_dir), {"AGENTS.md": resolved("stash")})


if __name__ == "__main__":
    unittest.main()
