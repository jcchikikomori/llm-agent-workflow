"""
Load sampling and level classification.

Signals: /proc/meminfo (MemAvailable, swap) and PSI (/proc/pressure/*). PSI
stall % is computed from deltas of the cumulative `total=` counter between
two samples, because avg10 lags ~10 s behind a thrash that is already
happening. Without a previous sample (a hook's one-shot read) avg10 is used.

Levels: ok < elevated < critical < hard. `classify` maps one set of metrics
to a raw tier; `LevelFSM` adds the hysteresis the watchdog acts on:
elevated and hard apply at once, critical needs N consecutive samples, and
stepping down needs M calm samples in a row.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import rg_common as common

PSI_RESOURCES = ("memory", "io", "cpu")

# Metric name -> (threshold key suffix, comparison). "below" trips when the
# metric is under the threshold, "at_least" when it is at or over it.
METRIC_RULES = {
    "mem_available_pct": "below",
    "swap_free_pct": "below",
    "psi_memory_some": "at_least",
    "psi_memory_full": "at_least",
    "psi_io_full": "at_least",
    "psi_cpu_some": "at_least",
    "host_available_pct": "below",
    "host_commit_free_pct": "below",
}

# CPU contention slows everything down but doesn't hang the machine the way
# memory thrash does, so it can raise the level to elevated and no higher.
CPU_MAX_LEVEL = "elevated"


@dataclass
class Sample:
    ts: float
    meminfo: dict = field(default_factory=dict)
    psi: dict = field(default_factory=dict)
    host: dict | None = None


def read_meminfo(root: Path) -> dict:
    """Return /proc/meminfo as {key: kB}."""
    values = {}
    try:
        text = (Path(root) / "meminfo").read_text()
    except OSError:
        return values
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key.strip()] = int(parts[0])
    return values


def read_psi(root: Path, resource: str) -> dict | None:
    """Return {'some': {'avg10': f, 'total': i}, 'full': {...}} or None when
    the kernel has no PSI (WSL1, old kernels, CONFIG_PSI=n)."""
    try:
        text = (Path(root) / "pressure" / resource).read_text()
    except OSError:
        return None
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        row = {}
        for item in parts[1:]:
            key, _, value = item.partition("=")
            try:
                row[key] = float(value) if key.startswith("avg") else int(value)
            except ValueError:
                continue
        result[parts[0]] = row
    return result or None


def take_sample(root: Path | None = None, host: dict | None = None, clock=time.time) -> Sample:
    root = Path(root) if root is not None else common.proc_root()
    psi = {}
    for resource in PSI_RESOURCES:
        data = read_psi(root, resource)
        if data is not None:
            psi[resource] = data
    return Sample(ts=clock(), meminfo=read_meminfo(root), psi=psi, host=host)


def stall_pct(prev: Sample | None, cur: Sample, resource: str, kind: str) -> float | None:
    """Percent of wall time stalled since prev, from the PSI total counter
    (microseconds). Falls back to avg10 without a usable previous sample."""
    row = cur.psi.get(resource, {}).get(kind)
    if not row:
        return None
    if prev is not None:
        prev_row = prev.psi.get(resource, {}).get(kind)
        elapsed_us = (cur.ts - prev.ts) * 1_000_000
        if prev_row and "total" in prev_row and "total" in row and elapsed_us > 0:
            delta = row["total"] - prev_row["total"]
            if delta >= 0:
                return min(100.0, delta / elapsed_us * 100.0)
    avg = row.get("avg10")
    return float(avg) if avg is not None else None


def _pct(part: int | None, whole: int | None) -> float | None:
    if part is None or not whole:
        return None
    return part / whole * 100.0


def metrics(cur: Sample, prev: Sample | None = None) -> dict:
    """Flatten a sample into the metric names thresholds are written against.
    A metric the machine can't provide is None and never trips anything."""
    mem = cur.meminfo
    host = cur.host or {}
    return {
        "mem_available_pct": _pct(mem.get("MemAvailable"), mem.get("MemTotal")),
        "swap_free_pct": _pct(mem.get("SwapFree"), mem.get("SwapTotal")),
        "psi_memory_some": stall_pct(prev, cur, "memory", "some"),
        "psi_memory_full": stall_pct(prev, cur, "memory", "full"),
        "psi_io_full": stall_pct(prev, cur, "io", "full"),
        "psi_cpu_some": stall_pct(prev, cur, "cpu", "some"),
        "host_available_pct": _pct(host.get("available_kb"), host.get("total_kb")),
        "host_commit_free_pct": _pct(host.get("commit_free_kb"), host.get("commit_total_kb")),
    }


def thresholds_for(cfg: dict, wsl: bool) -> dict:
    thresholds = cfg.get("thresholds", {})
    if wsl:
        thresholds = common.deep_merge(thresholds, cfg.get("wsl_thresholds", {}))
    return thresholds


def _trips(metric: str, value: float | None, tier_rules: dict) -> bool:
    if value is None:
        return False
    rule = METRIC_RULES.get(metric)
    limit = tier_rules.get(f"{metric}_{rule}") if rule else None
    if limit is None:
        return False
    return value < limit if rule == "below" else value >= limit


def classify(values: dict, thresholds: dict) -> tuple:
    """Return (raw tier, reasons) for one set of metrics. reasons lists the
    metrics that tripped the returned tier, e.g. 'mem_available_pct=8.1'."""
    for tier in ("hard", "critical", "elevated"):
        tier_rules = thresholds.get(tier, {})
        reasons = []
        for metric, value in values.items():
            if metric == "psi_cpu_some" and common.level_index(tier) > common.level_index(CPU_MAX_LEVEL):
                continue
            if _trips(metric, value, tier_rules):
                reasons.append(f"{metric}={value:.1f}")
        if reasons:
            return tier, reasons
    return "ok", []


class LevelFSM:
    """Hysteresis over raw tiers.

    - elevated and hard take effect on the first sample;
    - critical needs `critical_samples` consecutive samples at critical or
      above (a single spike doesn't freeze anybody);
    - dropping a level needs `calm_samples` consecutive samples below it,
      and lands on the highest tier seen during those samples (hard, then
      critical, critical, ok steps down to critical, not to ok).
    """

    def __init__(self, critical_samples: int = 2, calm_samples: int = 3):
        self.critical_samples = max(1, critical_samples)
        self.calm_samples = max(1, calm_samples)
        self.level = "ok"
        self._critical_run = 0
        self._calm_run = 0
        self._calm_peak = 0

    def update(self, raw: str) -> str:
        raw_idx = common.level_index(raw)
        critical_idx = common.level_index("critical")
        self._critical_run = self._critical_run + 1 if raw_idx >= critical_idx else 0

        cur_idx = common.level_index(self.level)
        if raw == "critical" and cur_idx < critical_idx and self._critical_run < self.critical_samples:
            # Stepping up to critical needs confirmation; staying there doesn't.
            raw, raw_idx = "elevated", common.level_index("elevated")

        if raw_idx >= cur_idx:
            self.level = raw
            self._calm_run = self._calm_peak = 0
        else:
            self._calm_run += 1
            self._calm_peak = max(self._calm_peak, raw_idx)
            if self._calm_run >= self.calm_samples:
                self.level = common.LEVELS[self._calm_peak]
                self._calm_run = self._calm_peak = 0
        return self.level


def status_path() -> Path:
    return common.state_dir() / "status.json"


def current_level(cfg: dict, wsl: bool = False, clock=time.time) -> tuple:
    """The level a hook should act on: the watchdog's (hysteresis applied,
    host metrics included) when status.json is fresh, else a direct one-shot
    read of meminfo and PSI. Returns (level, reasons, source)."""
    status = common.read_json(status_path(), None)
    max_age = cfg.get("status_max_age_seconds", 15)
    if isinstance(status, dict) and clock() - status.get("ts", 0) <= max_age:
        return status.get("level", "ok"), status.get("reasons", []), "watchdog"
    sample = take_sample(clock=clock)
    level, reasons = classify(metrics(sample), thresholds_for(cfg, wsl))
    return level, reasons, "direct"
