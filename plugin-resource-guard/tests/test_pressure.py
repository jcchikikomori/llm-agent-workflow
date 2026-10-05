from __future__ import annotations

import time
import unittest

from fixtures import SandboxTestCase

import rg_common as common
import rg_pressure as pressure

DEFAULT_THRESHOLDS = None


def thresholds():
    return pressure.thresholds_for(common.load_config(), wsl=False)


class SamplingTests(SandboxTestCase):
    def test_should_read_meminfo_in_kb(self):
        self.proc.meminfo(total=16_000_000, available=4_000_000)
        values = pressure.read_meminfo(self.proc.root)
        self.assertEqual(values["MemTotal"], 16_000_000)
        self.assertEqual(values["MemAvailable"], 4_000_000)

    def test_should_return_empty_meminfo_when_missing(self):
        self.assertEqual(pressure.read_meminfo(self.tmp / "none"), {})

    def test_should_parse_psi_some_and_full(self):
        self.proc.psi("memory", some_avg10=1.5, some_total=10, full_avg10=0.5, full_total=4)
        data = pressure.read_psi(self.proc.root, "memory")
        self.assertEqual(data["some"], {"avg10": 1.5, "avg60": 0.0, "avg300": 0.0, "total": 10})
        self.assertEqual(data["full"]["total"], 4)

    def test_should_return_none_without_psi(self):
        self.assertIsNone(pressure.read_psi(self.proc.root, "memory"))

    def test_should_skip_malformed_psi_values(self):
        (self.proc.root / "pressure").mkdir()
        (self.proc.root / "pressure" / "cpu").write_text("some avg10=x total=5\n\n")
        self.assertEqual(pressure.read_psi(self.proc.root, "cpu"), {"some": {"total": 5}})

    def test_should_take_sample_from_proc_root_override(self):
        self.proc.meminfo()
        self.proc.psi("io", full_avg10=3.0)
        sample = pressure.take_sample(clock=lambda: 100.0)
        self.assertEqual(sample.ts, 100.0)
        self.assertIn("io", sample.psi)
        self.assertNotIn("cpu", sample.psi)


class StallTests(unittest.TestCase):
    def sample(self, ts, total, avg10=0.0):
        return pressure.Sample(ts=ts, psi={"memory": {"full": {"avg10": avg10, "total": total}}})

    def test_should_compute_stall_from_total_delta(self):
        prev, cur = self.sample(10.0, 0), self.sample(12.0, 500_000)
        self.assertAlmostEqual(pressure.stall_pct(prev, cur, "memory", "full"), 25.0)

    def test_should_cap_stall_at_100(self):
        prev, cur = self.sample(10.0, 0), self.sample(11.0, 5_000_000)
        self.assertEqual(pressure.stall_pct(prev, cur, "memory", "full"), 100.0)

    def test_should_fall_back_to_avg10_without_previous(self):
        self.assertEqual(pressure.stall_pct(None, self.sample(1.0, 9, avg10=7.5), "memory", "full"), 7.5)

    def test_should_fall_back_to_avg10_when_counter_went_backwards(self):
        prev, cur = self.sample(10.0, 900), self.sample(12.0, 100, avg10=2.0)
        self.assertEqual(pressure.stall_pct(prev, cur, "memory", "full"), 2.0)

    def test_should_return_none_for_missing_resource(self):
        self.assertIsNone(pressure.stall_pct(None, self.sample(1.0, 0), "io", "full"))


class MetricTests(unittest.TestCase):
    def test_should_compute_percentages_and_host_metrics(self):
        sample = pressure.Sample(
            ts=1.0,
            meminfo={"MemTotal": 1000, "MemAvailable": 150, "SwapTotal": 200, "SwapFree": 50},
            host={"total_kb": 100, "available_kb": 12, "commit_total_kb": 400, "commit_free_kb": 40},
        )
        values = pressure.metrics(sample)
        self.assertEqual(values["mem_available_pct"], 15.0)
        self.assertEqual(values["swap_free_pct"], 25.0)
        self.assertEqual(values["host_available_pct"], 12.0)
        self.assertEqual(values["host_commit_free_pct"], 10.0)
        self.assertIsNone(values["psi_memory_full"])

    def test_should_leave_swap_none_without_swap(self):
        sample = pressure.Sample(ts=1.0, meminfo={"MemTotal": 10, "MemAvailable": 5, "SwapTotal": 0, "SwapFree": 0})
        self.assertIsNone(pressure.metrics(sample)["swap_free_pct"])


class ClassifyTests(SandboxTestCase):
    def values(self, **kw):
        base = {name: None for name in pressure.METRIC_RULES}
        base.update(kw)
        return base

    def test_should_be_ok_when_nothing_trips(self):
        self.assertEqual(pressure.classify(self.values(mem_available_pct=50.0), thresholds()), ("ok", []))

    def test_should_trip_elevated_just_below_boundary(self):
        level, reasons = pressure.classify(self.values(mem_available_pct=19.9), thresholds())
        self.assertEqual(level, "elevated")
        self.assertEqual(reasons, ["mem_available_pct=19.9"])

    def test_should_not_trip_at_exact_below_boundary(self):
        self.assertEqual(pressure.classify(self.values(mem_available_pct=20.0), thresholds())[0], "ok")

    def test_should_trip_at_least_rules_on_the_boundary(self):
        self.assertEqual(pressure.classify(self.values(psi_memory_full=10.0), thresholds())[0], "critical")
        self.assertEqual(pressure.classify(self.values(psi_memory_full=9.9), thresholds())[0], "ok")

    def test_should_pick_highest_tier(self):
        level, _ = pressure.classify(self.values(mem_available_pct=4.0, psi_memory_some=50.0), thresholds())
        self.assertEqual(level, "hard")

    def test_should_cap_cpu_at_elevated(self):
        rules = {"hard": {"psi_cpu_some_at_least": 50}, "elevated": {"psi_cpu_some_at_least": 50}}
        self.assertEqual(pressure.classify(self.values(psi_cpu_some=99.0), rules)[0], "elevated")

    def test_should_trip_on_host_metrics(self):
        level, reasons = pressure.classify(self.values(host_commit_free_pct=1.5), thresholds())
        self.assertEqual(level, "hard")
        self.assertEqual(reasons, ["host_commit_free_pct=1.5"])

    def test_should_apply_wsl_io_threshold(self):
        cfg = common.load_config()
        values = self.values(psi_io_full=16.0)
        self.assertEqual(pressure.classify(values, pressure.thresholds_for(cfg, wsl=False))[0], "ok")
        self.assertEqual(pressure.classify(values, pressure.thresholds_for(cfg, wsl=True))[0], "elevated")


class FsmTests(unittest.TestCase):
    def run_fsm(self, raws, critical_samples=2, calm_samples=3):
        fsm = pressure.LevelFSM(critical_samples, calm_samples)
        return [fsm.update(raw) for raw in raws]

    def test_should_raise_elevated_immediately(self):
        self.assertEqual(self.run_fsm(["elevated"]), ["elevated"])

    def test_should_need_two_samples_for_critical(self):
        self.assertEqual(self.run_fsm(["critical", "critical"]), ["elevated", "critical"])

    def test_should_go_hard_immediately(self):
        self.assertEqual(self.run_fsm(["ok", "hard"]), ["ok", "hard"])

    def test_should_need_three_calm_samples_to_drop(self):
        levels = self.run_fsm(["hard", "ok", "ok", "ok"])
        self.assertEqual(levels, ["hard", "hard", "hard", "ok"])

    def test_should_step_down_to_the_highest_tier_seen_while_calming(self):
        levels = self.run_fsm(["hard", "critical", "critical", "ok", "ok", "ok", "ok"])
        self.assertEqual(levels, ["hard", "hard", "hard", "critical", "critical", "critical", "ok"])

    def test_should_reset_calm_run_on_flap(self):
        levels = self.run_fsm(["critical", "critical", "ok", "ok", "critical", "ok", "ok", "ok"])
        self.assertEqual(levels[-4:], ["critical", "critical", "critical", "ok"])

    def test_should_not_confirm_critical_after_single_spike(self):
        self.assertEqual(self.run_fsm(["critical", "ok"]), ["elevated", "elevated"])


class CurrentLevelTests(SandboxTestCase):
    def test_should_use_fresh_watchdog_status(self):
        common.atomic_write_json(pressure.status_path(), {"ts": 1000.0, "level": "critical", "reasons": ["x=1"]})
        level = pressure.current_level(common.load_config(), clock=lambda: 1010.0)
        self.assertEqual(level, ("critical", ["x=1"], "watchdog"))

    def test_should_sample_directly_when_status_stale(self):
        common.atomic_write_json(pressure.status_path(), {"ts": 1000.0, "level": "critical"})
        self.proc.meminfo(total=100, available=8)
        level, reasons, source = pressure.current_level(common.load_config(), clock=lambda: 1100.0)
        self.assertEqual((level, source), ("critical", "direct"))
        self.assertEqual(reasons, ["mem_available_pct=8.0"])

    def test_should_be_ok_with_no_data_at_all(self):
        self.assertEqual(pressure.current_level(common.load_config(), clock=time.time)[0], "ok")


if __name__ == "__main__":
    unittest.main()
