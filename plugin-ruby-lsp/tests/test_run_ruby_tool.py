#!/usr/bin/env python3
"""Selection-logic tests for scripts/run-ruby-tool.sh.

Stub `docker`, `bundle` and tool binaries are placed first on PATH. Each stub
records its argv to a log file, so the tests assert which runtime the wrapper
picked without touching a real Docker daemon or Ruby install.

  python3 -m unittest discover -s plugin-ruby-lsp/tests
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "run-ruby-tool.sh"
sys.path.insert(0, str(WRAPPER.parent))

import lsp_hub  # noqa: E402

LOCKFILE = """GEM
  remote: https://rubygems.org/
  specs:
    reek (6.5.0)
    ruby-lsp (0.26.1)

PLATFORMS
  ruby
"""

# `docker` stub: behavior driven by env so each test can flip one condition.
DOCKER_STUB = """#!/usr/bin/env bash
if [ "$1" = compose ] && [ "$2" = version ]; then exit "${STUB_COMPOSE_EXIT:-0}"; fi
if [ "$1" = info ]; then exit "${STUB_INFO_EXIT:-0}"; fi
if [ "$1" = compose ] && [ "$2" = config ]; then printf '%b' "${STUB_SERVICES:-db\\nweb\\n}"; exit 0; fi
if [ "$1" = inspect ]; then [ -n "${STUB_RUNNING:-}" ] || exit 1; printf '%s\\n' "$STUB_RUNNING"; exit 0; fi
printf 'docker %s\\n' "$*" >> "$STUB_LOG"
"""

RECORD_STUB = """#!/usr/bin/env bash
printf '{name} %s\\n' "$*" >> "$STUB_LOG"
"""


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.project = tmp / "project"
        self.project.mkdir()
        (self.project / "Gemfile").write_text("source 'https://rubygems.org'\n")
        (self.project / "Gemfile.lock").write_text(LOCKFILE)
        (self.project / "compose.yml").write_text("services: {}\n")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log = tmp / "calls.log"
        self.stub("docker", DOCKER_STUB)
        self.stub("bundle", RECORD_STUB.format(name="bundle"))

    def tearDown(self):
        self._tmp.cleanup()

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def remove_stub(self, name):
        (self.bin / name).unlink()

    def run_wrapper(self, *args, **env_overrides):
        # Only the stub dir plus the dirs holding bash/coreutils: keeps a real
        # docker/bundle/reek on the machine from leaking into the test.
        system_dirs = {str(Path(shutil.which(tool)).parent) for tool in ("bash", "grep", "dirname")}
        env = {
            "PATH": os.pathsep.join([str(self.bin), *sorted(system_dirs)]),
            "HOME": self._tmp.name,
            "STUB_LOG": str(self.log),
            "CLAUDE_PLUGIN_ROOT": "/plugin/root",
            **env_overrides,
        }
        return subprocess.run(
            ["bash", str(WRAPPER), *args], cwd=self.project, capture_output=True, text=True, env=env
        )

    def calls(self):
        return self.log.read_text().strip() if self.log.exists() else ""

    def skip_if_leaks(self, tool):
        system_dirs = os.pathsep.join(
            str(Path(shutil.which(t)).parent) for t in ("bash", "grep", "dirname")
        )
        if shutil.which(tool, path=system_dirs):
            self.skipTest(f"{tool} installed in a system dir; cannot hide it")

    def test_docker_selected_when_all_conditions_hold(self):
        proc = self.run_wrapper("reek", "--format", "json", "app/models/a.rb")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        call = self.calls()
        self.assertTrue(call.startswith("docker compose run --rm --no-deps -T "))
        self.assertIn(f"-v {self.project}:{self.project} ", call)
        self.assertIn("-v /plugin/root:/plugin/root:ro ", call)
        self.assertIn(f"-w {self.project} ", call)
        self.assertTrue(call.endswith("web bundle exec reek --format json app/models/a.rb"))

    def test_lsp_mode_execs_the_bridge(self):
        self.stub("python3", RECORD_STUB.format(name="python3"))
        self.run_wrapper("lsp")
        self.assertEqual(self.calls(), "python3 /plugin/root/scripts/lsp_bridge.py")

    def test_lsp_backend_maps_to_ruby_lsp(self):
        self.run_wrapper("lsp-backend")
        call = self.calls()
        self.assertTrue(call.startswith("docker compose run --rm --no-deps -T -e "), call)
        self.assertTrue(call.endswith("web bundle exec ruby-lsp"))

    def test_lsp_backend_names_the_hub_container(self):
        self.run_wrapper("lsp-backend", RUBY_LSP_PLUGIN_HUB_CONTAINER="ruby-lsp-abc")
        remove, run = self.calls().splitlines()
        self.assertEqual(remove, "docker rm -f ruby-lsp-abc")
        self.assertTrue(run.startswith("docker compose run --rm --no-deps -T --name ruby-lsp-abc -e "), run)
        self.assertTrue(run.endswith("web bundle exec ruby-lsp"))

    def test_reek_reuses_the_running_hub_container(self):
        # The bash key and lsp_hub.container_name() must agree, or Reek never finds the container.
        container = lsp_hub.container_name(str(self.project))
        self.run_wrapper("reek", "--format", "json", "a.rb", STUB_RUNNING=f"true {self.project} /plugin/root ")
        self.assertEqual(self.calls(), f"docker exec -w {self.project} {container} bundle exec reek --format json a.rb")

    def test_reek_reuse_matches_plugin_root_as_first_mount(self):
        self.run_wrapper("reek", STUB_RUNNING="true /plugin/root ")
        self.assertTrue(self.calls().startswith("docker exec "))

    def test_reek_skips_a_stopped_hub_container(self):
        self.run_wrapper("reek", STUB_RUNNING="false /plugin/root ")
        self.assertTrue(self.calls().startswith("docker compose run "))

    def test_reek_skips_a_container_from_another_plugin_version(self):
        # The bundled .reek.yml lives under the plugin root, so it must be mounted.
        self.run_wrapper("reek", STUB_RUNNING="true /plugin/root-0.1.0 ")
        self.assertTrue(self.calls().startswith("docker compose run "))

    def test_force_host_skips_the_hub_container(self):
        self.run_wrapper("reek", STUB_RUNNING="true /plugin/root ", RUBY_LSP_PLUGIN_FORCE_HOST="1")
        self.assertEqual(self.calls(), "bundle exec reek")

    def test_service_override(self):
        self.run_wrapper("reek", RUBY_LSP_PLUGIN_SERVICE="worker")
        self.assertIn(" worker bundle exec reek", self.calls())

    def test_first_known_service_wins(self):
        self.run_wrapper("reek", STUB_SERVICES="db\\napi\\napp\\n")
        self.assertIn(" app bundle exec reek", self.calls())

    def test_unknown_services_fall_back_to_host_bundle(self):
        proc = self.run_wrapper("reek", STUB_SERVICES="db\\nredis\\n")
        self.assertEqual(self.calls(), "bundle exec reek")
        self.assertIn("RUBY_LSP_PLUGIN_SERVICE", proc.stderr)

    def test_no_compose_file_uses_host_bundle(self):
        (self.project / "compose.yml").unlink()
        self.run_wrapper("reek")
        self.assertEqual(self.calls(), "bundle exec reek")

    def test_docker_daemon_down_uses_host_bundle(self):
        proc = self.run_wrapper("reek", STUB_INFO_EXIT="1")
        self.assertEqual(self.calls(), "bundle exec reek")
        self.assertIn("daemon not reachable", proc.stderr)

    def test_compose_missing_uses_host_bundle(self):
        self.run_wrapper("reek", STUB_COMPOSE_EXIT="1")
        self.assertEqual(self.calls(), "bundle exec reek")

    def test_force_host_skips_docker(self):
        self.run_wrapper("reek", RUBY_LSP_PLUGIN_FORCE_HOST="1")
        self.assertEqual(self.calls(), "bundle exec reek")

    def test_force_docker_refuses_fallback(self):
        proc = self.run_wrapper("reek", RUBY_LSP_PLUGIN_FORCE_DOCKER="1", STUB_INFO_EXIT="1")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.calls(), "")

    def test_gem_not_in_lockfile_uses_global_binary(self):
        (self.project / "Gemfile.lock").write_text("GEM\n  specs:\n    rails (7.2.0)\n")
        self.stub("reek", RECORD_STUB.format(name="reek"))
        self.run_wrapper("reek", "x.rb")
        self.assertEqual(self.calls(), "reek x.rb")

    def test_nothing_found_exits_127(self):
        self.skip_if_leaks("reek")
        (self.project / "Gemfile.lock").unlink()
        proc = self.run_wrapper("reek")
        self.assertEqual(proc.returncode, 127)
        self.assertIn("reek not found", proc.stderr)

    def test_diagnostics_never_on_stdout(self):
        proc = self.run_wrapper("reek", STUB_INFO_EXIT="1")
        self.assertEqual(proc.stdout, "")

    def test_unknown_mode_is_usage_error(self):
        proc = self.run_wrapper("rubocop")
        self.assertEqual(proc.returncode, 64)
        self.assertIn("usage", proc.stderr)

    def test_no_args_is_usage_error(self):
        self.assertEqual(self.run_wrapper().returncode, 64)


if __name__ == "__main__":
    unittest.main()
