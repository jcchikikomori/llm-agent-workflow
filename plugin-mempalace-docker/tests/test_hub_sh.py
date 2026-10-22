#!/usr/bin/env python3
"""Behaviour of scripts/hub.sh: the run argv, lifecycle decisions, registry.

  python3 -m unittest discover -s plugin-mempalace-docker/tests
"""

import json
import os
import platform
import shutil
import stat
import unittest

from support import CPU_IMAGE, FP_LABEL, HubTestCase, system_path


class CreateArgvTests(HubTestCase):
    def test_missing_hub_is_created_with_the_shared_layout(self):
        app = self.project("Projects", "app")
        lib = self.project("Projects", "lib")
        self.register(app)
        self.register(lib)

        proc = self.hub("ensure")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.run_call()
        self.assertEqual(argv[:4], ["run", "-d", "--name", "mempalace-hub"])
        self.assert_flag(argv, "-p", "127.0.0.1:8765:8765")
        self.assert_flag(argv, "--env-file", f"{self.state}/hub/env")
        self.assertEqual(
            self.mount_args(argv),
            [
                "mempalace-data:/data",
                f"{self.home}/.claude/projects:/transcripts:ro",
                f"{self.home}/.claude:{self.home}/.claude:ro",
                f"{app}:{app}:ro",
                f"{lib}:{lib}:ro",
            ],
        )
        self.assertEqual(argv[-6:], [CPU_IMAGE, "serve", "--host", "0.0.0.0", "--port", "8765"])
        self.assertTrue(any(a.startswith(f"{FP_LABEL}=") for a in argv))
        self.assert_flag(argv, "--health-start-interval", "1s")
        self.assertIn("--health-cmd", argv)

    def test_no_work_mount_restart_policy_memory_or_gpu_by_default(self):
        self.hub("ensure")
        argv = self.run_call()
        self.assertFalse(any("/work" in a for a in argv))
        self.assertNotIn("--restart", argv)
        self.assertNotIn("--memory", argv)
        self.assertNotIn("--runtime=nvidia", argv)

    def test_token_reaches_the_container_only_through_the_env_file(self):
        self.hub("ensure")
        token = (self.state / "hub" / "token").read_text().strip()
        env_file = self.state / "hub" / "env"

        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(
            env_file.read_text(),
            f"MEMPALACE_MCP_HTTP_TOKEN={token}\nMEMPALACE_MCP_IDLE_HOURS=2\n",
        )
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state / "hub" / "token").stat().st_mode), 0o600)
        self.assertNotIn(token, (self.stub_dir / "calls.log").read_text())

    def test_state_file_records_what_was_mounted(self):
        app = self.project("Projects", "app")
        self.register(app)

        self.hub("ensure")

        state = json.loads((self.state / "hub" / "state.json").read_text())
        self.assertEqual(state["container"], "mempalace-hub")
        self.assertEqual(state["port"], 8765)
        self.assertEqual(state["mounted_targets"], [str(app)])
        self.assertEqual(state["image"], CPU_IMAGE)
        self.assertEqual(len(state["fingerprint"]), 64)

    def test_always_on_when_idle_hours_is_zero(self):
        self.hub("ensure", MEMPALACE_HUB_IDLE_HOURS="0")
        argv = self.run_call()
        self.assert_flag(argv, "--restart", "unless-stopped")
        self.assertIn("MEMPALACE_MCP_IDLE_HOURS=0\n", (self.state / "hub" / "env").read_text())

    def test_explicit_memory_cap(self):
        self.hub("ensure", MEMPALACE_HUB_MEMORY="4g")
        self.assert_flag(self.run_call(), "--memory", "4g")

    def test_custom_name_and_port(self):
        self.hub("ensure", MEMPALACE_HUB_NAME="hub-x", MEMPALACE_HUB_PORT="18765")
        argv = self.run_call()
        self.assertEqual(argv[:4], ["run", "-d", "--name", "hub-x"])
        self.assert_flag(argv, "-p", "127.0.0.1:18765:8765")

    @unittest.skipUnless(platform.machine() == "x86_64", "the CUDA image is x86_64-only")
    def test_gpu_image_and_flags_when_nvidia_is_usable(self):
        self.hub(
            "ensure",
            STUB_NVIDIA_EXIT="0",
            STUB_INFO=" Runtimes: io.containerd.runc.v2 nvidia runc",
            STUB_IMAGE_EXIT="0",
        )
        argv = self.run_call()
        self.assertIn("--runtime=nvidia", argv)
        self.assert_flag(argv, "--gpus", "all")
        self.assertEqual(argv[-6], "mempalace:gpu")

    @unittest.skipUnless(platform.machine() == "x86_64", "the CUDA image is x86_64-only")
    def test_gpu_falls_back_to_cpu_when_the_image_is_not_built(self):
        proc = self.hub(
            "ensure",
            STUB_NVIDIA_EXIT="0",
            STUB_INFO=" Runtimes: io.containerd.runc.v2 nvidia runc",
            STUB_IMAGE_EXIT="1",
        )
        argv = self.run_call()
        self.assertNotIn("--runtime=nvidia", argv)
        self.assertEqual(argv[-6], CPU_IMAGE)
        self.assertIn("not built locally", proc.stderr)

    def test_explicit_image_override(self):
        self.hub("ensure", MEMPALACE_DOCKER_IMAGE="mempalace:custom")
        self.assertEqual(self.run_call()[-6], "mempalace:custom")


class RegistryMountTests(HubTestCase):
    def test_nested_targets_are_covered_by_their_parent(self):
        parent = self.project("Projects")
        child = self.project("Projects", "app")
        self.register(parent)
        self.register(child)

        self.hub("ensure")

        mounts = self.mount_args(self.run_call())
        self.assertIn(f"{parent}:{parent}:ro", mounts)
        self.assertNotIn(f"{child}:{child}:ro", mounts)

    def test_sibling_with_a_shared_prefix_is_not_mistaken_for_a_child(self):
        b = self.project("w", "b")
        bx = self.project("w", "b-x")
        bc = self.project("w", "b", "c")
        self.register(b)
        self.register(bx)
        self.register(bc, name="c")

        self.hub("ensure")

        mounts = self.mount_args(self.run_call())
        self.assertIn(f"{b}:{b}:ro", mounts)
        self.assertIn(f"{bx}:{bx}:ro", mounts)
        self.assertNotIn(f"{bc}:{bc}:ro", mounts)

    def test_dangling_entry_is_skipped_with_a_note(self):
        app = self.project("Projects", "app")
        self.register(app)
        self.register(self.home / "gone")

        proc = self.hub("ensure")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("does not resolve to a directory", proc.stderr)
        self.assertEqual(
            [m for m in self.mount_args(self.run_call()) if m.endswith(":ro") and "Projects" in m],
            [f"{app}:{app}:ro"],
        )

    def test_reserved_targets_never_become_mounts(self):
        self.registry.mkdir(parents=True)
        (self.registry / "root").symlink_to("/")
        (self.registry / "usr").symlink_to("/usr")

        proc = self.hub("ensure")

        self.assertIn("would overlay a system or container path", proc.stderr)
        mounts = self.mount_args(self.run_call())
        self.assertNotIn("/:/:ro", mounts)
        self.assertNotIn("/usr:/usr:ro", mounts)

    def test_target_under_dot_claude_is_already_covered(self):
        inside = self.home / ".claude" / "notes"
        inside.mkdir()
        self.register(inside)

        self.hub("ensure")

        self.assertNotIn(f"{inside}:{inside}:ro", self.mount_args(self.run_call()))


class LifecycleTests(HubTestCase):
    def test_running_hub_is_left_alone(self):
        self.set_container("running", "whatever", "healthy")
        proc = self.hub("ensure")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([c[0] for c in self.calls()], ["inspect"])

    def test_check_reports_a_running_hub_with_an_older_config(self):
        self.set_container("running", "old", "healthy")
        proc = self.hub("ensure", "--check")
        self.assertIn("older config", proc.stderr)
        self.assertEqual(self.calls_of("run"), [])
        self.assertEqual(self.calls_of("rm"), [])

    def test_check_is_quiet_when_the_config_matches(self):
        self.set_container("running", self.fingerprint(), "healthy")
        proc = self.hub("ensure", "--check")
        self.assertNotIn("older config", proc.stderr)

    def test_stopped_hub_with_the_same_config_is_started(self):
        self.set_container("exited", self.fingerprint(), "unhealthy")
        proc = self.hub("ensure")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls_of("start"), [["start", "mempalace-hub"]])
        self.assertEqual(self.calls_of("run"), [])

    def test_stopped_hub_with_an_older_config_is_recreated(self):
        self.set_container("exited", "old", "unhealthy")
        proc = self.hub("ensure")
        self.assertIn("config changed; recreating it", proc.stderr)
        self.assertNotIn("older config", proc.stderr)
        self.assertEqual(self.calls_of("rm"), [["rm", "-f", "mempalace-hub"]])
        self.run_call()

    def test_registry_change_changes_the_fingerprint(self):
        before = self.fingerprint()
        self.register(self.project("Projects", "app"))
        self.assertNotEqual(self.fingerprint(), before)

    def test_paused_hub_is_reported_not_touched(self):
        self.set_container("paused", "x", "healthy")
        proc = self.hub("ensure")
        self.assertIn("docker unpause mempalace-hub", proc.stderr)
        self.assertEqual([c[0] for c in self.calls()], ["inspect"])

    def test_wait_returns_once_healthy(self):
        self.set_health_sequence("starting", "starting", "healthy")
        proc = self.hub("ensure", "--wait", "10")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_wait_times_out(self):
        proc = self.hub("ensure", "--wait", "2")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not healthy after 2s", proc.stderr)

    def test_wait_on_an_exited_hub_shows_its_logs_and_the_lease_hint(self):
        proc = self.hub("ensure", "--wait", "5", STUB_RUN_STATUS="exited")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("STUB LOG LINE", proc.stderr)
        self.assertIn("mempalace-docker 1.x", proc.stderr)

    def test_unknown_ensure_argument_fails(self):
        proc = self.hub("ensure", "--bogus")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown argument", proc.stderr)

    def test_failed_run_fails_ensure(self):
        proc = self.hub("ensure", STUB_RUN_EXIT="125")
        self.assertNotEqual(proc.returncode, 0)

    def test_stop_running_and_missing(self):
        self.set_container("running", "x", "healthy")
        self.hub("stop")
        self.assertEqual(self.calls_of("stop"), [["stop", "mempalace-hub"]])
        self.hub("rm")
        proc = self.hub("stop")
        self.assertIn("does not exist", proc.stderr)

    def test_rm_keeps_the_volume(self):
        self.set_container("exited", "x", "unhealthy")
        proc = self.hub("rm")
        self.assertEqual(self.calls_of("rm"), [["rm", "-f", "mempalace-hub"]])
        self.assertIn("mempalace-data is untouched", proc.stderr)
        self.assertFalse(any(c[:2] == ["volume", "rm"] for c in self.calls()))

    def test_restart_recreates_and_waits(self):
        self.set_container("running", "old", "healthy")
        self.set_health_sequence("starting", "healthy")
        proc = self.hub("restart")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls_of("rm"), [["rm", "-f", "mempalace-hub"]])
        self.run_call()

    def test_status_exit_code_follows_the_container(self):
        app = self.project("Projects", "app")
        self.register(app)
        missing = self.hub("status")
        self.assertEqual(missing.returncode, 1)
        self.assertIn(f"registered: {app}", missing.stderr)
        self.set_container("running", "x", "healthy")
        self.assertEqual(self.hub("status").returncode, 0)

    def test_print_run_does_not_create_anything(self):
        proc = self.hub("print-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("docker run -d --name mempalace-hub "))
        self.assertIn("serve --host 0.0.0.0 --port 8765", proc.stdout)
        self.assertEqual(self.calls_of("run"), [])

    def test_token_is_stable(self):
        first = self.hub("token").stdout.strip()
        second = self.hub("token").stdout.strip()
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 40)

    def test_unknown_command_and_missing_command(self):
        self.assertEqual(self.hub("frobnicate").returncode, 1)
        self.assertEqual(self.hub().returncode, 1)

    def test_help_exits_zero(self):
        proc = self.hub("help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("hub.sh ensure", proc.stderr)

    def test_logs_passes_arguments_through(self):
        self.hub("logs", "--tail", "3")
        self.assertEqual(self.calls_of("logs"), [["logs", "--tail", "3", "mempalace-hub"]])

    def test_missing_docker_is_a_clear_error(self):
        if shutil.which("docker", path=system_path()):
            self.skipTest("a real docker lives in a system dir; cannot hide it")
        (self.bin / "docker").unlink()
        proc = self.hub("ensure")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("docker not found", proc.stderr)


class RegisterTests(HubTestCase):
    def test_register_creates_a_symlink_named_after_the_directory(self):
        app = self.project("Projects", "app")
        proc = self.hub("register", app)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        link = self.registry / "app"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(link), str(app))
        self.assertIn("hub.sh restart", proc.stderr)

    def test_register_twice_is_a_no_op(self):
        app = self.project("Projects", "app")
        self.hub("register", app)
        proc = self.hub("register", app)
        self.assertIn("already", proc.stderr)
        self.assertEqual(sorted(p.name for p in self.registry.iterdir()), ["app"])

    def test_register_inside_a_registered_parent_is_a_no_op(self):
        self.hub("register", self.project("Projects"))
        proc = self.hub("register", self.project("Projects", "app"))
        self.assertIn("already covered", proc.stderr)
        self.assertEqual(sorted(p.name for p in self.registry.iterdir()), ["Projects"])

    def test_name_clash_gets_a_hash_suffix(self):
        self.hub("register", self.project("a", "app"))
        self.hub("register", self.project("b", "app"))
        names = sorted(p.name for p in self.registry.iterdir())
        self.assertEqual(names[0], "app")
        self.assertRegex(names[1], r"^app-[0-9a-f]{8}$")

    def test_register_rejects_non_directories_and_reserved_paths(self):
        f = self.tmp / "file.txt"
        f.write_text("x")
        self.assertEqual(self.hub("register", f).returncode, 1)
        self.assertEqual(self.hub("register").returncode, 1)
        proc = self.hub("register", "/")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing /", proc.stderr)
        self.assertFalse(self.registry.exists() and any(self.registry.iterdir()))


if __name__ == "__main__":
    unittest.main()
