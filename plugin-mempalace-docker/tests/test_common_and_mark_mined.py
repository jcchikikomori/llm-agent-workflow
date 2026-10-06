#!/usr/bin/env python3
"""mempalace_docker_common helpers (in-process) and scripts/mark_mined.py."""

import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from support import MARK_MINED, PLUGIN, HubTestCase

sys.path.insert(0, str(PLUGIN / "hooks"))

VENDOR_SHA256 = {
    "hooks/vendor/mempal_precompact_hook.sh": "c25d64259c3072c9d79090f3e0a9f6590fafb8870081e77d68a4fbcacab0157f",
    "hooks/vendor/mempal_save_hook.sh": "e1d91a87109eac41c524c5480fda6873d7dd8f043f7e3105cfff3948a0057e89",
    "hooks/vendor/mempal_session_end_hook.sh": "46387399ff1bc63badf6deae8fff7714008510735bdd36494788d164f5d8bb70",
}


class CommonTests(HubTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, self.env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        import mempalace_docker_common

        self.common = importlib.reload(mempalace_docker_common)

    def test_paths_follow_the_environment(self):
        self.assertEqual(self.common.STATE_ROOT, self.state)
        self.assertEqual(self.common.REGISTRY_DIR, self.registry)
        with mock.patch.dict(os.environ, {"MEMPALACE_HUB_PROJECTS_DIR": str(self.tmp / "reg")}):
            self.assertEqual(importlib.reload(self.common).REGISTRY_DIR, self.tmp / "reg")

    def test_dedupe_nested_checks_every_kept_parent(self):
        paths = [Path("/w/b/c"), Path("/w/b-x"), Path("/w/b"), Path("/w/b")]
        self.assertEqual(self.common._dedupe_nested(paths), [Path("/w/b"), Path("/w/b-x")])

    def test_covering_target(self):
        app = self.project("Projects", "app")
        self.assertEqual(self.common.covering_target(app, [app.parent]), app.parent)
        self.assertIsNone(self.common.covering_target(app, [self.home / "Proj"]))

    def test_registry_targets_skip_dangling(self):
        app = self.project("Projects", "app")
        self.register(app)
        self.register(self.home / "gone")
        self.assertEqual(self.common.registry_targets(), [app])

    def test_registry_targets_without_a_registry(self):
        self.assertEqual(self.common.registry_targets(), [])

    def test_register_project_and_clash_suffix(self):
        a = self.project("a", "app")
        b = self.project("b", "app")
        link_a, created_a = self.common.register_project(a)
        link_b, created_b = self.common.register_project(b)
        again, created_again = self.common.register_project(a)
        self.assertTrue(created_a and created_b)
        self.assertFalse(created_again)
        self.assertEqual(link_a.name, "app")
        self.assertEqual(link_b.name, "app-" + hashlib.sha256(str(b).encode()).hexdigest()[:8])
        self.assertEqual(again, a)

    def test_auto_register_rules(self):
        allowed = self.project("Projects", "app", git=True)
        self.assertTrue(self.common.auto_register_allowed(allowed))
        self.assertFalse(self.common.auto_register_allowed(self.home))
        self.assertFalse(self.common.auto_register_allowed(self.project("Projects", "plain")))
        self.assertFalse(self.common.auto_register_allowed(self.tmp))

    def test_hub_state_tolerates_missing_and_bad_files(self):
        self.assertIsNone(self.common.hub_state())
        self.assertEqual(self.common.hub_mounted_targets(), [])
        (self.state / "hub").mkdir(parents=True)
        (self.state / "hub" / "state.json").write_text("[1, 2]")
        self.assertIsNone(self.common.hub_state())
        (self.state / "hub" / "state.json").write_text('{"mounted_targets": ["/a", 3, ""]}')
        self.assertEqual(self.common.hub_mounted_targets(), [Path("/a")])

    def test_autostart_switch(self):
        self.assertTrue(self.common.hub_autostart_enabled())
        for off in ("0", "false", "OFF", "no"):
            with mock.patch.dict(os.environ, {"MEMPALACE_HUB_AUTOSTART": off}):
                self.assertFalse(self.common.hub_autostart_enabled())

    def test_ensure_hub_reports_a_missing_script(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": str(self.tmp / "nowhere")}):
            ok, err = self.common.ensure_hub()
        self.assertFalse(ok)

    def test_ensure_hub_survives_a_timeout(self):
        with mock.patch.object(self.common.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("hub.sh", 1)):
            ok, err = self.common.ensure_hub()
        self.assertFalse(ok)
        self.assertIn("timed out", err)

    def test_mine_reason_states(self):
        app = self.project("Projects", "app", git=True)
        self.assertEqual(self.common.mine_reason(app), "never mined")
        self.common.write_stamp(app, sha="a" * 40)
        with mock.patch.object(self.common, "head_sha", return_value="b" * 40):
            self.assertEqual(self.common.mine_reason(app), "HEAD moved (aaaaaaaa -> bbbbbbbb)")
        self.common.write_stamp(app, sha=None)
        self.assertIsNone(self.common.mine_reason(app))
        stamp = self.common.stamp_path(app)
        data = json.loads(stamp.read_text())
        data["last_mined"] = time.time() - 30 * 86400
        stamp.write_text(json.dumps(data))
        self.assertIn("days ago", self.common.mine_reason(app))
        data["last_mined"] = "x"
        stamp.write_text(json.dumps(data))
        self.assertEqual(self.common.mine_reason(app), "stamp unreadable")

    def test_max_age_days(self):
        self.assertEqual(self.common.max_age_days(), 7)
        for raw, expected in (("2.5", 2.5), ("junk", 7)):
            with mock.patch.dict(os.environ, {"MEMPALACE_MINE_MAX_AGE_DAYS": raw}):
                self.assertEqual(self.common.max_age_days(), expected)

    def test_sessions_mark_and_gc(self):
        self.assertFalse(self.common.session_marked(""))
        self.common.mark_session("")
        self.common.mark_session("abc")
        self.assertTrue(self.common.session_marked("abc"))
        marker = self.common.SESSIONS_DIR / "abc"
        old = time.time() - 31 * 86400
        os.utime(marker, (old, old))
        self.common.gc_sessions()
        self.assertFalse(marker.exists())

    def test_mine_report_shapes(self):
        app = self.project("Projects", "app", git=True)
        self.assertIn("not registered for the hub", self.common.mine_report(root=app))
        self.register(app)
        self.assertIn("registered for the hub (via", self.common.mine_report(root=app))
        (self.state / "hub").mkdir(parents=True)
        (self.state / "hub" / "state.json").write_text(json.dumps({"mounted_targets": [str(app)]}))
        text = self.common.mine_report(tool_name="custom_mine", root=app)
        self.assertIn(f"call custom_mine with the path `{app}`", text)
        self.common.write_stamp(app)
        self.assertEqual(self.common.mine_report(root=app), "")


class MarkMinedTests(HubTestCase):
    def mark(self, *args):
        return subprocess.run(
            [sys.executable, str(MARK_MINED), *map(str, args)],
            capture_output=True, text=True, env=self.env(), timeout=30,
        )

    def test_stamp_then_show(self):
        app = self.project("Projects", "app", git=True)
        missing = self.mark("--root", app, "--show")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no stamp", missing.stdout)
        self.assertEqual(self.mark("--root", app).returncode, 0)
        shown = self.mark("--root", app, "--show")
        self.assertEqual(shown.returncode, 0)
        self.assertIn("status: up to date", shown.stdout)

    def test_report_never_writes_a_stamp(self):
        app = self.project("Projects", "app", git=True)
        proc = self.mark("--root", app, "--report", "--tool-name", "mempalace_mempalace_mine")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("never mined", proc.stdout)
        self.assertFalse((self.state / "projects").exists())

    def test_report_is_empty_when_up_to_date(self):
        app = self.project("Projects", "app", git=True)
        self.mark("--root", app)
        self.assertEqual(self.mark("--root", app, "--report").stdout, "")


class VendorTests(unittest.TestCase):
    def test_vendored_hooks_are_byte_identical(self):
        for rel, expected in VENDOR_SHA256.items():
            actual = hashlib.sha256((PLUGIN / rel).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, f"{rel} changed; vendored hooks must stay unmodified")


if __name__ == "__main__":
    unittest.main()
