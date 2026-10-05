from __future__ import annotations

import json
import os
import threading
import time
import unittest

from fixtures import SandboxTestCase

import rg_common as common


class ConfigTests(SandboxTestCase):
    def test_should_load_bundled_defaults_when_user_file_missing(self):
        cfg = common.load_config()
        self.assertEqual(cfg["freeze_mode"], "observe")
        self.assertEqual(cfg["thresholds"]["critical"]["mem_available_pct_below"], 10)

    def test_should_merge_nested_user_overrides_without_dropping_siblings(self):
        self.user_config({"freeze_mode": "enforce", "thresholds": {"critical": {"mem_available_pct_below": 12}}})
        cfg = common.load_config()
        self.assertEqual(cfg["freeze_mode"], "enforce")
        self.assertEqual(cfg["thresholds"]["critical"]["mem_available_pct_below"], 12)
        self.assertEqual(cfg["thresholds"]["critical"]["swap_free_pct_below"], 20)

    def test_should_replace_lists_instead_of_appending(self):
        self.user_config({"never_pause": ["*keep*"]})
        self.assertEqual(common.load_config()["never_pause"], ["*keep*"])

    def test_should_fall_back_to_defaults_when_user_file_is_broken(self):
        (self.tmp / "user-config.json").write_text("{not json")
        self.assertEqual(common.load_config()["freeze_mode"], "observe")

    def test_should_ignore_user_file_that_is_not_an_object(self):
        (self.tmp / "user-config.json").write_text("[1, 2]")
        self.assertTrue(common.load_config()["enabled"])

    def test_should_report_disabled_by_env_or_config(self):
        self.assertFalse(common.disabled({"enabled": True}))
        self.assertTrue(common.disabled({"enabled": False}))
        os.environ["RESOURCE_GUARD_DISABLE"] = "1"
        self.assertTrue(common.disabled({"enabled": True}))


class PathTests(SandboxTestCase):
    def test_should_create_state_dir_private(self):
        path = common.state_dir()
        self.assertEqual(path, self.state)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_should_default_state_dir_under_claude_home(self):
        os.environ.pop("RESOURCE_GUARD_STATE_DIR")
        self.assertEqual(common.state_dir(), self.claude_home / ".resource-guard")

    def test_should_fall_back_to_file_location_without_plugin_root(self):
        os.environ.pop("CLAUDE_PLUGIN_ROOT")
        self.assertEqual(common.plugin_root().name, "plugin-resource-guard")

    def test_should_use_live_roots_without_overrides(self):
        os.environ.pop("RESOURCE_GUARD_PROC_ROOT")
        os.environ.pop("RESOURCE_GUARD_SYS_ROOT")
        self.assertEqual(str(common.proc_root()), "/proc")
        self.assertEqual(str(common.sys_root()), "/sys")


class JsonTests(SandboxTestCase):
    def test_should_write_atomically_with_private_mode(self):
        path = self.state / "sub" / "x.json"
        common.atomic_write_json(path, {"a": 1})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["x.json"])

    def test_should_clean_up_the_temp_file_when_writing_fails(self):
        path = self.state / "y.json"
        with self.assertRaises(TypeError):
            common.atomic_write_json(path, {"a": object()})
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.startswith(".y.json")], [])
        self.assertFalse(path.exists())

    def test_should_return_default_for_missing_or_broken_json(self):
        self.assertEqual(common.read_json(self.tmp / "nope.json", {"d": 1}), {"d": 1})
        (self.tmp / "bad.json").write_text("{")
        self.assertIsNone(common.read_json(self.tmp / "bad.json"))


class LockTests(SandboxTestCase):
    def test_should_time_out_when_lock_is_held_elsewhere(self):
        path = self.state / "t.lock"
        held, release = threading.Event(), threading.Event()

        def holder():
            with common.locked(path):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        started = time.monotonic()
        with self.assertRaises(common.LockTimeout):
            with common.locked(path, timeout=0.2):
                pass
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        release.set()
        thread.join()

    def test_should_take_free_lock_with_timeout(self):
        with common.locked(self.state / "free.lock", timeout=0.5):
            entered = True
        self.assertTrue(entered)


class EventTests(SandboxTestCase):
    def test_should_append_and_read_events_after_timestamp(self):
        first = common.append_event("freeze", session="1-2")
        common.append_event("resume", session="1-2")
        kinds = [event["kind"] for event in common.read_events(since=first["ts"] - 1)]
        self.assertEqual(kinds, ["freeze", "resume"])

    def test_should_rotate_events_file_over_cap(self):
        path = common.events_path()
        path.write_text("x" * (common.EVENTS_MAX_BYTES + 1))
        common.append_event("ping")
        self.assertTrue(path.with_name("events.jsonl.1").exists())
        self.assertEqual([e["kind"] for e in common.read_events()], ["ping"])

    def test_should_skip_garbage_lines(self):
        common.events_path().write_text("nope\n[]\n")
        self.assertEqual(common.read_events(), [])

    def test_should_return_empty_without_events_file(self):
        self.assertEqual(common.read_events(), [])


class KeyTests(unittest.TestCase):
    def test_should_round_trip_session_key(self):
        self.assertEqual(common.session_key(42, "1170752"), "42-1170752")
        self.assertEqual(common.parse_key("42-1170752"), (42, 1170752))

    def test_should_rank_levels_and_treat_unknown_as_ok(self):
        self.assertEqual(common.level_index("hard"), 3)
        self.assertEqual(common.level_index("bogus"), 0)


if __name__ == "__main__":
    unittest.main()
