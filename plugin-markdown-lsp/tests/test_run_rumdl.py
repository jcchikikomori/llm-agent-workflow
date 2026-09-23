#!/usr/bin/env python3
"""Selection-logic tests for scripts/run-rumdl.sh.

Stub `rumdl`, `docker`, `uvx` and `npx` binaries are placed on an isolated
PATH. Each stub records its argv to a log file, so the tests assert which
runtime and config the wrapper picked without a real rumdl or Docker daemon.

  python3 -m unittest discover -s plugin-markdown-lsp/tests
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = PLUGIN_ROOT / "scripts" / "run-rumdl.sh"
BUNDLED_CONFIG = PLUGIN_ROOT / "config" / "rumdl.toml"

# `docker info` exit code is env-driven so each test can flip daemon health.
DOCKER_STUB = """#!/usr/bin/env bash
if [ "$1" = info ]; then exit "${STUB_INFO_EXIT:-0}"; fi
printf 'docker %s\\n' "$*" >> "$STUB_LOG"
"""

RECORD_STUB = """#!/usr/bin/env bash
printf '{name} %s\\n' "$*" >> "$STUB_LOG"
"""

# Real utilities the wrapper and stubs need. Linked into an isolated dir so a
# host rumdl/docker/uvx/npx never leaks into the test PATH.
UTILITIES = ("bash", "env", "dirname", "grep", "id")


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # The .git dir marks the repo boundary, so the upward config search
        # never escapes into the machine's real home or root directories.
        self.repo = tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.project = self.repo
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.tools = tmp / "tools"
        self.tools.mkdir()
        for name in UTILITIES:
            (self.tools / name).symlink_to(shutil.which(name))
        self.log = tmp / "calls.log"

    def tearDown(self):
        self._tmp.cleanup()

    def stub(self, name, body=None):
        path = self.bin / name
        path.write_text(body or RECORD_STUB.format(name=name))
        path.chmod(0o755)

    def run_wrapper(self, *args, **env):
        full_env = {
            "PATH": f"{self.bin}:{self.tools}",
            "STUB_LOG": str(self.log),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
        }
        full_env.update(env)
        return subprocess.run(
            [shutil.which("bash"), str(WRAPPER), *(args or ("server",))],
            cwd=self.project,
            env=full_env,
            capture_output=True,
            text=True,
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    # -- runtime selection ---------------------------------------------------

    def test_docker_wins_over_host_rumdl(self):
        self.stub("rumdl")
        self.stub("docker", DOCKER_STUB)

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 0, result.stderr)
        [call] = self.calls()
        self.assertTrue(call.startswith("docker run "), call)
        self.assertIn("running rumdl in ghcr.io/rvben/rumdl:latest", result.stderr)

    def test_host_rumdl_when_daemon_unreachable(self):
        self.stub("rumdl")
        self.stub("docker", DOCKER_STUB)
        self.stub("uvx")

        result = self.run_wrapper(STUB_INFO_EXIT="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [f"rumdl --config {BUNDLED_CONFIG} server"])
        self.assertIn("docker daemon not reachable", result.stderr)
        self.assertIn("running host rumdl", result.stderr)

    def test_host_rumdl_when_docker_missing(self):
        self.stub("rumdl")

        result = self.run_wrapper()

        self.assertEqual(self.calls(), [f"rumdl --config {BUNDLED_CONFIG} server"])
        self.assertIn("docker not on PATH", result.stderr)

    def test_docker_run_args(self):
        self.stub("docker", DOCKER_STUB)

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 0, result.stderr)
        [call] = self.calls()
        self.assertTrue(call.startswith("docker run --rm -i --user "), call)
        self.assertIn(f"-v {self.project}:{self.project} ", call)
        self.assertIn(f"-v {PLUGIN_ROOT}:{PLUGIN_ROOT}:ro ", call)
        self.assertIn(f"-w {self.project} ", call)
        self.assertTrue(
            call.endswith(f"ghcr.io/rvben/rumdl:latest --config {BUNDLED_CONFIG} server"), call
        )

    def test_docker_skips_plugin_mount_inside_project(self):
        # A read-only plugin mount nested in the project mount would make that
        # subtree unwritable for `rumdl fmt`.
        self.stub("docker", DOCKER_STUB)
        nested_root = self.project / "plugin-markdown-lsp"
        nested_root.mkdir()

        self.run_wrapper(CLAUDE_PLUGIN_ROOT=str(nested_root))

        [call] = self.calls()
        self.assertIn(f"-v {self.project}:{self.project} ", call)
        self.assertNotIn(":ro", call)

    def test_docker_image_override(self):
        self.stub("docker", DOCKER_STUB)

        self.run_wrapper(MARKDOWN_LSP_PLUGIN_IMAGE="ghcr.io/rvben/rumdl:0.2.76")

        [call] = self.calls()
        self.assertIn(" ghcr.io/rvben/rumdl:0.2.76 ", call)

    def test_unreachable_daemon_falls_back_to_uvx(self):
        self.stub("docker", DOCKER_STUB)
        self.stub("uvx")

        result = self.run_wrapper(STUB_INFO_EXIT="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [f"uvx rumdl --config {BUNDLED_CONFIG} server"])
        self.assertIn("docker daemon not reachable", result.stderr)

    def test_npx_when_no_uvx(self):
        self.stub("npx")

        result = self.run_wrapper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [f"npx --yes rumdl --config {BUNDLED_CONFIG} server"])

    def test_nothing_found_exits_127_with_hint(self):
        result = self.run_wrapper()

        self.assertEqual(result.returncode, 127)
        self.assertIn("rumdl not found", result.stderr)
        self.assertIn("uv tool install rumdl", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_force_host_skips_docker(self):
        self.stub("docker", DOCKER_STUB)
        self.stub("uvx")

        result = self.run_wrapper(MARKDOWN_LSP_PLUGIN_FORCE_HOST="1")

        self.assertEqual(self.calls(), [f"uvx rumdl --config {BUNDLED_CONFIG} server"])
        self.assertIn("FORCE_HOST=1", result.stderr)

    def test_force_docker_uses_docker(self):
        self.stub("rumdl")
        self.stub("docker", DOCKER_STUB)

        result = self.run_wrapper(MARKDOWN_LSP_PLUGIN_FORCE_DOCKER="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        [call] = self.calls()
        self.assertTrue(call.startswith("docker run "), call)

    def test_force_docker_refuses_fallback_when_unusable(self):
        self.stub("rumdl")
        self.stub("docker", DOCKER_STUB)

        result = self.run_wrapper(MARKDOWN_LSP_PLUGIN_FORCE_DOCKER="1", STUB_INFO_EXIT="1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls(), [])
        self.assertIn("refusing host fallback", result.stderr)

    # -- config selection ----------------------------------------------------

    def assert_no_bundled_config(self):
        self.stub("rumdl")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["rumdl server"])
        self.assertIn("project config found", result.stderr)

    def test_project_rumdl_toml_wins(self):
        (self.project / ".rumdl.toml").write_text("[global]\n")
        self.assert_no_bundled_config()

    def test_project_config_dir_wins(self):
        (self.project / ".config").mkdir()
        (self.project / ".config" / "rumdl.toml").write_text("[global]\n")
        self.assert_no_bundled_config()

    def test_project_markdownlint_json_wins(self):
        (self.project / ".markdownlint.json").write_text("{}\n")
        self.assert_no_bundled_config()

    def test_pyproject_with_tool_rumdl_wins(self):
        (self.project / "pyproject.toml").write_text("[tool.rumdl]\nline-length = 100\n")
        self.assert_no_bundled_config()

    def test_pyproject_without_tool_rumdl_uses_bundled(self):
        self.stub("rumdl")
        (self.project / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")

        self.run_wrapper()

        self.assertEqual(self.calls(), [f"rumdl --config {BUNDLED_CONFIG} server"])

    def test_config_in_parent_dir_is_found(self):
        (self.repo / ".rumdl.toml").write_text("[global]\n")
        self.project = self.repo / "docs"
        self.project.mkdir()
        self.assert_no_bundled_config()

    def test_search_stops_at_git_boundary(self):
        # Config above the repo root belongs to something else.
        (self.repo.parent / ".rumdl.toml").write_text("[global]\n")
        self.stub("rumdl")

        self.run_wrapper()

        self.assertEqual(self.calls(), [f"rumdl --config {BUNDLED_CONFIG} server"])

    def test_explicit_config_env_wins(self):
        (self.project / ".rumdl.toml").write_text("[global]\n")
        self.stub("rumdl")

        self.run_wrapper(MARKDOWN_LSP_PLUGIN_CONFIG="/etc/custom.toml")

        self.assertEqual(self.calls(), ["rumdl --config /etc/custom.toml server"])

    # -- plumbing --------------------------------------------------------------

    def test_passes_extra_args_through(self):
        self.stub("rumdl")

        self.run_wrapper("check", "README.md")

        self.assertEqual(self.calls(), [f"rumdl --config {BUNDLED_CONFIG} check README.md"])

    def test_no_args_is_usage_error(self):
        result = subprocess.run(
            [shutil.which("bash"), str(WRAPPER)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("usage", result.stderr)

    def test_stdout_stays_clean(self):
        # stdout is the JSON-RPC channel; wrapper logs must never land there.
        self.stub("docker", DOCKER_STUB)
        for env in ({}, {"STUB_INFO_EXIT": "1"}, {"MARKDOWN_LSP_PLUGIN_FORCE_HOST": "1"}):
            with self.subTest(env=env):
                result = self.run_wrapper(**env)
                self.assertEqual(result.stdout, "")
                self.assertIn("[markdown-lsp]", result.stderr)

    def test_bundled_config_exists(self):
        self.assertTrue(BUNDLED_CONFIG.is_file())


if __name__ == "__main__":
    os.chdir(PLUGIN_ROOT)
    unittest.main()
