#!/usr/bin/env python3
"""SessionStart hook: registration, hub start, mine prompt and conflict scan."""

import json
import os
import subprocess
import sys
import unittest

from support import HOOK, HubTestCase

TOOL = "mcp__plugin_mempalace-docker_mempalace__mempalace_mine"


class SessionStartTests(HubTestCase):
    def run_hook(self, cwd, payload=None, raw=None, **env):
        stdin = raw if raw is not None else json.dumps(payload or {"session_id": "s1"})
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=self.env(**env),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout.strip():
            return ""
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        return out["hookSpecificOutput"]["additionalContext"]

    def stamp(self, root):
        subprocess.run(
            [sys.executable, str(self.plugin_script("mark_mined.py")), "--root", str(root)],
            check=True, capture_output=True, env=self.env(),
        )

    @staticmethod
    def plugin_script(name):
        from support import PLUGIN
        return PLUGIN / "scripts" / name

    # ----------------------------------------------------------- hub + mine

    def test_new_git_project_is_registered_mounted_and_mine_prompted(self):
        app = self.project("Projects", "app", git=True)

        text = self.run_hook(app)

        link = self.registry / "app"
        self.assertEqual(os.path.realpath(link), str(app))
        self.assertIn("Registered this project for the shared hub", text)
        self.assertEqual(len(self.calls_of("run")), 1)
        self.assertIn(f"call {TOOL} with the path `{app}`", text)
        self.assertNotIn("/work", text)

    def test_no_autostart_means_no_docker_and_a_restart_note(self):
        app = self.project("Projects", "app", git=True)

        text = self.run_hook(app, MEMPALACE_HUB_AUTOSTART="0")

        self.assertEqual(self.calls(), [])
        self.assertIn("Do NOT mine it now", text)
        self.assertIn("hub.sh restart", text)

    def test_mined_and_mounted_project_raises_nothing_but_starts_the_hub(self):
        app = self.project("Projects", "app", git=True)
        self.run_hook(app)
        self.stamp(app)
        self.set_container("exited", "", "unhealthy")

        text = self.run_hook(app, payload={"session_id": "s2"})

        self.assertEqual(text, "")
        self.assertEqual(len(self.calls_of("run")), 2)

    def test_running_hub_with_an_older_config_is_reported(self):
        app = self.project("Projects", "app", git=True)
        self.run_hook(app)
        self.stamp(app)
        self.set_container("running", "old", "healthy")

        text = self.run_hook(app, payload={"session_id": "s2"})

        self.assertIn("older config", text)
        self.assertEqual(len(self.calls_of("run")), 1)

    def test_hub_start_failure_is_reported_once(self):
        app = self.project("Projects", "app", git=True)
        text = self.run_hook(app, STUB_RUN_EXIT="125")
        self.assertIn("could not be started", text)
        self.assertIn("hub.sh start", text)

    def test_home_itself_is_never_auto_registered(self):
        text = self.run_hook(self.home)
        self.assertFalse(self.registry.exists() and any(self.registry.iterdir()))
        self.assertIn("not registered for the hub", text)

    def test_non_git_directory_is_not_auto_registered(self):
        plain = self.project("Projects", "plain")
        self.run_hook(plain)
        self.assertFalse(self.registry.exists() and any(self.registry.iterdir()))

    def test_checkout_under_dot_claude_is_not_auto_registered(self):
        inside = self.project(".claude", "plugins", "thing", git=True)
        self.run_hook(inside)
        self.assertFalse(self.registry.exists() and any(self.registry.iterdir()))

    def test_project_under_a_registered_parent_is_not_registered_again(self):
        parent = self.project("Projects")
        self.register(parent)
        app = self.project("Projects", "app", git=True)

        text = self.run_hook(app)

        self.assertEqual(sorted(p.name for p in self.registry.iterdir()), ["Projects"])
        self.assertNotIn("Registered this project", text)
        self.assertIn(f"with the path `{app}`", text)

    def test_garbage_stdin_still_exits_zero(self):
        app = self.project("Projects", "app", git=True)
        self.run_hook(app, raw="not json")

    # -------------------------------------------------------------- conflicts

    def write_conflicts(self):
        settings = self.home / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"mempalace@mempalace": True}}))
        shims = self.home / ".local" / "bin"
        shims.mkdir(parents=True)
        (shims / "mempalace").write_text("#!/bin/sh\n")

    def test_conflicts_are_reported_once_per_session(self):
        self.write_conflicts()
        app = self.project("Projects", "app", git=True)

        first = self.run_hook(app, MEMPALACE_HUB_AUTOSTART="0")
        second = self.run_hook(app, MEMPALACE_HUB_AUTOSTART="0")

        self.assertIn("Official mempalace plugin still enabled", first)
        self.assertIn("Hand-rolled CLI shims still present", first)
        self.assertNotIn("Conflicting mempalace setups", second)

    def test_dismissed_conflicts_stay_silent(self):
        self.write_conflicts()
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "conflicts-dismissed").touch()
        app = self.project("Projects", "app", git=True)

        text = self.run_hook(app, MEMPALACE_HUB_AUTOSTART="0")

        self.assertNotIn("Conflicting mempalace setups", text)

    def test_stale_hooks_and_plain_server_are_reported(self):
        (self.home / ".claude" / "settings.local.json").write_text(
            '{"hooks": {"Stop": [{"command": "~/.local/bin/mempal_save_hook.sh"}]}}'
        )
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"mempalace": {}}}))
        app = self.project("Projects", "app", git=True)

        text = self.run_hook(app, MEMPALACE_HUB_AUTOSTART="0")

        self.assertIn("settings.local.json", text)
        self.assertIn("hand-rolled `mempalace` MCP server", text)


if __name__ == "__main__":
    unittest.main()
