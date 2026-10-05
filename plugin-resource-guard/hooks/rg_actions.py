"""
Freeze and resume, shared by the watchdog, the hooks and the CLI.

Every change goes through actions.lock and frozen.json, so two callers can't
interleave a freeze with a resume. Only what this module stopped is recorded,
and only what is recorded is ever resumed: a process the user stopped with
Ctrl-Z, or a container they paused themselves, is left alone.

Order matters:
- freeze: SIGSTOP parent-first, rescan for anything forked meanwhile (up to
  3 passes), then pause containers;
- resume: unpause containers first, then SIGCONT, so a resumed client never
  talks to a paused container.

Write-ahead: a process or container is recorded in frozen.json before it is
signalled or paused, and the record is saved again however the freeze ends.
A crash, a SIGTERM or a dead daemon halfway through can only leave a record
of something still running (resuming that is harmless), never a stopped
process nobody knows about.

A container whose client was alive at freeze time and died while frozen
(Esc, a background-task limit, a timeout) is stopped after resume: nothing is
left to collect its output, and paused memory is better given back.
"""

from __future__ import annotations

import os
import signal
import time

import rg_common as common
import rg_docker as docker_mod
import rg_procs as procs_mod

LOCK_TIMEOUT = 5.0
RESCAN_PASSES = 3
UNPAUSE_ATTEMPTS = 5
STOP_GRACE = 10


def frozen_path():
    return common.state_dir() / "frozen.json"


def load_frozen() -> dict:
    data = common.read_json(frozen_path(), {}) or {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return {"sessions": sessions if isinstance(sessions, dict) else {}}


def save_frozen(data: dict) -> None:
    common.atomic_write_json(frozen_path(), data)


def frozen_keys() -> set:
    return set(load_frozen()["sessions"])


def freeze_session(session, *, cfg: dict, targets: set, procs: dict | None = None, docker=None,
                   containers: list = (), attrs: dict | None = None, mode: str = "enforce",
                   reason: str = "", root=None, sender=None, lock_timeout: float = LOCK_TIMEOUT,
                   still_target=None) -> dict:
    """Freeze one session's Bash work and the containers it owns. In observe
    mode nothing is signalled or paused; the summary says what would be.

    `still_target()` is asked again once the lock is held: the user may have
    typed in this session while the caller waited, making it foreground."""
    key = session.key
    enforce = mode == "enforce"
    stopped, paused = [], []
    attrs = attrs or {}
    with common.locked(common.actions_lock(), timeout=lock_timeout):
        if still_target is not None and not still_target():
            return {"key": key, "procs": [], "containers": [], "mode": mode, "skipped": True}
        frozen = load_frozen()
        entry = frozen["sessions"].get(key) or {"pid": session.pid, "start": session.start,
                                                 "frozen_at": time.time(), "procs": [], "containers": []}

        def persist() -> None:
            if not enforce:
                return
            if entry["procs"] or entry["containers"]:
                frozen["sessions"][key] = entry
            else:
                frozen["sessions"].pop(key, None)
            save_frozen(frozen)

        recorded = {tuple(item) for item in entry["procs"]}
        self_pids = procs_mod.self_and_ancestors(root)
        try:
            for attempt in range(RESCAN_PASSES):
                scan = procs if attempt == 0 and procs is not None else procs_mod.scan(root)
                pids = procs_mod.work_pids(scan, session.pid, cfg, root, self_pids=self_pids,
                                           session_start=session.start)
                batch = [[pid, scan[pid].start] for pid in pids
                         if (pid, scan[pid].start) not in recorded and scan[pid].state not in ("T", "t")]
                if not batch:
                    break
                if enforce:
                    entry["procs"].extend(batch)
                    persist()
                for item in batch:
                    if enforce and not procs_mod.signal_verified(item[0], item[1], signal.SIGSTOP, root, sender):
                        entry["procs"].remove(item)
                        continue
                    recorded.add(tuple(item))
                    stopped.append(item[0])
                if not enforce:
                    break

            for container in containers if docker is not None or not enforce else ():
                attr = attrs.get(container.id)
                if attr is None or attr.owner != key or not docker_mod.pausable(container, attr, targets, cfg)[0]:
                    continue
                if enforce:
                    client_alive = bool(attr.client) and procs_mod.start_of(attr.client[0], root) == attr.client[1]
                    item = {"id": container.id, "name": container.name,
                            "client": list(attr.client) if attr.client else None, "client_alive": client_alive}
                    entry["containers"].append(item)
                    persist()
                    try:
                        ok = docker.pause(container.id)
                    except docker_mod.DockerUnavailable:
                        ok = False
                    if not ok:
                        entry["containers"].remove(item)
                        continue
                paused.append(container.name)
        finally:
            persist()

    if stopped or paused:
        common.append_event("freeze" if enforce else "would-freeze", session=key,
                            procs=len(stopped), containers=paused, reason=reason)
    return {"key": key, "procs": stopped, "containers": paused, "mode": mode}


def resume_session(key: str, *, docker=None, root=None, sender=None, stop_orphans: bool = True,
                   stop_grace: int = STOP_GRACE, reason: str = "", lock_timeout: float = LOCK_TIMEOUT) -> dict:
    """Undo one session's freeze. Containers the daemon couldn't unpause stay
    recorded so the next call retries them, up to UNPAUSE_ATTEMPTS times."""
    with common.locked(common.actions_lock(), timeout=lock_timeout):
        frozen = load_frozen()
        entry = frozen["sessions"].pop(key, None)
        if entry is None:
            return {"key": key, "resumed": False}
        unpaused, pending, abandoned = [], [], []
        for item in entry.get("containers", []):
            try:
                if docker is None:
                    raise docker_mod.DockerUnavailable("no docker client")
                docker.unpause(item["id"])
                unpaused.append(item)
            except docker_mod.DockerUnavailable:
                item = dict(item, attempts=int(item.get("attempts", 0)) + 1)
                (abandoned if item["attempts"] >= UNPAUSE_ATTEMPTS else pending).append(item)

        continued = 0
        for pid, start in reversed(entry.get("procs", [])):
            if procs_mod.signal_verified(int(pid), int(start), signal.SIGCONT, root, sender):
                continued += 1

        stopped = []
        for item in unpaused if stop_orphans else []:
            client = item.get("client")
            if item.get("client_alive") and client and procs_mod.start_of(client[0], root) != client[1]:
                try:
                    if docker.stop(item["id"], grace=stop_grace):
                        stopped.append(item["name"])
                except docker_mod.DockerUnavailable:
                    pass

        if pending:
            frozen["sessions"][key] = dict(entry, procs=[], containers=pending)
        save_frozen(frozen)

    common.append_event("resume", session=key, procs=continued, containers=[i["name"] for i in unpaused],
                        pending=[i["name"] for i in pending], reason=reason)
    if stopped:
        common.append_event("orphan-stopped", session=key, containers=stopped)
    if abandoned:
        common.append_event("unpause-failed", session=key, containers=[i["name"] for i in abandoned])
    return {"key": key, "resumed": True, "procs": continued, "containers": [i["name"] for i in unpaused],
            "pending": [i["name"] for i in pending], "orphans_stopped": stopped,
            "abandoned": [i["name"] for i in abandoned]}


def resume_all(*, docker=None, root=None, sender=None, reason: str = "", lock_timeout: float = LOCK_TIMEOUT,
               stop_grace: int = STOP_GRACE, deadline: float | None = None) -> list:
    """Resume every frozen session. With `deadline` (a time.time() value) it
    stops starting new sessions once that passes: a hook has a hard timeout,
    and whatever is left stays recorded for the watchdog or the next hook."""
    results = []
    for key in sorted(frozen_keys()):
        if deadline is not None and time.time() >= deadline:
            break
        results.append(resume_session(key, docker=docker, root=root, sender=sender, reason=reason,
                                      lock_timeout=lock_timeout, stop_grace=stop_grace))
    return results
