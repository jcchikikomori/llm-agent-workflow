#!/usr/bin/env python3
"""
resource-guard CLI. Works from any terminal, with no Claude Code session
needed, so it is the way out when sessions stop responding.

SessionStart keeps a stable link to this script at
~/.claude/.resource-guard/bin/resource-guard. From Windows, while the WSL
shell itself is unusable, call it by its absolute path (`wsl.exe -e` runs no
shell, so nothing expands `~`); `doctor` prints the exact line:

    wsl.exe -d <distro> -e /home/<user>/.claude/.resource-guard/bin/resource-guard resume --all
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
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

# Seams for tests: signal sender and sleep.
SENDER = None
SLEEP = time.sleep
STOP_GRACE = 3.0


def fmt_kb(kb: float | None) -> str:
    if kb is None:
        return "-"
    for unit, size in (("GB", 1024 * 1024), ("MB", 1024)):
        if kb >= size:
            return f"{kb / size:.1f} {unit}"
    return f"{int(kb)} KB"


def live_view(cfg: dict) -> dict:
    procs = procs_mod.scan()
    sessions = sessions_mod.cc_sessions(procs)
    # Run from a Bash tool, this CLI's own shell carries CLAUDE_PID: never
    # count it (or anything above it) as work.
    self_pids = procs_mod.self_and_ancestors()
    work = {s.key: procs_mod.work_pids(procs, s.pid, cfg, self_pids=self_pids, session_start=s.start)
            for s in sessions}
    docker = docker_mod.client()
    containers, attrs, docker_error = [], {}, None
    if docker is not None:
        try:
            containers = docker.list_containers()
            attrs = docker_mod.attribute(containers, sessions, procs, work)
        except docker_mod.DockerUnavailable as exc:
            docker_error = str(exc)
    return {"procs": procs, "sessions": sessions, "work": work, "docker": docker,
            "containers": containers, "attrs": attrs, "docker_error": docker_error}


def own_key() -> str | None:
    """Key of the Claude session this CLI runs inside, if any."""
    own = sessions_mod.own_session()
    return common.session_key(*own) if own else None


SELF_REFUSAL = ("refusing to {verb} the session this command runs in: its own commands would {effect}. "
                "Pass --include-self to do it anyway.")


def resolve(target: str, sessions: list):
    for session in sessions:
        if target in (str(session.pid), session.key) or (len(target) >= 4 and session.session_id.startswith(target)):
            return session
    return None


# -- status -----------------------------------------------------------------


def cmd_status(args, cfg) -> int:
    status = common.read_json(pressure.status_path(), None)
    wsl = wsl_mod.detect()
    fresh = isinstance(status, dict) and time.time() - status.get("ts", 0) <= cfg.get("status_max_age_seconds", 15)
    if fresh:
        level, reasons, source = status["level"], status.get("reasons", []), "watchdog"
    else:
        level, reasons, source = pressure.current_level(cfg, wsl["wsl"])
    print(f"level:      {level}" + (f" ({', '.join(reasons)})" if reasons else "") + f"  [{source}]")
    metrics = status.get("metrics", {}) if fresh else pressure.metrics(pressure.take_sample())
    shown = ", ".join(f"{k}={v:.1f}" for k, v in metrics.items() if isinstance(v, (int, float)))
    print(f"metrics:    {shown or 'none'}")
    host = status.get("host") if fresh else None
    if host:
        print(f"host:       {fmt_kb(host['available_kb'])} available of {fmt_kb(host['total_kb'])}, "
              f"commit {fmt_kb(host['commit_free_kb'])} free of {fmt_kb(host['commit_total_kb'])}")
    if ctl.alive():
        age = ctl.heartbeat_age()
        state = "hung" if age is not None and age > ctl.HUNG_AFTER else "running"
        print(f"watchdog:   {state}" + (f" (pid {status.get('pid')}, heartbeat {age:.0f}s ago)" if status and age is not None else ""))
    else:
        print("watchdog:   not running")
    print(f"freeze:     {cfg.get('freeze_mode', 'observe')}" + ("" if cfg.get("gate", True) else ", gate off"))
    frozen = sorted(actions.frozen_keys())
    print(f"frozen:     {', '.join(frozen) if frozen else 'nothing'}")
    if wsl["wsl"]:
        print(f"wsl:        WSL{wsl['version'] or '?'} {wsl['distro']}".rstrip())
    return 0


# -- sessions ---------------------------------------------------------------


def session_rows(view: dict, cfg: dict) -> list:
    now = time.time()
    protected = sessions_mod.foreground(view["sessions"], now, cfg.get("foreground_grace_seconds", 60))
    frozen = actions.frozen_keys()
    rows = []
    for session in view["sessions"]:
        work = view["work"][session.key]
        work_kb = sum(view["procs"][p].rss_kb for p in work if p in view["procs"])
        tree_kb = procs_mod.tree_rss_kb(view["procs"], session.pid)
        owned = [c for c in view["containers"]
                 if view["attrs"].get(c.id) and view["attrs"][c.id].owner == session.key]
        rows.append({
            "key": session.key, "pid": session.pid, "session_id": session.session_id[:8],
            "cwd": session.cwd, "kind": session.kind,
            "foreground": session.key in protected, "frozen": session.key in frozen,
            "tree_kb": tree_kb, "baseline_kb": max(0, tree_kb - work_kb),
            "work_procs": len(work), "work_kb": work_kb,
            "containers": [{"name": c.name, "state": c.state, "mem_kb": (docker_mod.cgroup_mem(c.id) or 0) // 1024}
                           for c in owned],
        })
    return rows


def cmd_sessions(args, cfg) -> int:
    view = live_view(cfg)
    rows = session_rows(view, cfg)
    owned_ids = {cid for cid, attr in view["attrs"].items() if attr.owner}
    unowned = [c for c in view["containers"] if c.id not in owned_ids]
    unowned_kb = sum((docker_mod.cgroup_mem(c.id) or 0) // 1024 for c in unowned)
    owned_kb = sum(c["mem_kb"] for row in rows for c in row["containers"])
    meminfo = pressure.read_meminfo(common.proc_root())
    used_kb = meminfo.get("MemTotal", 0) - meminfo.get("MemAvailable", 0)
    other_kb = max(0, used_kb - sum(r["tree_kb"] for r in rows) - owned_kb - unowned_kb) if meminfo else None
    if args.json:
        print(json.dumps({"sessions": rows, "unattributed_containers": [c.name for c in unowned],
                          "unattributed_kb": unowned_kb, "other_kb": other_kb,
                          "docker_error": view["docker_error"]}, indent=1))
        return 0
    print(f"{'PID':>8}  {'FLAGS':<6} {'TREE':>9} {'BASELINE':>9} {'WORK':>14}  CONTAINERS  CWD")
    for row in rows:
        flags = ("F" if row["foreground"] else "-") + ("Z" if row["frozen"] else "-") + row["kind"][:1]
        containers = ", ".join(f"{c['name']}({fmt_kb(c['mem_kb'])}{', paused' if c['state'] == 'paused' else ''})"
                               for c in row["containers"]) or "-"
        work = f"{row['work_procs']}p {fmt_kb(row['work_kb'])}"
        print(f"{row['pid']:>8}  {flags:<6} {fmt_kb(row['tree_kb']):>9} {fmt_kb(row['baseline_kb']):>9} "
              f"{work:>14}  {containers}  {row['cwd']}")
    print(f"unattributed containers: {len(unowned)} using {fmt_kb(unowned_kb)} (MCP/LSP servers, shared services)")
    if other_kb is not None:
        print(f"other (kernel, page cache, other distros): {fmt_kb(other_kb)}")
    if view["docker_error"]:
        print(f"docker: unavailable ({view['docker_error']})")
    print("flags: F foreground, Z frozen, last letter = kind (i interactive, b bg, u unknown)")
    return 0


# -- freeze / resume / stop -------------------------------------------------


def cmd_freeze(args, cfg) -> int:
    view = live_view(cfg)
    mine = own_key()
    if args.others:
        protected = sessions_mod.foreground(view["sessions"], time.time(), cfg.get("foreground_grace_seconds", 60))
        targets = [s for s in view["sessions"] if s.key not in protected and (s.key != mine or args.include_self)]
    else:
        session = resolve(args.target or "", view["sessions"])
        if session is None:
            print(f"no live session matches {args.target!r}", file=sys.stderr)
            return 1
        if session.key == mine and not args.include_self:
            print(SELF_REFUSAL.format(verb="freeze", effect="stop"), file=sys.stderr)
            return 1
        targets = [session]
    keys = {s.key for s in targets} | actions.frozen_keys()
    for session in targets:
        result = actions.freeze_session(session, cfg=cfg, targets=keys, procs=view["procs"], docker=view["docker"],
                                        containers=view["containers"], attrs=view["attrs"], mode="enforce",
                                        reason="cli", sender=SENDER)
        print(f"froze {session.key}: {len(result['procs'])} processes, "
              f"containers: {', '.join(result['containers']) or 'none'}")
    if not targets:
        print("nothing to freeze")
    return 0


def cmd_resume(args, cfg) -> int:
    docker = docker_mod.client()
    if args.all:
        results = actions.resume_all(docker=docker, sender=SENDER, reason="cli")
    else:
        key = next((k for k in actions.frozen_keys() if args.target in (k, k.split("-")[0])), None)
        if key is None:
            print(f"nothing frozen for {args.target!r}", file=sys.stderr)
            return 1
        results = [actions.resume_session(key, docker=docker, sender=SENDER, reason="cli")]
    for result in results:
        print(f"resumed {result['key']}: {result.get('procs', 0)} processes, "
              f"containers: {', '.join(result.get('containers', [])) or 'none'}"
              + (f", still paused: {', '.join(result['pending'])}" if result.get("pending") else ""))
    if not results:
        print("nothing frozen")
    return 0


def cmd_stop(args, cfg) -> int:
    view = live_view(cfg)
    session = resolve(args.target, view["sessions"])
    if session is None:
        print(f"no live session matches {args.target!r}", file=sys.stderr)
        return 1
    if session.key == own_key() and not args.include_self:
        print(SELF_REFUSAL.format(verb="stop", effect="be killed"), file=sys.stderr)
        return 1
    work = view["work"][session.key]
    owned = [c for c in view["containers"]
             if view["attrs"].get(c.id) and view["attrs"][c.id].owner == session.key]
    names = ", ".join(c.name for c in owned) or "none"
    if not args.yes:
        print(f"would stop {len(work)} processes and containers: {names} of {session.key} ({session.cwd}); "
              "the Claude session itself keeps running. Re-run with --yes.")
        return 1
    actions.resume_session(session.key, docker=view["docker"], sender=SENDER, stop_orphans=False, reason="cli-stop")
    identities = [(pid, view["procs"][pid].start) for pid in work if pid in view["procs"]]
    for pid, start in identities:
        procs_mod.signal_verified(pid, start, signal.SIGTERM, sender=SENDER)
        procs_mod.signal_verified(pid, start, signal.SIGCONT, sender=SENDER)
    if identities:
        SLEEP(STOP_GRACE)
    killed = sum(procs_mod.signal_verified(pid, start, signal.SIGKILL, sender=SENDER) for pid, start in identities)
    stopped = []
    for container in owned:
        try:
            if container.state == "paused":
                view["docker"].unpause(container.id)
            if view["docker"].stop(container.id):
                stopped.append(container.name)
        except docker_mod.DockerUnavailable:
            continue
    common.append_event("cli-stop", session=session.key, procs=len(identities), containers=stopped)
    print(f"stopped {len(identities)} processes ({killed} needed SIGKILL), containers: {', '.join(stopped) or 'none'}")
    return 0


# -- doctor -----------------------------------------------------------------


def doctor_checks(cfg: dict, view: dict | None = None) -> list:
    checks = []
    wsl = wsl_mod.detect()
    if not sys.platform.startswith("linux"):
        checks.append(("fail", f"platform {sys.platform} is unsupported in v1: hooks do nothing"))
    elif wsl["wsl"] and wsl["version"] == 1:
        checks.append(("fail", "WSL1 has no PSI and no VM boundary: unsupported in v1"))
    else:
        checks.append(("ok", "platform: " + (f"WSL{wsl['version'] or '?'} {wsl['distro']}" if wsl["wsl"] else "Linux")))
    psi = pressure.read_psi(common.proc_root(), "memory")
    checks.append(("ok", "PSI available") if psi else ("warn", "no /proc/pressure: memory stall can't be measured"))
    checks.append(("ok" if ctl.alive() else "warn", "watchdog " + ("running" if ctl.alive() else "not running")))
    checks.append(("ok", f"freeze_mode={cfg.get('freeze_mode')}, gate={'on' if cfg.get('gate', True) else 'off'}, "
                         f"config {common.user_config_path()}"))
    view = view if view is not None else live_view(cfg)
    if view["docker"] is None:
        checks.append(("warn", "no docker daemon socket found: containers can't be paused"))
    elif view["docker_error"]:
        checks.append(("warn", f"docker unreachable: {view['docker_error']}"))
    else:
        checks.append(("ok", f"docker reachable, {len(view['containers'])} running containers"))
        for key, pids in view["work"].items():
            for pid in pids:
                invocation = docker_mod.docker_invocation(view["procs"][pid].cmdline) if pid in view["procs"] else None
                if invocation and invocation[0] == "run":
                    labeled = any(a.owner == key for a in view["attrs"].values())
                    if not labeled:
                        checks.append(("warn", f"session {key} runs `docker run` without resource-guard labels: "
                                               "the shim is bypassed (alias or absolute path?)"))
                        break
    if wsl["wsl"]:
        status = common.read_json(pressure.status_path(), {}) or {}
        for line in wsl_mod.doctor_advice(wsl_mod.load_wslconfig(), status.get("host")):
            checks.append(("warn", line))
        link = common.state_dir() / "bin" / "resource-guard"
        checks.append(("info", f"escape hatch from PowerShell: wsl.exe -d {wsl['distro'] or '<distro>'} -e {link} resume --all"))
    return checks


def cmd_doctor(args, cfg) -> int:
    checks = doctor_checks(cfg)
    for level, text in checks:
        print(f"[{level:^4}] {text}")
    return 1 if any(level == "fail" for level, _ in checks) else 0


# -- watchdog ---------------------------------------------------------------


def cmd_watchdog(args, cfg) -> int:
    if args.action == "start":
        print(f"watchdog: {ctl.ensure_watchdog(force=True)}")
        return 0
    if args.action == "status":
        age = ctl.heartbeat_age()
        if not ctl.alive():
            print("watchdog: not running")
        else:
            print(f"watchdog: {'hung' if ctl.hung() else 'running'}" + (f", heartbeat {age:.0f}s ago" if age is not None else ""))
        return 0
    status = common.read_json(pressure.status_path(), {}) or {}
    pid = status.get("pid")
    proc = procs_mod.read_proc(pid) if isinstance(pid, int) else None
    if not ctl.alive() or proc is None or not any("watchdog.py" in arg for arg in proc.cmdline):
        print("watchdog: not running")
        return 0
    procs_mod.signal_verified(proc.pid, proc.start, signal.SIGTERM, sender=SENDER)
    for _ in range(50):
        if not ctl.alive():
            print("watchdog: stopped (it resumed what it froze)")
            return 0
        SLEEP(0.1)
    procs_mod.signal_verified(proc.pid, proc.start, signal.SIGKILL, sender=SENDER)
    actions.resume_all(docker=docker_mod.client(), sender=SENDER, reason="watchdog-killed")
    print("watchdog: killed; frozen work resumed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resource-guard", description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="load level, metrics, watchdog, frozen sessions")
    sessions = sub.add_parser("sessions", help="per-session footprint and containers")
    sessions.add_argument("--json", action="store_true")
    freeze = sub.add_parser("freeze", help="freeze a session's Bash work and containers now")
    group = freeze.add_mutually_exclusive_group(required=True)
    group.add_argument("target", nargs="?", help="pid, pid-start key or session id prefix")
    group.add_argument("--others", action="store_true", help="every session except the foreground one")
    freeze.add_argument("--include-self", action="store_true", help="allow freezing the session this runs in")
    resume = sub.add_parser("resume", help="resume frozen work")
    group = resume.add_mutually_exclusive_group(required=True)
    group.add_argument("target", nargs="?")
    group.add_argument("--all", action="store_true")
    stop = sub.add_parser("stop", help="terminate a session's Bash work and stop its containers")
    stop.add_argument("target")
    stop.add_argument("--yes", action="store_true")
    stop.add_argument("--include-self", action="store_true", help="allow stopping the session this runs in")
    sub.add_parser("doctor", help="check the setup and WSL limits")
    watchdog = sub.add_parser("watchdog", help="start, stop or check the watchdog")
    watchdog.add_argument("action", choices=("start", "stop", "status"))
    return parser


COMMANDS = {"status": cmd_status, "sessions": cmd_sessions, "freeze": cmd_freeze, "resume": cmd_resume,
            "stop": cmd_stop, "doctor": cmd_doctor, "watchdog": cmd_watchdog}


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = common.load_config()
    try:
        return COMMANDS[args.command](args, cfg)
    except common.LockTimeout:
        print("another resource-guard action holds the lock; try again in a few seconds", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
