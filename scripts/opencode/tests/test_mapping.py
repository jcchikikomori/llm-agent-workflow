#!/usr/bin/env python3
"""Unit and CLI tests for the mapping.json loader and `convert.py list`.

The loader positives import mapping.py in-process; `list` and every negative run the real CLI in a subprocess.
Negatives write a mutated copy of mapping.json into a temp fixture repo at scripts/opencode/mapping.json, the path
`list --repo DIR` reads.

  python3 -m unittest discover -s scripts/opencode/tests
"""

import json
import sys
import unittest

from _support import REPO_ROOT, Sandbox, run_convert

SCRIPTS_DIR = REPO_ROOT / "scripts" / "opencode"
MAPPING_JSON = SCRIPTS_DIR / "mapping.json"

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2

# Every plugin.json id except opencode-migrate (exclude_plugins). ai-attribution is claude-attribution until 3.1.
PORT_TARGETS = {
    "claude-attribution", "commit-guard", "env-guard", "markdown-format", "memory-guard", "token-saver",
    "wandavision", "gh-issue-to-pr", "ruby-lsp", "markdown-lsp", "mempalace-docker", "dev", "qa",
}
KINDS = ("payload", "plugins", "skills", "agents", "commands", "config")
PAYLOAD_IDS = {"commit-guard", "memory-guard", "markdown-format", "token-saver", "ruby-lsp", "markdown-lsp",
               "mempalace-docker", "qa"}


def import_mapping():
    """Import scripts/opencode/mapping.py without writing __pycache__/ into the repo."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import mapping
    finally:
        sys.path.remove(str(SCRIPTS_DIR))
        sys.dont_write_bytecode = previous
    return mapping


class MappingLoaderTests(unittest.TestCase):
    def test_the_repo_mapping_json_loads_with_schema_version_2(self):
        mapping = import_mapping()

        data = mapping.load(MAPPING_JSON)

        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["payload_namespace"], "llm-agent-workflow")
        self.assertEqual(data["plugin_id_source"], "plugin.json#name")
        self.assertEqual(data["exclude_plugins"], ["opencode-migrate"])
        self.assertEqual(data["exclude_agents"], ["plugin-env-guard/agents/secret-exposure-auditor.md"])
        self.assertEqual(data["hand_written_agent_prefix"], "opencode-")

    def test_the_repo_mapping_json_fills_the_eight_payloads_with_markers_and_the_guard(self):
        mapping = import_mapping()

        data = mapping.load(MAPPING_JSON)

        self.assertEqual(set(data["payloads"]), PAYLOAD_IDS)
        self.assertEqual(data["payloads"]["mempalace-docker"],
                         {"include": ["hooks", "scripts"], "exclude": ["hooks/vendor"],
                          "marker": "scripts/mark_mined.py"})
        self.assertEqual(data["payloads"]["qa"]["marker"], "skills/coverage-quality/hooks/post-test-coverage.sh")
        self.assertEqual(data["guard"], {
            "blocked_origin_repo_names": ["skills-md"], "blocked_scope_dirs": ["skills", "skill"],
            "writable_scope_dirs": ["plugins", "agents", "commands", "llm-agent-workflow"], "submodules": "approval",
        })
        self.assertEqual(data["extra_sources"]["wandavision"]["manifest"],
                         "plugin-wandavision/.claude-plugin/plugin.json")

    def test_every_marker_in_the_repo_mapping_names_an_existing_file(self):
        mapping = import_mapping()
        data = mapping.load(MAPPING_JSON)
        plugin_dirs = {json.loads(path.read_text())["name"]: path.parents[1]
                       for path in REPO_ROOT.glob("plugin-*/.claude-plugin/plugin.json")}

        missing = sorted(plugin_id for plugin_id, payload in data["payloads"].items()
                         if not (plugin_dirs[plugin_id] / payload["marker"]).is_file())

        self.assertEqual(missing, [])


class ListTests(unittest.TestCase):
    """`convert.py list --repo <this checkout>`: one `id|kind|relpath` row per install source."""

    @classmethod
    def setUpClass(cls):
        sandbox = Sandbox()
        cls.addClassCleanup(sandbox.cleanup)
        cls.result = run_convert("list", "--repo", REPO_ROOT, sandbox=sandbox)
        cls.rows = [line.split("|") for line in cls.result.stdout.splitlines()]

    def rows_of_kind(self, kind):
        return [row for row in self.rows if row[1] == kind]

    def test_list_exits_0_and_prints_nothing_on_stderr(self):
        self.assertEqual(self.result.returncode, EXIT_OK, self.result.stderr)
        self.assertEqual(self.result.stderr, "")

    def test_every_row_has_three_fields_and_a_known_kind(self):
        self.assertTrue(self.rows)
        self.assertEqual([row for row in self.rows if len(row) != 3], [])
        self.assertEqual(sorted({row[1] for row in self.rows} - set(KINDS)), [])

    def test_ids_are_the_13_port_targets_from_plugin_json(self):
        self.assertEqual({row[0] for row in self.rows}, PORT_TARGETS)

    def test_rows_are_sorted_by_id_then_kind_in_unit_order_then_relpath(self):
        expected = sorted(self.rows, key=lambda row: (row[0], KINDS.index(row[1]), row[2]))

        self.assertEqual(self.rows, expected)
        self.assertEqual(len(self.rows), len({tuple(row) for row in self.rows}))

    def test_wandavision_ts_and_skill_come_through_extra_sources(self):
        self.assertIn(["wandavision", "plugins", "wandavision/opencode-plugin/opencode-wandavision.ts"], self.rows)
        self.assertIn(["wandavision", "skills", "wandavision/skill/wandavision"], self.rows)

    def test_no_row_names_opencode_migrate(self):
        self.assertEqual([row for row in self.rows if any("opencode-migrate" in field for field in row)], [])

    def test_the_excluded_secret_exposure_auditor_is_not_listed(self):
        self.assertEqual([row for row in self.rows if "secret-exposure-auditor" in row[2]], [])

    def test_the_hand_written_gh_agent_is_listed_and_its_claude_twin_is_not(self):
        agents = [row[2] for row in self.rows_of_kind("agents") if row[0] == "gh-issue-to-pr"]

        self.assertEqual(agents, ["plugin-gh-issue-to-pr/agents/opencode-gh-issue-to-pr.md"])

    def test_plugin_dir_skills_are_listed_under_their_plugin_id(self):
        self.assertIn(["dev", "skills", "plugin-dev/skills/coding-principles"], self.rows)
        self.assertIn(["qa", "skills", "plugin-qa/skills/coverage-quality"], self.rows)

    def test_claude_format_agents_are_listed_under_their_plugin_id(self):
        self.assertIn(["dev", "agents", "plugin-dev/agents/task-executor.md"], self.rows)
        self.assertIn(["qa", "agents", "plugin-qa/agents/web-qa-reviewer.md"], self.rows)

    def test_payload_rows_are_one_per_include_entry(self):
        self.assertEqual(self.rows_of_kind("payload"), [
            ["commit-guard", "payload", "plugin-commit-guard/hooks"],
            ["markdown-format", "payload", "plugin-markdown-format/config"],
            ["markdown-lsp", "payload", "plugin-markdown-lsp/config"],
            ["markdown-lsp", "payload", "plugin-markdown-lsp/scripts"],
            ["memory-guard", "payload", "plugin-memory-guard/config"],
            ["memory-guard", "payload", "plugin-memory-guard/hooks"],
            ["memory-guard", "payload", "plugin-memory-guard/scripts"],
            ["mempalace-docker", "payload", "plugin-mempalace-docker/hooks"],
            ["mempalace-docker", "payload", "plugin-mempalace-docker/scripts"],
            ["qa", "payload", "plugin-qa/skills/coverage-quality/hooks"],
            ["qa", "payload", "plugin-qa/skills/coverage-quality/scripts"],
            ["ruby-lsp", "payload", "plugin-ruby-lsp/config"],
            ["ruby-lsp", "payload", "plugin-ruby-lsp/hooks"],
            ["ruby-lsp", "payload", "plugin-ruby-lsp/scripts"],
            ["token-saver", "payload", "plugin-token-saver/config"],
        ])

    def test_config_rows_come_only_from_payload_plugins(self):
        self.assertEqual(self.rows_of_kind("config"), [
            ["markdown-lsp", "config", "plugin-markdown-lsp/.lsp.json"],
            ["mempalace-docker", "config", "plugin-mempalace-docker/.mcp.json"],
            ["ruby-lsp", "config", "plugin-ruby-lsp/.lsp.json"],
        ])

    def test_commands_and_ts_plugins_come_from_the_plugin_dirs(self):
        self.assertIn(["claude-attribution", "commands", "plugin-attribution/commands/claude-attribution.md"],
                      self.rows)
        self.assertIn(["claude-attribution", "plugins", "plugin-attribution/plugins/opencode-claude-attribution.ts"],
                      self.rows)


class MappingValidationTests(unittest.TestCase):
    """A mutated copy of mapping.json in a temp fixture repo makes `list` exit 1 and name the rule."""

    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.repo = self.sandbox.root / "repo"
        self.mapping_path = self.repo / "scripts" / "opencode" / "mapping.json"
        self.mapping_path.parent.mkdir(parents=True)

    def list_with(self, text):
        self.mapping_path.write_text(text)
        return run_convert("list", "--repo", self.repo, sandbox=self.sandbox)

    def list_with_data(self, mutate):
        data = json.loads(MAPPING_JSON.read_text())
        mutate(data)
        return self.list_with(json.dumps(data))

    def assert_fatal(self, result, rule, *fragments):
        self.assertEqual(result.returncode, EXIT_FATAL, result.stderr)
        self.assertEqual(result.stdout, "")
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertTrue(lines[0].startswith(f"ERROR mapping.json: {rule}: "), lines[0])
        for fragment in fragments:
            self.assertIn(fragment, lines[0])

    def test_an_unknown_top_level_key_is_fatal(self):
        result = self.list_with_data(lambda data: data.update(surprise=True))

        self.assert_fatal(result, "unknown-key", "/surprise")

    def test_payloads_as_a_list_is_fatal(self):
        result = self.list_with_data(lambda data: data.update(payloads=list(data["payloads"].values())))

        self.assert_fatal(result, "wrong-type", "/payloads", "object", "array")

    def test_a_paths_entry_with_preserve_and_to_is_fatal(self):
        entry = {"id": "claude-projects-mount", "from": "~/.claude/projects", "match": "prefix", "preserve": True,
                 "to": "{memory_root}/"}

        result = self.list_with_data(lambda data: data.update(paths=[entry]))

        self.assert_fatal(result, "paths-preserve-and-to", "/paths/0")

    def test_a_paths_entry_with_neither_preserve_nor_to_is_fatal(self):
        entry = {"id": "claude-home", "from": "~/.claude/"}

        result = self.list_with_data(lambda data: data.update(paths=[entry]))

        self.assert_fatal(result, "paths-preserve-or-to", "/paths/0")

    def test_valid_paths_entries_from_the_design_doc_pass_validation(self):
        mapping = import_mapping()
        data = json.loads(MAPPING_JSON.read_text())
        data["paths"] = [
            {"id": "claude-projects-mount", "from": "~/.claude/projects", "match": "prefix", "preserve": True},
            {"id": "plugin-root", "from": ["${CLAUDE_PLUGIN_ROOT}", "$CLAUDE_PLUGIN_ROOT"], "to": "{payload_dir}"},
        ]

        self.assertIsNone(mapping.validate(data))

    def test_a_missing_schema_version_is_fatal(self):
        result = self.list_with_data(lambda data: data.pop("schema_version"))

        self.assert_fatal(result, "missing-key", "/schema_version")

    def test_a_schema_version_other_than_2_is_fatal(self):
        result = self.list_with_data(lambda data: data.update(schema_version=3))

        self.assert_fatal(result, "wrong-value", "/schema_version", "3")

    def test_a_boolean_is_not_accepted_as_an_integer(self):
        result = self.list_with_data(lambda data: data.update(schema_version=True))

        self.assert_fatal(result, "wrong-type", "/schema_version", "integer", "boolean")

    def test_a_nested_wrong_type_names_its_pointer(self):
        result = self.list_with_data(lambda data: data["guard"].update(blocked_origin_repo_names="skills-md"))

        self.assert_fatal(result, "wrong-type", "/guard/blocked_origin_repo_names", "array", "string")

    def test_an_unknown_nested_key_is_fatal(self):
        result = self.list_with_data(lambda data: data["payloads"]["qa"].update(markers=[]))

        self.assert_fatal(result, "unknown-key", "/payloads/qa/markers")

    def test_an_absolute_or_parent_relative_source_path_is_fatal(self):
        for bad in ("/etc/passwd", "../outside/hooks"):
            with self.subTest(bad):
                result = self.list_with_data(lambda data: data["payloads"]["qa"].update(include=[bad]))

                self.assert_fatal(result, "relative-path", "/payloads/qa/include/0")

    def test_a_snippets_override_object_passes_validation(self):
        mapping = import_mapping()
        data = json.loads(MAPPING_JSON.read_text())
        data["snippets"] = {"mempalace-docker": {"timeout": 60000}}

        self.assertIsNone(mapping.validate(data))

    def test_snippets_as_an_array_is_fatal(self):
        result = self.list_with_data(lambda data: data.update(snippets=[]))

        self.assert_fatal(result, "wrong-type", "/snippets")

    def test_invalid_json_is_fatal_with_its_line(self):
        result = self.list_with('{\n  "schema_version": 2\n  "payload_namespace": "x"\n}\n')

        self.assert_fatal(result, "invalid-json", "line 3")

    def test_a_duplicate_key_is_fatal(self):
        result = self.list_with('{"schema_version": 2, "schema_version": 2}')

        self.assert_fatal(result, "duplicate-key", "schema_version")

    def test_a_repo_without_mapping_json_is_fatal(self):
        result = run_convert("list", "--repo", self.sandbox.root / "no-such-repo", sandbox=self.sandbox)

        self.assert_fatal(result, "unreadable")


class CliUsageTests(unittest.TestCase):
    """argparse usage errors exit 2 with usage on stderr and nothing on stdout."""

    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def test_usage_errors_exit_2(self):
        cases = {
            "no subcommand": (),
            "unknown subcommand": ("frobnicate",),
            "list without --repo": ("list",),
            "guard without --op": ("guard", "--scope-root", self.sandbox.home, "x"),
            "guard with an unknown --op": ("guard", "--scope-root", self.sandbox.home, "--op", "move", "x"),
            "guard without a PATH": ("guard", "--scope-root", self.sandbox.home, "--op", "write"),
        }
        for name, args in cases.items():
            with self.subTest(name):
                result = run_convert(*args, sandbox=self.sandbox)

                self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
