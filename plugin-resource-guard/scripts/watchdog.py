#!/usr/bin/env python3
"""
resource-guard watchdog: one per machine, started by the SessionStart hook.

Each tick it samples load, applies hysteresis, writes status.json for the
hooks and the CLI, and acts:

- hard: freeze every background session at once;
- critical: freeze one background session, let it settle, and freeze the
  next only while memory stall is still high or MemAvailable still falls
  (freezing frees no memory: if memory is low but flat and calm, the rest is
  held by idle servers and freezing more would only hurt);
- calm again: resume one session at a time, the foreground first, backing
  off a session that tipped the machine over right after its last resume.

The foreground session (the one the user last typed in) is never frozen.
On exit, by signal or idleness, everything it froze is resumed.
"""

from __future__ import annotations

import dataclasses
import fcntl
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import rg_actions as actions  # noqa: E402
import rg_common as common  # noqa: E402
import rg_docker as docker_mod  # noqa: E402
import rg_pressure as pressure  # noqa: E402
import rg_procs as procs_mod  # noqa: E402
import rg_sessions as sessions_mod  # noqa: E402
import rg_watchdog_ctl as ctl  # noqa: E402
import rg_wsl as wsl_mod  # noqa: E402

SCAN_EVERY = 30.0
DOCKER_EVERY = 60.0
BACKOFF_WINDOW = 300.0
MEM_FALL_TOLERANCE = 1.0
MAX_FAILED_TICKS = 5


class Watchdog:
    def __init__(self, *, sampler=pressure.take_sample, scanner=procs_mod.scan, docker_factory=docker_mod.client,
                 host_probe=wsl_mod.host_memory, wsl_info=None, sender=None, config_loader=common.load_config):
        self.sampler = sampler
        self.scanner = scanner
        self.docker_factory = docker_factory
        self.host_probe = host_probe
        self.wsl = wsl_info if wsl_info is not None else wsl_mod.detect()
        self.sender = sender
        self.config_loader = config_loader
        self.cfg = config_loader()
        self.fsm = pressure.LevelFSM(self.cfg.get("critical_samples", 2), self.cfg.get("calm_samples", 3))
        self.prev_sample = None
        self.host = None
        self.host_ts = 0.0
        self.history = deque(maxlen=max(1, self.cfg.get("calm_samples", 3)))
        self.last_scan = 0.0
        self.last_docker = 0.0
        self.scan_cache = {"sessions": [], "procs": {}, "work": {}, "containers": [], "attrs": {}, "rss": {}}
        self.prev_rss = {}
        self.last_freeze = 0.0
        self.last_resume = 0.0
        self.episode_active = False
        self.episode_mem = None
        self.episode_flagged = False
        self.resumed_at = {}
        self.backoff = {}
        self.would_frozen = set()
        self.no_sessions_since = None

    # -- sampling -----------------------------------------------------------

    def _host_due(self, level: str, now: float) -> bool:
        host_cfg = self.cfg.get("host_check", {})
        if not (self.wsl.get("wsl") and host_cfg.get("enabled", True)):
            return False
        every = host_cfg.get("elevated_interval_seconds", 20) if common.level_index(level) >= 1 \
            else host_cfg.get("interval_seconds", 60)
        return now - self.host_ts >= every

    def _sample(self, now: float) -> tuple:
        if self._host_due(self.fsm.level, now):
            self.host_ts = now
            probed = self.host_probe(timeout=self.cfg.get("host_check", {}).get("timeout_seconds", 10))
            self.host = probed or self.host
        sample = self.sampler(host=self.host)
        values = pressure.metrics(sample, self.prev_sample)
        self.prev_sample = sample
        raw, reasons = pressure.classify(values, pressure.thresholds_for(self.cfg, self.wsl.get("wsl", False)))
        level = self.fsm.update(raw)
        self.history.append(values)
        return level, raw, reasons, values

    def _scan(self, level: str, now: float, docker) -> dict:
        busy = common.level_index(level) >= 1 or actions.frozen_keys() or self.would_frozen
        if not busy and now - self.last_scan < SCAN_EVERY:
            return self.scan_cache
        self.last_scan = now
        procs = self.scanner()
        sessions = sessions_mod.cc_sessions(procs)
        self_pids = procs_mod.self_and_ancestors()
        work = {s.key: procs_mod.work_pids(procs, s.pid, self.cfg, self_pids=self_pids, session_start=s.start)
                for s in sessions}
        cache = dict(self.scan_cache, sessions=sessions, procs=procs, work=work)
        if docker is None:
            # No daemon now: what an earlier tick listed can't be paused.
            cache["containers"], cache["attrs"] = [], {}
        elif busy or now - self.last_docker >= DOCKER_EVERY:
            self.last_docker = now
            try:
                containers = docker.list_containers()
                cache["containers"] = containers
                cache["attrs"] = docker_mod.attribute(containers, sessions, procs, work)
            except docker_mod.DockerUnavailable as exc:
                print(f"docker unavailable: {exc}", flush=True)
        self.prev_rss = cache.get("rss", {})
        cache["rss"] = {s.key: sum(procs[p].rss_kb for p in work[s.key] if p in procs) for s in sessions}
        self.scan_cache = cache
        return cache

    # -- decisions ----------------------------------------------------------

    def _candidates(self, cache: dict, protected: set) -> list:
        frozen = actions.frozen_keys() | self.would_frozen
        # target_pids narrows who may be frozen (smoke tests on a live
        # machine); it never changes who counts as foreground.
        allowed = set(self.cfg.get("target_pids") or [])
        out = []
        for session in cache["sessions"]:
            if session.key in protected or session.key in frozen or (allowed and session.pid not in allowed):
                continue
            owned = [c for c in cache["containers"] if cache["attrs"].get(c.id) and
                     cache["attrs"][c.id].owner == session.key]
            if not cache["work"].get(session.key) and not owned:
                continue
            growth = cache["rss"].get(session.key, 0) - self.prev_rss.get(session.key, 0)
            out.append((growth, cache["rss"].get(session.key, 0), session))
        out.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in out]

    def _foreground(self, sessions: list, now: float) -> set:
        """Foreground with prompt times read again: the user may have typed in
        a session since the last scan."""
        times = sessions_mod.prompt_times()
        fresh = [dataclasses.replace(s, last_prompt_at=times.get(s.key, s.last_prompt_at)) for s in sessions]
        return sessions_mod.foreground(fresh, now, self.cfg.get("foreground_grace_seconds", 60))

    def _freeze(self, session, cache: dict, docker, reason: str, together: set, now: float) -> None:
        """`together`: keys frozen in this same round. A container another
        session also uses is paused only when that session is frozen too."""
        targets = {session.key} | set(together) | actions.frozen_keys()
        mode = self.cfg.get("freeze_mode", "observe")

        def still_target() -> bool:
            return session.key not in self._foreground(cache["sessions"], now)

        try:
            result = actions.freeze_session(session, cfg=self.cfg, targets=targets, procs=cache["procs"],
                                            docker=docker, containers=cache["containers"], attrs=cache["attrs"],
                                            mode=mode, reason=reason, sender=self.sender, lock_timeout=10,
                                            still_target=still_target)
        except common.LockTimeout:
            return
        if mode != "enforce" and (result["procs"] or result["containers"]):
            self.would_frozen.add(session.key)

    def _calm(self) -> bool:
        if len(self.history) < self.history.maxlen:
            return False
        critical = self.cfg["thresholds"].get("critical", {})
        hard = self.cfg["thresholds"].get("hard", {})
        psi_limit = critical.get("psi_memory_full_at_least", 10)
        mem_floor = hard.get("mem_available_pct_below", 5)
        for values in self.history:
            psi = values.get("psi_memory_full")
            mem = values.get("mem_available_pct")
            if psi is not None and psi >= psi_limit:
                return False
            if mem is not None and mem <= mem_floor:
                return False
        first, last = self.history[0].get("mem_available_pct"), self.history[-1].get("mem_available_pct")
        return first is None or last is None or last - first >= -MEM_FALL_TOLERANCE

    def _resume_one(self, protected: set, docker, now: float) -> None:
        frozen = sorted(actions.frozen_keys() | self.would_frozen)
        ready = [key for key in frozen if self.backoff.get(key, (0, 0.0))[1] <= now]
        if not ready:
            return
        # Foreground first, then whoever waited longest: a session whose
        # containers can't be unpaused yet must not block everybody else.
        key = min(ready, key=lambda k: (k not in protected, self.resumed_at.get(k, 0.0), k))
        self.last_resume = now
        self.resumed_at[key] = now
        if key in self.would_frozen:
            self.would_frozen.discard(key)
            common.append_event("would-resume", session=key)
            return
        try:
            actions.resume_session(key, docker=docker, sender=self.sender, reason="calm", lock_timeout=10)
        except common.LockTimeout:
            pass

    def _resume_dead(self, cache: dict, docker) -> None:
        """A frozen session whose claude process is gone: let its leftovers
        run (they get their SIGHUP and exit) instead of staying stopped."""
        live = {s.key for s in cache["sessions"]}
        for key in actions.frozen_keys() - live:
            pid, start = common.parse_key(key)
            if procs_mod.start_of(pid) != start:
                try:
                    actions.resume_session(key, docker=docker, sender=self.sender, reason="session-gone",
                                           lock_timeout=10)
                except common.LockTimeout:
                    pass
        self.would_frozen &= live

    def _process_requests(self, docker) -> None:
        directory = common.state_dir() / "requests"
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            request = common.read_json(path, {})
            request = request if isinstance(request, dict) else {}
            if request.get("kind") in ("resume", "end") and request.get("key"):
                self.would_frozen.discard(request["key"])
                try:
                    actions.resume_session(request["key"], docker=docker, sender=self.sender,
                                           reason=request["kind"], lock_timeout=10)
                except common.LockTimeout:
                    continue  # keep the request for the next tick
            try:
                path.unlink()
            except OSError:
                pass

    def _note_transition(self, level: str, now: float) -> None:
        critical = common.level_index(level) >= common.level_index("critical")
        if critical and not self.episode_active:
            self.episode_active, self.episode_mem, self.episode_flagged = True, None, False
            delays = self.cfg.get("resume_backoff_seconds", [60, 120, 240]) or [60]
            for key, ts in list(self.resumed_at.items()):
                if now - ts < BACKOFF_WINDOW:
                    count = self.backoff.get(key, (0, 0.0))[0]
                    self.backoff[key] = (count + 1, now + delays[min(count, len(delays) - 1)])
        elif not critical:
            self.episode_active = False

    def _act(self, level: str, values: dict, cache: dict, docker, now: float) -> None:
        protected = self._foreground(cache["sessions"], now)
        if level == "hard":
            candidates = self._candidates(cache, protected)
            together = {s.key for s in candidates}
            for session in candidates:
                self._freeze(session, cache, docker, "hard", together, now)
            self.last_freeze = now
            return
        if level == "critical":
            if now - self.last_freeze < self.cfg.get("freeze_settle_seconds", 10):
                return
            mem = values.get("mem_available_pct")
            psi = values.get("psi_memory_full")
            psi_limit = self.cfg["thresholds"].get("critical", {}).get("psi_memory_full_at_least", 10)
            still_bad = self.episode_mem is None or (psi is not None and psi >= psi_limit) or \
                (mem is not None and mem < self.episode_mem - MEM_FALL_TOLERANCE)
            candidates = self._candidates(cache, protected)
            if still_bad and candidates:
                self._freeze(candidates[0], cache, docker, "critical", set(), now)
                self.last_freeze, self.episode_mem = now, mem if mem is not None else self.episode_mem
            elif not self.episode_flagged:
                # Either nothing is left to freeze, or memory is low but no
                # longer falling: what holds it is idle (servers, caches), and
                # freezing more sessions would only cost the user work.
                self.episode_flagged = True
                common.append_event("held-by-idle" if candidates else "nothing-to-freeze", level=level)
            return
        if (actions.frozen_keys() or self.would_frozen) and self._calm() and \
                now - self.last_resume >= self.cfg.get("resume_settle_seconds", 20):
            self._resume_one(protected, docker, now)

    # -- tick ---------------------------------------------------------------

    def tick(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self.cfg = self.config_loader()
        docker = self.docker_factory()
        if common.disabled(self.cfg):
            actions.resume_all(docker=docker, sender=self.sender, reason="disabled")
            return {"exit": "disabled"}
        level, raw, reasons, values = self._sample(now)
        self._note_transition(level, now)
        self._process_requests(docker)
        cache = self._scan(level, now, docker)
        self._resume_dead(cache, docker)
        self._act(level, values, cache, docker, now)

        if cache["sessions"]:
            self.no_sessions_since = None
        elif self.no_sessions_since is None:
            self.no_sessions_since = now
        status = self._status(now, level, raw, reasons, values, cache)
        common.atomic_write_json(pressure.status_path(), status)
        if self.no_sessions_since is not None and now - self.no_sessions_since >= self.cfg.get("idle_exit_seconds", 120):
            status["exit"] = "idle"
        return status

    def _status(self, now, level, raw, reasons, values, cache) -> dict:
        protected = sessions_mod.foreground(cache["sessions"], now, self.cfg.get("foreground_grace_seconds", 60))
        frozen = actions.frozen_keys()
        rows = []
        for session in cache["sessions"]:
            procs = cache["procs"]
            owned = [c for c in cache["containers"] if cache["attrs"].get(c.id) and
                     cache["attrs"][c.id].owner == session.key]
            rows.append({
                "key": session.key,
                "pid": session.pid,
                "session_id": session.session_id[:8],
                "cwd": session.cwd,
                "kind": session.kind,
                "foreground": session.key in protected,
                "frozen": session.key in frozen,
                "would_freeze": session.key in self.would_frozen,
                "tree_rss_kb": procs_mod.tree_rss_kb(procs, session.pid) if procs else 0,
                "work_rss_kb": cache["rss"].get(session.key, 0),
                "work_procs": len(cache["work"].get(session.key, [])),
                "containers": [{"name": c.name, "state": c.state, "mem_bytes": docker_mod.cgroup_mem(c.id)}
                               for c in owned],
            })
        return {
            "ts": now,
            "pid": os.getpid(),
            "level": level,
            "raw": raw,
            "reasons": reasons,
            "metrics": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in values.items()},
            "host": self.host,
            "wsl": self.wsl,
            "freeze_mode": self.cfg.get("freeze_mode", "observe"),
            "sessions": rows,
            "frozen": sorted(frozen),
        }

    def interval(self) -> float:
        intervals = self.cfg.get("interval_seconds", {})
        key = "elevated" if common.level_index(self.fsm.level) >= 1 else "ok"
        return float(intervals.get(key, 2 if key == "elevated" else 5))


def acquire_singleton():
    fd = os.open(str(ctl.lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _exit_on_signal(signum, frame):
    raise SystemExit(0)


EXIT_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def run(watchdog: Watchdog | None = None, max_ticks: int | None = None, sleep=time.sleep) -> int:
    fd = acquire_singleton()
    if fd is None:
        return 0
    os.utime(ctl.lock_path())  # "just started", for the hung check
    previous = {signum: signal.getsignal(signum) for signum in EXIT_SIGNALS}
    for signum in EXIT_SIGNALS:
        signal.signal(signum, _exit_on_signal)
    clean = False
    try:
        watchdog = watchdog or Watchdog()
        if actions.frozen_keys():
            actions.resume_all(docker=watchdog.docker_factory(), sender=watchdog.sender, reason="watchdog-start")
        print(f"watchdog {os.getpid()} started", flush=True)
        ticks, failed, status = 0, 0, {}
        try:
            while max_ticks is None or ticks < max_ticks:
                ticks += 1
                try:
                    status = watchdog.tick()
                    failed = 0
                except Exception as exc:  # one bad tick must not leave sessions frozen
                    print(f"tick failed: {type(exc).__name__}: {exc}", flush=True)
                    failed += 1
                    # A tick that always fails keeps the lock but no heartbeat:
                    # hooks would call it hung and never take over. Step aside.
                    status = {"exit": "failing"} if failed >= MAX_FAILED_TICKS else {}
                if status.get("exit"):
                    print(f"watchdog exiting: {status['exit']}", flush=True)
                    break
                sleep(watchdog.interval())
        except SystemExit:
            print("watchdog exiting: signal", flush=True)
        # A failing exit stays in the spawn history, so a watchdog that can
        # never tick ends in "gave_up" instead of respawning forever.
        clean = status.get("exit") != "failing"
    finally:
        # A second TERM must not cut the final resume short.
        for signum in EXIT_SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        try:
            actions.resume_all(docker=docker_mod.client(), sender=watchdog.sender if watchdog else None,
                               reason="watchdog-exit")
        finally:
            if clean:
                ctl.clear_spawn_history()
            os.close(fd)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    sys.exit(run())
