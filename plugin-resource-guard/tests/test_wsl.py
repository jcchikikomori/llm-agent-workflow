from __future__ import annotations

import os
import subprocess
import unittest
from types import SimpleNamespace

from fixtures import SandboxTestCase

import rg_wsl as wsl

GB = 1024**3
WSLCONFIG = """[wsl2]
memory=16GB # cap
#processors=12
processors=8
swap=12GB
kernelCommandLine="vsyscall=emulate"

[experimental]
sparseVhd=true
"""


def runner_for(outputs):
    """subprocess.run stand-in: maps argv[0] to (returncode, stdout) or an
    exception instance to raise."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        result = outputs.get(argv[0], (1, ""))
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(returncode=result[0], stdout=result[1])

    run.calls = calls
    return run


class DetectTests(SandboxTestCase):
    def test_should_detect_wsl2_from_osrelease(self):
        self.proc.osrelease("6.18.33.2-microsoft-standard-WSL2")
        self.assertEqual(wsl.detect(env={"WSL_DISTRO_NAME": "Ubuntu-22.04"}),
                         {"wsl": True, "version": 2, "distro": "Ubuntu-22.04"})

    def test_should_detect_wsl1(self):
        self.proc.osrelease("4.4.0-19041-Microsoft")
        self.assertEqual(wsl.detect(env={})["version"], 1)

    def test_should_report_plain_linux(self):
        self.proc.osrelease("6.8.0-generic")
        self.assertEqual(wsl.detect(env={}), {"wsl": False, "version": None, "distro": ""})

    def test_should_trust_distro_env_when_osrelease_is_unreadable(self):
        self.assertEqual(wsl.detect(env={"WSL_DISTRO_NAME": "Debian"}), {"wsl": True, "version": None, "distro": "Debian"})


class ParseTests(unittest.TestCase):
    def test_should_parse_sizes(self):
        self.assertEqual(wsl.parse_size("16GB"), 16 * GB)
        self.assertEqual(wsl.parse_size("512mb"), 512 * 1024**2)
        self.assertEqual(wsl.parse_size("1.5G"), int(1.5 * GB))
        self.assertEqual(wsl.parse_size("2048"), 2048)
        self.assertIsNone(wsl.parse_size("lots"))
        self.assertIsNone(wsl.parse_size(None))

    def test_should_parse_wslconfig(self):
        parsed = wsl.parse_wslconfig(WSLCONFIG)
        self.assertEqual(parsed["memory_bytes"], 16 * GB)
        self.assertEqual(parsed["swap_bytes"], 12 * GB)
        self.assertEqual(parsed["processors"], 8)
        self.assertIsNone(parsed["auto_memory_reclaim"])

    def test_should_read_auto_memory_reclaim(self):
        parsed = wsl.parse_wslconfig("[experimental]\nautoMemoryReclaim=gradual\n")
        self.assertEqual(parsed["auto_memory_reclaim"], "gradual")
        self.assertIsNone(parsed["memory_bytes"])

    def test_should_return_empty_for_unparseable_file(self):
        self.assertEqual(wsl.parse_wslconfig("no section header"), {})


class FindAndLoadTests(SandboxTestCase):
    def test_should_use_override_path(self):
        cfg = self.tmp / "my.wslconfig"
        cfg.write_text(WSLCONFIG)
        os.environ["RESOURCE_GUARD_WSLCONFIG"] = str(cfg)
        self.assertEqual(wsl.find_wslconfig(runner=runner_for({})), cfg)

    def test_should_return_none_for_missing_override(self):
        os.environ["RESOURCE_GUARD_WSLCONFIG"] = str(self.tmp / "absent")
        self.assertIsNone(wsl.find_wslconfig(runner=runner_for({})))

    def test_should_resolve_profile_through_wslvar_and_wslpath(self):
        profile = self.tmp / "profile"
        profile.mkdir()
        (profile / ".wslconfig").write_text(WSLCONFIG)
        run = runner_for({"wslvar": (0, "C:\\Users\\me\r\n"), "wslpath": (0, f"{profile}\n")})
        self.assertEqual(wsl.find_wslconfig(runner=run), profile / ".wslconfig")
        self.assertEqual(run.calls[1], ["wslpath", "-u", "C:\\Users\\me"])

    def test_should_survive_missing_wslvar(self):
        run = runner_for({"wslvar": FileNotFoundError("wslvar")})
        result = wsl.find_wslconfig(runner=run)
        self.assertTrue(result is None or result.name == ".wslconfig")

    def test_should_cache_parse_by_mtime(self):
        cfg = self.tmp / "c.wslconfig"
        cfg.write_text(WSLCONFIG)
        os.environ["RESOURCE_GUARD_WSLCONFIG"] = str(cfg)
        first = wsl.load_wslconfig(runner=runner_for({}))
        cfg.write_text("[wsl2]\nmemory=4GB\n")
        os.utime(cfg, (1, 1))
        second = wsl.load_wslconfig(runner=runner_for({}))
        self.assertEqual(first["memory_bytes"], 16 * GB)
        self.assertEqual(second["memory_bytes"], 4 * GB)
        self.assertEqual(wsl.load_wslconfig(runner=runner_for({}))["path"], str(cfg))

    def test_should_return_empty_without_wslconfig(self):
        os.environ["RESOURCE_GUARD_WSLCONFIG"] = str(self.tmp / "absent")
        self.assertEqual(wsl.load_wslconfig(runner=runner_for({})), {})


class HostMemoryTests(unittest.TestCase):
    def test_should_parse_probe_output(self):
        run = runner_for({"powershell.exe": (0, "32960644 5932020 58377192 5894592 13027938304\r\n")})
        host = wsl.host_memory(runner=run, clock=lambda: 7.0)
        self.assertEqual(host, {"ts": 7.0, "total_kb": 32960644, "available_kb": 5932020,
                                "commit_total_kb": 58377192, "commit_free_kb": 5894592,
                                "vmmem_bytes": 13027938304})

    def test_should_accept_missing_vmmem(self):
        run = runner_for({"powershell.exe": (0, "100 50 200 20")})
        self.assertIsNone(wsl.host_memory(runner=run)["vmmem_bytes"])

    def test_should_return_none_on_failure_or_garbage(self):
        self.assertIsNone(wsl.host_memory(runner=runner_for({"powershell.exe": (1, "")})))
        self.assertIsNone(wsl.host_memory(runner=runner_for({"powershell.exe": (0, "a b c d")})))
        timeout = subprocess.TimeoutExpired("powershell.exe", 10)
        self.assertIsNone(wsl.host_memory(runner=runner_for({"powershell.exe": timeout})))


class DoctorAdviceTests(unittest.TestCase):
    def test_should_flag_oversized_memory_missing_reclaim_and_big_swap(self):
        cfg = {"memory_bytes": 28 * GB, "swap_bytes": 20 * GB, "auto_memory_reclaim": None}
        host = {"total_kb": 32 * 1024 * 1024}
        advice = wsl.doctor_advice(cfg, host, env={})
        self.assertEqual(len(advice), 5)
        self.assertIn("over 75% of host RAM", advice[0])
        self.assertIn("autoMemoryReclaim=gradual", advice[1])
        self.assertIn("swap=20.0 GB", advice[2])
        self.assertIn("wsl.exe --shutdown", advice[-1])

    def test_should_stay_quiet_for_sane_config(self):
        cfg = {"memory_bytes": 8 * GB, "swap_bytes": 2 * GB, "auto_memory_reclaim": "gradual"}
        advice = wsl.doctor_advice(cfg, {"total_kb": 32 * 1024 * 1024}, env={"CLAUDE_CODE_TOOL_MEMORY_LIMIT": "4g"})
        self.assertEqual(advice, [])


if __name__ == "__main__":
    unittest.main()
