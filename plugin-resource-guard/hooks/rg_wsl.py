"""
WSL awareness.

Inside WSL2 /proc only shows the utility VM: every distro (docker-desktop
included) shares it, and the VM itself is a guest that the Windows host can
run out of memory under. So the watchdog also asks Windows how much memory
and commit it has left, and `doctor` reads .wslconfig to explain the VM caps.

The PowerShell probe costs ~1.2 s, so only the watchdog makes it, on its own
schedule; hooks read the cached result in status.json. Nothing here ever
writes to the Windows side.
"""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import time
from pathlib import Path

import rg_common as common

WSLCONFIG_ENV = "RESOURCE_GUARD_WSLCONFIG"

# Win32_OperatingSystem: FreePhysicalMemory is "available" (standby cache
# included); TotalVirtualMemorySize/FreeVirtualMemory are the commit limit
# and what is left of it. The vmmem processes' working set is the VM's
# footprint as Windows sees it.
HOST_SCRIPT = (
    "$o=Get-CimInstance Win32_OperatingSystem;"
    "$v=(Get-Process -Name vmmemWSL,vmmem -ErrorAction SilentlyContinue | Measure-Object WorkingSet64 -Sum).Sum;"
    "'{0} {1} {2} {3} {4}' -f $o.TotalVisibleMemorySize,$o.FreePhysicalMemory,"
    "$o.TotalVirtualMemorySize,$o.FreeVirtualMemory,$v"
)

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def detect(env: dict | None = None) -> dict:
    """{'wsl': bool, 'version': 1|2|None, 'distro': str}."""
    env = os.environ if env is None else env
    try:
        release = (common.proc_root() / "sys" / "kernel" / "osrelease").read_text().strip().lower()
    except OSError:
        release = ""
    distro = env.get("WSL_DISTRO_NAME", "")
    if "microsoft" not in release and not distro:
        return {"wsl": False, "version": None, "distro": ""}
    version = 2 if ("wsl2" in release or "microsoft-standard" in release) else (1 if "microsoft" in release else None)
    return {"wsl": True, "version": version, "distro": distro}


def parse_size(text: str | None) -> int | None:
    """'16GB' / '512MB' / '2048' -> bytes (binary units, as WSL reads them)."""
    match = _SIZE.match(text or "")
    if not match:
        return None
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


def parse_wslconfig(text: str) -> dict:
    parser = configparser.ConfigParser(strict=False, interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        parser.read_string(text)
    except configparser.Error:
        return {}
    wsl2 = parser["wsl2"] if parser.has_section("wsl2") else {}
    experimental = parser["experimental"] if parser.has_section("experimental") else {}
    processors = wsl2.get("processors")
    return {
        "memory_bytes": parse_size(wsl2.get("memory")),
        "swap_bytes": parse_size(wsl2.get("swap")),
        "processors": int(processors) if processors and processors.strip().isdigit() else None,
        "auto_memory_reclaim": (experimental.get("autoMemoryReclaim") or wsl2.get("autoMemoryReclaim") or None),
    }


def _run(argv: list, runner, timeout: float) -> str | None:
    try:
        result = runner(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip().replace("\r", "") or None


def find_wslconfig(runner=subprocess.run) -> Path | None:
    override = os.environ.get(WSLCONFIG_ENV)
    if override:
        return Path(override) if Path(override).is_file() else None
    profile = _run(["wslvar", "USERPROFILE"], runner, 5)
    if profile:
        unix = _run(["wslpath", "-u", profile], runner, 5)
        if unix and (Path(unix) / ".wslconfig").is_file():
            return Path(unix) / ".wslconfig"
    for candidate in sorted(Path("/mnt/c/Users").glob("*/.wslconfig")) if Path("/mnt/c/Users").is_dir() else []:
        if candidate.parent.name not in ("Public", "Default", "All Users"):
            return candidate
    return None


def load_wslconfig(runner=subprocess.run) -> dict:
    """Parsed .wslconfig, cached in state by path and mtime (reading /mnt/c
    goes through 9P and is slow). {} when there is none."""
    cache_path = common.state_dir() / "wsl.json"
    cache = common.read_json(cache_path, {}) or {}
    path = Path(cache["path"]) if cache.get("path") else find_wslconfig(runner)
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        common.atomic_write_json(cache_path, {})
        return {}
    if cache.get("path") == str(path) and cache.get("mtime") == mtime:
        return cache.get("parsed", {})
    try:
        parsed = parse_wslconfig(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}
    parsed["path"] = str(path)
    common.atomic_write_json(cache_path, {"path": str(path), "mtime": mtime, "parsed": parsed})
    return parsed


def host_memory(runner=subprocess.run, timeout: float = 10, clock=time.time) -> dict | None:
    """Windows memory as seen from the host, or None when the probe fails."""
    out = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", HOST_SCRIPT], runner, timeout)
    if not out:
        return None
    parts = out.split()
    if len(parts) < 4 or not all(p.isdigit() for p in parts[:4]):
        return None
    vmmem = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
    return {
        "ts": clock(),
        "total_kb": int(parts[0]),
        "available_kb": int(parts[1]),
        "commit_total_kb": int(parts[2]),
        "commit_free_kb": int(parts[3]),
        "vmmem_bytes": vmmem,
    }


def _gb(value: float) -> str:
    return f"{value / 1024**3:.1f} GB"


def doctor_advice(wslcfg: dict, host: dict | None, env: dict | None = None) -> list:
    """Advice only. Every .wslconfig change needs `wsl.exe --shutdown`, which
    kills every running distro, so the guard prints it and never runs it."""
    env = os.environ if env is None else env
    advice = []
    memory = wslcfg.get("memory_bytes")
    swap = wslcfg.get("swap_bytes")
    host_total = host["total_kb"] * 1024 if host else None
    if memory and host_total and memory > host_total * 0.75:
        advice.append(
            f".wslconfig memory={_gb(memory)} is over 75% of host RAM ({_gb(host_total)}); "
            "Windows itself can run short before the VM notices"
        )
    if memory and not wslcfg.get("auto_memory_reclaim"):
        advice.append("set [experimental] autoMemoryReclaim=gradual so freed VM memory returns to Windows")
    if memory and swap and swap > memory / 2:
        advice.append(
            f".wslconfig swap={_gb(swap)} is more than half of memory; swap lives on a VHD, "
            "so a large swap turns memory pressure into long disk stalls"
        )
    if not env.get("CLAUDE_CODE_TOOL_MEMORY_LIMIT"):
        advice.append(
            "CLAUDE_CODE_TOOL_MEMORY_LIMIT caps Bash/Monitor memory per session, but it needs cgroup "
            "delegation ([boot] systemd=true in /etc/wsl.conf) and never covers Docker"
        )
    if advice:
        advice.append("apply .wslconfig changes with `wsl.exe --shutdown` (stops ALL distros; run it yourself)")
    return advice
