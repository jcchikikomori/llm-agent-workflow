from __future__ import annotations

import unittest

from fixtures import SandboxTestCase

import rg_common as common
import rg_gate as gate


class SplitTests(unittest.TestCase):
    def test_should_split_compound_commands(self):
        self.assertEqual(gate.split_segments("a && b || c; d | e\nf"), ["a", "b", "c", "d", "e", "f"])
        self.assertEqual(gate.split_segments(""), [])


class ClassifyTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = common.load_config()

    def kind(self, tool, **tool_input):
        return gate.classify_tool(tool, tool_input, self.cfg)

    def test_should_treat_spawns_as_heavy(self):
        for tool in ("Agent", "Task", "Workflow", "Monitor"):
            self.assertEqual(self.kind(tool), "heavy", tool)

    def test_should_pass_other_tools(self):
        self.assertEqual(self.kind("Read", file_path="/x"), "light")
        self.assertEqual(gate.classify_tool("Bash", "not a dict", self.cfg), "light")

    def test_should_detect_heavy_shell_commands(self):
        heavy = [
            "docker compose run --rm app bundle exec rspec",
            "cd x && docker build -t y .",
            "bundle exec rspec spec/models",
            "npm run test",
            "python3 -m unittest discover -s tests",
            "make -j8",
            "go test ./...",
        ]
        for command in heavy:
            self.assertEqual(self.kind("Bash", command=command), "heavy", command)

    def test_should_detect_relief_commands(self):
        for command in ("docker stop abc", "docker compose down", "docker compose -f a.yml -p x stop app",
                        "pkill -f rspec", "kill -9 123",
                        "~/.claude/.resource-guard/bin/resource-guard resume --all",
                        'python3 "/p/scripts/resource_guard.py" freeze --others',
                        "resource-guard status"):
            self.assertEqual(self.kind("Bash", command=command), "relief", command)

    def test_should_not_treat_plugin_paths_as_relief(self):
        self.assertEqual(self.kind("Bash", command="python3 -m unittest discover -s plugin-resource-guard/tests"),
                         "heavy")
        self.assertEqual(self.kind("Bash", command="cat plugin-resource-guard/README.md"), "light")

    def test_should_detect_heavy_commands_behind_wrappers_and_flags(self):
        for command in ("docker --context remote run -d img", "docker compose -f a.yml -p x up -d",
                        "docker exec -it app bundle exec rspec spec/a_spec.rb", "RAILS_ENV=test bundle exec rspec",
                        "bin/rails test", "uv run pytest", "./vendor/bin/phpunit", "timeout 600 make",
                        "python3 -m pip install x", "docker build -t resource-guard ."):
            self.assertEqual(self.kind("Bash", command=command), "heavy", command)

    def test_should_not_read_mentions_as_heavy(self):
        for command in ("docker logs plus_app-web-run-3f2a1b", "docker exec app cat /tmp/up", "grep -rn pytest spec/",
                        "cat pytest.ini", "git commit -m 'make it work'", "echo 'npm install later'"):
            self.assertEqual(self.kind("Bash", command=command), "light", command)

    def test_should_let_heavy_win_over_relief_in_one_command(self):
        self.assertEqual(self.kind("Bash", command="docker stop a && docker run b"), "heavy")

    def test_should_not_read_rm_flag_as_relief(self):
        self.assertEqual(self.kind("Bash", command="docker compose run --rm app rspec"), "heavy")

    def test_should_treat_background_commands_as_heavy_unless_relief(self):
        self.assertEqual(self.kind("Bash", command="tail -f log", run_in_background=True), "heavy")
        self.assertEqual(self.kind("Bash", command="docker stop a", run_in_background=True), "relief")

    def test_should_pass_light_commands(self):
        self.assertEqual(self.kind("Bash", command="git status && ls -la"), "light")

    def test_should_skip_invalid_user_patterns(self):
        cfg = dict(self.cfg, heavy_patterns=["(", "\\bfoo\\b"])
        self.assertEqual(gate.classify_tool("Bash", {"command": "foo"}, cfg), "heavy")


class DecideTests(unittest.TestCase):
    def test_decision_matrix(self):
        cases = {
            ("ok", False, "heavy"): "pass",
            ("elevated", False, "heavy"): "soft_wait",
            ("elevated", True, "heavy"): "pass",
            ("critical", False, "heavy"): "wait_deny",
            ("critical", True, "heavy"): "ask",
            ("hard", False, "heavy"): "wait_deny",
            ("hard", True, "heavy"): "ask",
            ("hard", False, "relief"): "pass",
            ("hard", False, "light"): "pass",
        }
        for (level, fg, kind), expected in cases.items():
            self.assertEqual(gate.decide(level, fg, kind), expected, (level, fg, kind))


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def test_should_release_early_when_load_drops(self):
        levels = iter(["critical", "critical", "elevated"])
        result = gate.wait_for_calm(lambda: next(levels), 20, 2, "critical", self.sleep, self.clock)
        self.assertEqual(result, "elevated")
        self.assertEqual(self.sleeps, [2, 2])

    def test_should_give_up_at_deadline(self):
        result = gate.wait_for_calm(lambda: "hard", 5, 2, "critical", self.sleep, self.clock)
        self.assertEqual(result, "hard")
        self.assertEqual(self.sleeps, [2, 2, 1])

    def test_should_not_wait_when_already_calm(self):
        self.assertEqual(gate.wait_for_calm(lambda: "ok", 20, 2, "elevated", self.sleep, self.clock), "ok")
        self.assertEqual(self.sleeps, [])


class OutputTests(SandboxTestCase):
    def test_should_deny_with_reason_and_user_message(self):
        out = gate.deny_output("Bash", "critical", ["mem_available_pct=8.0"])
        specific = out["hookSpecificOutput"]
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertIn("critical: mem_available_pct=8.0", specific["permissionDecisionReason"])
        self.assertEqual(out["systemMessage"], "resource-guard held back Bash in a background session "
                                               "(critical: mem_available_pct=8.0)")

    def test_should_ask_in_foreground(self):
        out = gate.ask_output("Agent", "hard", [])
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("machine load is hard.", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_should_never_emit_allow(self):
        outputs = [gate.deny_output("Bash", "hard", []), gate.ask_output("Bash", "hard", [])]
        self.assertNotIn("allow", [o["hookSpecificOutput"]["permissionDecision"] for o in outputs])

    def test_should_stop_turn_after_deny_burst(self):
        out = gate.stop_output(3, "critical", ["x=1"])
        self.assertEqual(out["continue"], False)
        self.assertIn("after 3 held-back calls", out["stopReason"])

    def test_should_count_denies_inside_window(self):
        self.assertEqual(gate.record_deny("1-2", now=100.0, window=300), 1)
        self.assertEqual(gate.record_deny("1-2", now=200.0, window=300), 2)
        self.assertEqual(gate.record_deny("1-2", now=450.0, window=300), 2)


if __name__ == "__main__":
    unittest.main()
