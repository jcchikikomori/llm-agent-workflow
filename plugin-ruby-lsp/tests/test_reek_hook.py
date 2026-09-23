#!/usr/bin/env python3
"""Unit + end-to-end tests for the ruby-lsp Reek PostToolUse hook.

Run with either:

  python3 -m unittest discover -s plugin-ruby-lsp/tests
  python3 -m pytest plugin-ruby-lsp/tests

End-to-end tests point CLAUDE_PLUGIN_ROOT at a temp dir holding a fake
scripts/run-ruby-tool.sh, so no real reek, bundle or docker is needed.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parents[1] / "hooks"
HOOK = HOOK_DIR / "reek_hook.py"

sys.path.insert(0, str(HOOK_DIR))
import reek_hook as hook  # noqa: E402


def smell(line, smell_type="FeatureEnvy", context="Order#total", message="refers to 'item' more than self"):
    return {"lines": [line], "smell_type": smell_type, "context": context, "message": message}


FAKE_WRAPPER = """#!/usr/bin/env bash
printf '%s\\n' "$*" > "$FAKE_ARGS_LOG"
printf '%s' "$FAKE_STDOUT"
exit "${FAKE_EXIT:-0}"
"""


class Sandbox:
    """Temp Rails-ish project + fake plugin root + temp HOME."""

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.project = self.tmp / "app_root"
        (self.project / "app" / "models").mkdir(parents=True)
        (self.project / "Gemfile").write_text("source 'https://rubygems.org'\n")
        self.model = self.project / "app" / "models" / "order.rb"
        self.model.write_text("class Order; end\n")
        self.plugin = self.tmp / "plugin"
        (self.plugin / "scripts").mkdir(parents=True)
        (self.plugin / "config").mkdir()
        (self.plugin / "config" / ".reek.yml").write_text("---\n")
        wrapper = self.plugin / "scripts" / "run-ruby-tool.sh"
        wrapper.write_text(FAKE_WRAPPER)
        wrapper.chmod(0o755)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.args_log = self.tmp / "args.log"

    def run(self, file_path, stdout="[]", exit_code=0, session_id="s1"):
        payload = json.dumps({"session_id": session_id, "tool_input": {"file_path": str(file_path)}})
        env = dict(
            os.environ,
            HOME=str(self.home),
            CLAUDE_PLUGIN_ROOT=str(self.plugin),
            FAKE_STDOUT=stdout,
            FAKE_EXIT=str(exit_code),
            FAKE_ARGS_LOG=str(self.args_log),
        )
        env.pop("CLAUDE_PROJECT_DIR", None)
        return subprocess.run(
            [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, env=env
        )

    def wrapper_args(self):
        return self.args_log.read_text().strip() if self.args_log.exists() else None


class ShouldCheckTests(unittest.TestCase):
    def test_model_file_checked(self):
        self.assertTrue(hook.should_check("app/models/order.rb"))

    def test_rake_file_checked(self):
        self.assertTrue(hook.should_check("lib/tasks/import.rake"))

    def test_markdown_skipped(self):
        self.assertFalse(hook.should_check("README.md"))

    def test_erb_skipped(self):
        self.assertFalse(hook.should_check("app/views/orders/show.html.erb"))

    def test_schema_skipped(self):
        self.assertFalse(hook.should_check("db/schema.rb"))

    def test_migration_skipped(self):
        self.assertFalse(hook.should_check("db/migrate/20260101000000_add_total.rb"))

    def test_spec_skipped(self):
        self.assertFalse(hook.should_check("spec/models/order_spec.rb"))

    def test_nested_vendor_skipped(self):
        self.assertFalse(hook.should_check("engines/billing/vendor/lib/x.rb"))


class ParseAndFormatTests(unittest.TestCase):
    def test_parse_list(self):
        self.assertEqual(hook.parse_smells('[{"lines": [1]}]'), [{"lines": [1]}])

    def test_parse_empty_stdout(self):
        self.assertEqual(hook.parse_smells(""), [])

    def test_parse_garbage(self):
        self.assertIsNone(hook.parse_smells("not json"))

    def test_parse_non_list(self):
        self.assertIsNone(hook.parse_smells('{"a": 1}'))

    def test_format_single(self):
        text = hook.format_smells([smell(42)], "app/models/order.rb")
        self.assertIn(
            "- app/models/order.rb:42 FeatureEnvy: Order#total refers to 'item' more than self", text
        )
        self.assertIn("found 1 smell(s)", text)

    def test_format_caps_at_max(self):
        text = hook.format_smells([smell(n) for n in range(1, 14)], "a.rb")
        self.assertEqual(text.count("FeatureEnvy"), 10)
        self.assertIn("- (+3 more)", text)
        self.assertIn("found 13 smell(s)", text)

    def test_format_missing_fields(self):
        text = hook.format_smells([{}], "a.rb")
        self.assertIn("- a.rb:? Smell: ", text)


class BuildCommandTests(unittest.TestCase):
    def test_bundled_config_when_project_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = hook.build_command(Path(tmp) / "a.rb", Path(tmp))
        self.assertIn("-c", cmd)

    def test_project_config_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".reek.yml").write_text("---\n")
            cmd = hook.build_command(Path(tmp) / "a.rb", Path(tmp))
        self.assertNotIn("-c", cmd)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_smells_emit_additional_context(self):
        proc = self.box.run(self.box.model, stdout=json.dumps([smell(3)]), exit_code=2)
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("app/models/order.rb:3 FeatureEnvy", out["hookSpecificOutput"]["additionalContext"])

    def test_wrapper_runs_in_project_root_with_json_flags(self):
        self.box.run(self.box.model)
        args = self.box.wrapper_args()
        self.assertTrue(args.startswith("reek --format json --no-progress -c "))
        self.assertTrue(args.endswith(str(self.box.model.resolve())))

    def test_clean_file_prints_nothing(self):
        proc = self.box.run(self.box.model, stdout="[]", exit_code=0)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_non_ruby_file_never_runs_reek(self):
        readme = self.box.project / "README.md"
        readme.write_text("# hi\n")
        proc = self.box.run(readme)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIsNone(self.box.wrapper_args())

    def test_missing_file_never_runs_reek(self):
        proc = self.box.run(self.box.project / "app" / "models" / "gone.rb")
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(self.box.wrapper_args())

    def test_reek_missing_hints_once_per_session(self):
        first = self.box.run(self.box.model, exit_code=127)
        second = self.box.run(self.box.model, exit_code=127)
        self.assertEqual((first.returncode, first.stdout), (0, ""))
        self.assertIn("reek not installed", first.stderr)
        self.assertNotIn("reek not installed", second.stderr)

    def test_reek_error_fails_open(self):
        proc = self.box.run(self.box.model, stdout="boom", exit_code=1)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIn("reek exit 1", proc.stderr)

    def test_unparseable_output_fails_open(self):
        proc = self.box.run(self.box.model, stdout="not json", exit_code=2)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIn("unparseable", proc.stderr)

    def test_bad_stdin_fails_open(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input="{nope", capture_output=True, text=True
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_no_file_path_is_noop(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input='{"tool_input": {}}', capture_output=True, text=True
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
