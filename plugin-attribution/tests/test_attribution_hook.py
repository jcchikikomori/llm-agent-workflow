#!/usr/bin/env python3
"""Unit + end-to-end tests for the ai-attribution PreToolUse hook.

Run with either:

  python3 -m unittest discover -s plugin-attribution/tests
  python3 -m pytest plugin-attribution/tests

The end-to-end tests invoke the real hook binary in a subprocess with a
temporary HOME, and without the inherited XDG_CONFIG_HOME, so the reviewer-name
files never touch the real ones.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK_DIR = Path(__file__).resolve().parents[1] / "hooks"
HOOK = HOOK_DIR / "attribution_hook.py"
NAME = "Jane Reviewer"
OTHER_NAME = "Someone Else"
POST_TOOL = "mcp__github__create_issue"
IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0

sys.path.insert(0, str(HOOK_DIR))
import attribution_hook as hook  # noqa: E402


def run_hook(tool_name, tool_input, home, xdg_config_home=None):
    """Run the hook binary with a JSON payload; return (exit_code, stderr).

    HOME points at the temp dir. XDG_CONFIG_HOME is dropped from the inherited
    environment so the hook's `~/.config` default lands under that temp HOME;
    pass `xdg_config_home` to point the primary name file somewhere else.
    """
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    env = dict(os.environ, HOME=str(home))
    env.pop("XDG_CONFIG_HOME", None)
    if xdg_config_home is not None:
        env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stderr


def primary_name_file(config_home):
    """The tool-neutral name file under a given config dir."""
    return Path(config_home) / "llm-agent-workflow" / "attribution-name.txt"


def legacy_claude_name_file(home):
    """The pre-2.0 Claude Code name file (read-only fallback)."""
    return Path(home) / ".claude" / "claude-attribution-name.txt"


def legacy_opencode_name_file(home):
    """The pre-2.0 opencode name file (read-only fallback)."""
    return Path(home) / ".config" / "opencode" / "claude-attribution-name.txt"


def write_name(path, name):
    """Write a reviewer-name file, creating parent dirs; return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    return path


def make_home(tmpdir):
    """Create a temp HOME with the reviewer name in the primary file; return the dir."""
    home = Path(tmpdir) / "home"
    write_name(primary_name_file(home / ".config"), NAME)
    return home


def attributed_post(name=NAME, family="Claude"):
    """An MCP post body carrying the attribution line for `family` and `name`."""
    return {
        "title": "Fix flaky test",
        "body": f"Fixes timeout.\n\n🤖 Written by {family}, reviewed by {name}",
    }


class HasAttributionTests(unittest.TestCase):
    def test_attribution_pass(self):
        body = "Fixed the flaky test.\n\n🤖 Written by Claude, reviewed by Jane Reviewer"
        self.assertTrue(hook.has_attribution(body, NAME))

    def test_attribution_pass_without_emoji(self):
        body = "Fixed the flaky test.\n\nWritten by Claude, reviewed by Jane Reviewer"
        self.assertTrue(hook.has_attribution(body, NAME))

    def test_attribution_fail_missing(self):
        self.assertFalse(hook.has_attribution("Just a plain comment.", NAME))

    def test_attribution_fail_wrong_reviewer(self):
        body = "🤖 Written by Claude, reviewed by Someone Else"
        self.assertFalse(hook.has_attribution(body, NAME))

    # --- Family-agnostic validator (ADR-0002): AC-031 positives ---

    def test_attribution_pass_with_other_family(self):
        body = "Fixed the flaky test.\n\n🤖 Written by GPT, reviewed by Jane Reviewer"
        self.assertTrue(hook.has_attribution(body, NAME))

    def test_attribution_pass_with_multi_word_family(self):
        body = "Written by Claude Opus 4, reviewed by Jane Reviewer"
        self.assertTrue(hook.has_attribution(body, NAME))

    def test_attribution_pass_without_comma(self):
        body = "Written by Claude reviewed by Jane Reviewer"
        self.assertTrue(hook.has_attribution(body, NAME))

    def test_attribution_pass_family_of_40_characters(self):
        body = "Written by " + "F" * 40 + ", reviewed by Jane Reviewer"
        self.assertTrue(hook.has_attribution(body, NAME))

    def test_attribution_is_case_insensitive(self):
        body = "WRITTEN BY gpt, REVIEWED BY jane reviewer"
        self.assertTrue(hook.has_attribution(body, NAME))

    # --- Family-agnostic validator (ADR-0002): AC-032 negatives ---

    def test_attribution_fail_empty_family(self):
        body = "Written by , reviewed by Jane Reviewer"
        self.assertFalse(hook.has_attribution(body, NAME))

    def test_attribution_fail_whitespace_only_family(self):
        body = "Written by    , reviewed by Jane Reviewer"
        self.assertFalse(hook.has_attribution(body, NAME))

    def test_attribution_fail_family_over_40_characters(self):
        body = "Written by " + "F" * 41 + ", reviewed by Jane Reviewer"
        self.assertFalse(hook.has_attribution(body, NAME))

    def test_attribution_fail_family_with_comma(self):
        body = "Written by Claude, Inc, reviewed by Jane Reviewer"
        self.assertFalse(hook.has_attribution(body, NAME))

    def test_attribution_fail_split_before_reviewed(self):
        body = "🤖 Written by GPT,\nreviewed by Jane Reviewer"
        self.assertFalse(hook.has_attribution(body, NAME))

    def test_attribution_fail_split_after_written_by(self):
        body = "🤖 Written by\nClaude, reviewed by Jane Reviewer"
        self.assertFalse(hook.has_attribution(body, NAME))

    def test_attribution_fail_family_split_across_lines(self):
        body = "🤖 Written by Cla\nude, reviewed by Jane Reviewer"
        self.assertFalse(hook.has_attribution(body, NAME))


class BlockMessageTests(unittest.TestCase):
    MESSAGES = ("SETUP_MESSAGE", "MISSING_MESSAGE", "SLOP_MESSAGE", "COMMIT_TRAILER_MESSAGE")

    def test_every_block_message_uses_the_ai_attribution_prefix(self):
        for constant in self.MESSAGES:
            with self.subTest(message=constant):
                self.assertTrue(getattr(hook, constant).startswith("[ai-attribution] BLOCKED:"))

    def test_no_block_message_mentions_the_old_plugin_name(self):
        for constant in self.MESSAGES:
            with self.subTest(message=constant):
                self.assertNotIn("claude-attribution", getattr(hook, constant))

    def test_setup_message_names_the_primary_name_file(self):
        self.assertIn("XDG_CONFIG_HOME", hook.SETUP_MESSAGE)
        self.assertIn("llm-agent-workflow/attribution-name.txt", hook.SETUP_MESSAGE)


class NameFileResolutionTests(unittest.TestCase):
    """Candidate order and XDG handling, resolved in-process with a patched environment."""

    def test_candidates_are_primary_then_claude_legacy_then_opencode_legacy(self):
        with mock.patch.dict(os.environ, {"HOME": "/h", "XDG_CONFIG_HOME": "/xdg"}):
            candidates = hook.name_file_candidates()
        self.assertEqual(
            candidates,
            [
                Path("/xdg/llm-agent-workflow/attribution-name.txt"),
                Path("/h/.claude/claude-attribution-name.txt"),
                Path("/h/.config/opencode/claude-attribution-name.txt"),
            ],
        )

    def test_primary_defaults_to_dot_config_when_xdg_config_home_is_unset(self):
        with mock.patch.dict(os.environ, {"HOME": "/h"}):
            os.environ.pop("XDG_CONFIG_HOME", None)
            primary = hook.name_file_candidates()[0]
        self.assertEqual(primary, Path("/h/.config/llm-agent-workflow/attribution-name.txt"))

    def test_primary_defaults_to_dot_config_when_xdg_config_home_is_empty(self):
        with mock.patch.dict(os.environ, {"HOME": "/h", "XDG_CONFIG_HOME": ""}):
            primary = hook.name_file_candidates()[0]
        self.assertEqual(primary, Path("/h/.config/llm-agent-workflow/attribution-name.txt"))


class NativeAttributionTests(unittest.TestCase):
    def test_slack_mcp_is_exempt(self):
        self.assertTrue(hook.has_native_attribution("mcp__slack__postMessage"))

    def test_other_mcp_is_not_exempt(self):
        self.assertFalse(hook.has_native_attribution("mcp__github__create_issue"))


class SlopPhraseTests(unittest.TestCase):
    def test_each_banned_phrase_is_detected(self):
        cases = [
            "Landed this commit and moved on.",
            "This PR includes several changes in this PR.",
            "A comprehensive rewrite of the module.",
            "The robust solution handles all edge cases.",
            "Great job on the refactor.",
            "Thanks for this contribution.",
            "I think maybe we should retry.",
            "You might consider extracting a helper.",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(hook.find_slop_phrases(text), text)

    def test_slop_detection_is_case_insensitive(self):
        self.assertTrue(hook.find_slop_phrases("This is a COMPREHENSIVE change."))

    def test_clean_text_has_no_slop(self):
        text = (
            "The retry loop now backs off exponentially. "
            "Added a regression test for the timeout path."
        )
        self.assertEqual(hook.find_slop_phrases(text), [])


class CommitTrailerTests(unittest.TestCase):
    def test_co_authored_by_ai_is_detected(self):
        messages = ["fix: retry backoff\n\nCo-authored-by: Claude 3.7 Sonnet <noreply@anthropic.com>"]
        self.assertTrue(hook.has_ai_trailer(messages))

    def test_generated_by_ai_is_detected(self):
        self.assertTrue(hook.has_ai_trailer(["feat: parser\n\nGenerated by Claude"]))

    def test_assisted_by_ai_is_detected(self):
        self.assertTrue(hook.has_ai_trailer(["refactor: moved types\n\nAssisted by Copilot"]))

    def test_human_co_author_is_allowed(self):
        messages = ["fix: backoff\n\nCo-authored-by: Jane Reviewer <jane@example.com>"]
        self.assertFalse(hook.has_ai_trailer(messages))

    def test_non_ai_generated_by_is_allowed(self):
        self.assertFalse(hook.has_ai_trailer(["docs: schema\n\nGenerated by the build system"]))

    def test_clean_message_is_allowed(self):
        self.assertFalse(hook.has_ai_trailer(["fix: retry backoff sleeps exponentially"]))

    def test_extract_messages_from_dash_m(self):
        self.assertEqual(
            hook.find_commit_messages('git commit -m "fix: backoff"'),
            ["fix: backoff"],
        )

    def test_extract_messages_from_combined_short_flags(self):
        self.assertEqual(
            hook.find_commit_messages('git commit -am "fix: backoff"'),
            ["fix: backoff"],
        )

    def test_extract_messages_from_multiple_dash_m(self):
        messages = hook.find_commit_messages(
            'git commit -m "fix: backoff" -m "Added regression test"'
        )
        self.assertEqual(messages, ["fix: backoff", "Added regression test"])

    def test_extract_message_files_from_dash_F(self):
        self.assertEqual(
            hook.find_commit_message_files("git commit -F /tmp/msg.txt"),
            ["/tmp/msg.txt"],
        )


class EndToEndTests(unittest.TestCase):
    """Real hook binary, subprocess, temp HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = make_home(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # --- Attribution pass/fail ---

    def test_mcp_post_with_attribution_allowed(self):
        code, _ = run_hook(
            "mcp__github__create_issue",
            {"title": "Fix flaky test", "body": f"Fixes timeout.\n\n🤖 Written by Claude, reviewed by {NAME}"},
            self.home,
        )
        self.assertEqual(code, 0)

    def test_mcp_post_without_attribution_blocked(self):
        code, stderr = run_hook(
            "mcp__github__create_issue",
            {"title": "Fix flaky test", "body": "Fixes timeout."},
            self.home,
        )
        self.assertEqual(code, 2)
        self.assertIn("Attribution line missing", stderr)

    def test_mcp_post_with_other_family_line_allowed(self):
        code, _ = run_hook(POST_TOOL, attributed_post(family="GPT"), self.home)
        self.assertEqual(code, 0)

    def test_bash_post_with_other_family_line_allowed(self):
        code, _ = run_hook(
            "Bash",
            {"command": f'gh pr comment 42 --body "Fixed the timeout. 🤖 Written by GPT, reviewed by {NAME}"'},
            self.home,
        )
        self.assertEqual(code, 0)

    def test_missing_line_message_suggests_the_claude_line(self):
        code, stderr = run_hook(POST_TOOL, {"title": "Fix flaky test", "body": "Fixes timeout."}, self.home)
        self.assertEqual(code, 2)
        self.assertIn("[ai-attribution] BLOCKED: Attribution line missing", stderr)
        self.assertIn(f"🤖 Written by Claude, reviewed by {NAME}", stderr)

    # --- Native-attribution bypass ---

    def test_slack_mcp_bypasses_checks(self):
        code, _ = run_hook(
            "mcp__slack__postMessage",
            {"channel": "#dev", "text": "Comprehensive update, great job everyone."},
            self.home,
        )
        self.assertEqual(code, 0)

    # --- Slop phrase block ---

    def test_mcp_post_with_slop_blocked(self):
        code, stderr = run_hook(
            "mcp__github__create_issue",
            {"title": "Fix flaky test", "body": "This is a comprehensive fix."},
            self.home,
        )
        self.assertEqual(code, 2)
        self.assertIn("AI-slop filler detected", stderr)

    def test_bash_post_with_slop_blocked(self):
        code, _ = run_hook(
            "Bash",
            {"command": f'gh pr comment 42 --body "Great job on this, robust work"'},
            self.home,
        )
        self.assertEqual(code, 2)

    # --- Commit trailer block ---

    def test_commit_dash_m_with_ai_trailer_blocked(self):
        code, stderr = run_hook(
            "Bash",
            {"command": 'git commit -m "fix: backoff\n\nCo-authored-by: Claude <noreply@anthropic.com>"'},
            self.home,
        )
        self.assertEqual(code, 2)
        self.assertIn("AI-generated trailer detected", stderr)

    def test_commit_dash_m_clean_allowed(self):
        code, _ = run_hook(
            "Bash",
            {"command": 'git commit -m "fix: retry backoff sleeps exponentially"'},
            self.home,
        )
        self.assertEqual(code, 0)

    def test_commit_dash_F_with_ai_trailer_blocked(self):
        msg_file = Path(self._tmp.name) / "msg.txt"
        msg_file.write_text("feat: parser\n\nGenerated by Claude")
        code, _ = run_hook(
            "Bash",
            {"command": f"git commit -F {msg_file}"},
            self.home,
        )
        self.assertEqual(code, 2)

    # --- Non-posting Bash untouched ---

    def test_unrelated_bash_command_allowed(self):
        code, _ = run_hook("Bash", {"command": "ls -la"}, self.home)
        self.assertEqual(code, 0)


class NameFileEndToEndTests(unittest.TestCase):
    """Real hook binary; each test places the reviewer name in a different file."""

    def setUp(self):
        # addCleanup (not tearDown) so later-registered cleanups, such as the
        # chmod restore in the unreadable-file test, run before the dir is removed.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.primary = primary_name_file(self.home / ".config")

    # --- AC-031: fallbacks, precedence, XDG ---

    def test_legacy_claude_name_file_and_claude_line_still_pass(self):
        """Backward compatibility: the pre-2.0 Claude Code setup is unchanged for the user."""
        write_name(legacy_claude_name_file(self.home), NAME)
        code, _ = run_hook(POST_TOOL, attributed_post(family="Claude"), self.home)
        self.assertEqual(code, 0)

    def test_name_only_in_legacy_claude_file_is_read(self):
        write_name(legacy_claude_name_file(self.home), NAME)
        code, _ = run_hook(POST_TOOL, attributed_post(family="GPT"), self.home)
        self.assertEqual(code, 0)

    def test_name_only_in_legacy_opencode_file_is_read(self):
        write_name(legacy_opencode_name_file(self.home), NAME)
        code, _ = run_hook(POST_TOOL, attributed_post(family="GPT"), self.home)
        self.assertEqual(code, 0)

    def test_primary_file_wins_when_legacy_files_disagree(self):
        write_name(self.primary, NAME)
        write_name(legacy_claude_name_file(self.home), OTHER_NAME)
        write_name(legacy_opencode_name_file(self.home), OTHER_NAME)
        code, stderr = run_hook(POST_TOOL, attributed_post(name=OTHER_NAME), self.home)
        self.assertEqual(code, 2)
        self.assertIn(f"reviewed by {NAME}", stderr)

    def test_xdg_config_home_moves_the_primary_file(self):
        xdg = Path(self._tmp.name) / "xdg"
        write_name(primary_name_file(xdg), NAME)
        code, _ = run_hook(POST_TOOL, attributed_post(), self.home, xdg_config_home=xdg)
        self.assertEqual(code, 0)

    def test_dot_config_is_not_read_when_xdg_config_home_points_elsewhere(self):
        write_name(self.primary, NAME)
        xdg = Path(self._tmp.name) / "xdg"
        xdg.mkdir()
        code, stderr = run_hook(POST_TOOL, attributed_post(), self.home, xdg_config_home=xdg)
        self.assertEqual(code, 2)
        self.assertIn("Reviewer name not configured", stderr)

    # --- AC-032: not configured, fail closed ---

    def test_no_name_file_blocks_with_setup_message_naming_the_primary_path(self):
        code, stderr = run_hook(POST_TOOL, attributed_post(), self.home)
        self.assertEqual(code, 2)
        self.assertIn("[ai-attribution] BLOCKED: Reviewer name not configured", stderr)
        self.assertIn("llm-agent-workflow/attribution-name.txt", stderr)

    @unittest.skipIf(IS_ROOT, "chmod 000 does not stop root from reading the file")
    def test_unreadable_name_file_blocks_with_setup_message(self):
        path = write_name(self.primary, NAME)
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        code, stderr = run_hook(POST_TOOL, attributed_post(), self.home)
        self.assertEqual(code, 2)
        self.assertIn("Reviewer name not configured", stderr)

    def test_directory_as_name_file_blocks_with_setup_message(self):
        self.primary.mkdir(parents=True)
        code, stderr = run_hook(POST_TOOL, attributed_post(), self.home)
        self.assertEqual(code, 2)
        self.assertIn("Reviewer name not configured", stderr)

    def test_undecodable_name_file_blocks_with_setup_message(self):
        self.primary.parent.mkdir(parents=True)
        self.primary.write_bytes(b"\xff\xfe\xfa")
        code, stderr = run_hook(POST_TOOL, attributed_post(), self.home)
        self.assertEqual(code, 2)
        self.assertIn("Reviewer name not configured", stderr)


if __name__ == "__main__":
    unittest.main()
