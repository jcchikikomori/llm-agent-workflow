#!/usr/bin/env python3
"""The CLI shim, the MEMPAL_PYTHON shim and the headersHelper.

The shims replaced `docker run --rm` per call with `docker exec` into the
shared hub, so these tests pin both the exec argv and the stdin rule the
vendored hooks depend on (see scripts/bin/mempalace-python3).
"""

import json
import unittest

from support import CLI_SHIM, HEADERS, PY_SHIM, HubTestCase


class CliShimTests(HubTestCase):
    def test_execs_the_cli_inside_a_healthy_hub(self):
        self.set_container("running", "x", "healthy")
        proc = self.run_script(CLI_SHIM, "mine", "/transcripts", "--mode", "convos")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.calls_of("exec"),
            [["exec", "-i", "mempalace-hub", "mempalace", "mine", "/transcripts", "--mode", "convos"]],
        )
        self.assertEqual(proc.stdout, "EXEC-OUT\n")

    def test_mine_without_mode_gets_upstreams_default(self):
        self.set_container("running", "x", "healthy")
        self.run_script(CLI_SHIM, "mine", "/home/me/app")
        self.assertEqual(
            self.calls_of("exec")[0][3:],
            ["mempalace", "mine", "/home/me/app", "--mode", "projects"],
        )

    def test_mine_with_a_mode_is_passed_through(self):
        self.set_container("running", "x", "healthy")
        self.run_script(CLI_SHIM, "mine", "/x", "--mode=convos")
        self.assertEqual(self.calls_of("exec")[0][3:], ["mempalace", "mine", "/x", "--mode=convos"])

    def test_other_commands_get_no_mode(self):
        self.set_container("running", "x", "healthy")
        self.run_script(CLI_SHIM, "search", "mine")
        self.assertEqual(self.calls_of("exec")[0][3:], ["mempalace", "search", "mine"])

    def test_starts_a_missing_hub_before_exec(self):
        self.set_health_sequence("starting", "healthy")
        proc = self.run_script(CLI_SHIM, "status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verbs = [c[0] for c in self.calls()]
        self.assertLess(verbs.index("run"), verbs.index("exec"))

    def test_runs_anyway_when_the_hub_never_gets_healthy(self):
        proc = self.run_script(CLI_SHIM, "status", MEMPALACE_HUB_WAIT_SECONDS="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hub not healthy yet", proc.stderr)
        self.assertEqual(len(self.calls_of("exec")), 1)

    def test_exit_code_of_the_cli_is_preserved(self):
        self.set_container("running", "x", "healthy")
        proc = self.run_script(CLI_SHIM, "status", STUB_EXEC_EXIT="3")
        self.assertEqual(proc.returncode, 3)

    def test_hub_name_override(self):
        self.set_container("running", "x", "healthy")
        self.run_script(CLI_SHIM, "status", MEMPALACE_HUB_NAME="hub-x")
        self.assertEqual(self.calls_of("exec")[0][:3], ["exec", "-i", "hub-x"])


class PythonShimTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.set_container("running", "x", "starting")

    def test_inline_code_runs_detached_from_stdin(self):
        proc = self.run_script(PY_SHIM, "-c", "print(1)", input="HOOK PAYLOAD")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.calls_of("exec"),
            [["exec", "mempalace-hub", "/app/.venv/bin/python3", "-c", "print(1)"]],
        )
        self.assertEqual((self.stub_dir / "exec_stdin").read_text(), "")

    def test_module_calls_get_stdin(self):
        proc = self.run_script(
            PY_SHIM, "-m", "mempalace.hook_shell", "parse-stop", input="HOOK PAYLOAD"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.calls_of("exec"),
            [["exec", "-i", "mempalace-hub", "/app/.venv/bin/python3", "-m",
              "mempalace.hook_shell", "parse-stop"]],
        )
        self.assertEqual((self.stub_dir / "exec_stdin").read_text(), "HOOK PAYLOAD")

    def test_does_not_wait_for_health(self):
        # starting, never healthy: a waiting shim would call inspect repeatedly.
        self.run_script(PY_SHIM, "-c", "pass")
        self.assertEqual(len(self.calls_of("inspect")), 1)

    def test_starts_a_stopped_hub(self):
        (self.stub_dir / "state").unlink()
        self.run_script(PY_SHIM, "-c", "pass")
        self.assertEqual(len(self.calls_of("run")), 1)


class HeadersHelperTests(HubTestCase):
    def test_prints_only_the_bearer_header(self):
        self.set_container("running", "x", "healthy")
        proc = self.run_script(HEADERS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        token = (self.state / "hub" / "token").read_text().strip()
        self.assertEqual(json.loads(proc.stdout), {"Authorization": f"Bearer {token}"})

    def test_starts_the_hub_and_creates_the_token(self):
        self.set_health_sequence("starting", "healthy")
        proc = self.run_script(HEADERS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.calls_of("run")), 1)
        self.assertTrue((self.state / "hub" / "token").exists())
        self.assertIn("Authorization", json.loads(proc.stdout))

    def test_still_prints_the_header_when_the_hub_is_not_ready(self):
        proc = self.run_script(HEADERS, MEMPALACE_HUB_HELPER_WAIT_SECONDS="1")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Authorization", json.loads(proc.stdout))

    def test_still_prints_the_header_when_the_hub_cannot_start(self):
        proc = self.run_script(HEADERS, STUB_RUN_EXIT="125")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Authorization", json.loads(proc.stdout))


if __name__ == "__main__":
    unittest.main()
