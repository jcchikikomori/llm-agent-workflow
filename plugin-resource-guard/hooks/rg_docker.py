"""
Docker side of the freeze: list, pause, unpause and stop containers, and
work out which session each container belongs to.

The Engine API is spoken over the unix socket with http.client, because
under memory pressure forking the ~30 MB Go docker CLI once per call is the
slow, expensive part. A tcp/ssh DOCKER_HOST falls back to the CLI.

Attribution, strongest first:
1. labels the shim stamped at `docker run` time (dev.claude.pid/pid_start);
2. a live `docker run --name X` client in the session's Bash work owns X;
3. a live `docker exec X` client in a session's work references X.
Anything else is unattributed and is never paused.

A container labeled `dev.claude.role=server` is one of the session's own
MCP/LSP servers (the shim on Claude Code's PATH labels them). It is owned,
so it shows in the session's footprint, but it is never paused or stopped:
the session would lose that tool mid-call.
"""

from __future__ import annotations

import fnmatch
import hashlib
import http.client
import json
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import rg_common as common

TIMEOUT = 5.0

GLOBAL_VALUE_FLAGS = {"--config", "-c", "--context", "-H", "--host", "-l", "--log-level",
                      "--tlscacert", "--tlscert", "--tlskey"}
EXEC_VALUE_FLAGS = {"-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir", "--detach-keys"}
# `docker run`/`create` flags that take no value. Every other flag takes one
# (pflag never makes a value optional), which is what lets the parser find
# where the options end and the image starts.
RUN_BOOL_FLAGS = {"--rm", "--detach", "-d", "--interactive", "-i", "--tty", "-t", "--init", "--privileged",
                  "--read-only", "--publish-all", "-P", "--no-healthcheck", "--oom-kill-disable", "--sig-proxy",
                  "--disable-content-trust", "--quiet", "-q", "--help", "--use-api-socket"}
RUN_BOOL_SHORT = set("dtiPq")
MIN_ID_PREFIX = 12


class DockerUnavailable(Exception):
    """The daemon didn't answer in time, or there is no daemon."""


@dataclass
class Container:
    id: str
    name: str
    image: str
    state: str
    labels: dict = field(default_factory=dict)
    created: float = 0.0


@dataclass
class Attribution:
    owner: str | None = None
    refs: set = field(default_factory=set)
    client: tuple | None = None
    via: str = "none"
    role: str = "work"


def socket_path(env: dict | None = None, home: Path | None = None) -> tuple:
    """(unix socket path or None, use_cli). DOCKER_HOST wins, then the
    current docker context, then the default socket."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    host = env.get("DOCKER_HOST", "")
    if host:
        return (host[len("unix://"):], False) if host.startswith("unix://") else (None, True)
    context = env.get("DOCKER_CONTEXT")
    if not context:
        config = common.read_json(home / ".docker" / "config.json", {}) or {}
        context = config.get("currentContext") if isinstance(config, dict) else None
    if context and context != "default":
        digest = hashlib.sha256(context.encode()).hexdigest()
        meta = common.read_json(home / ".docker" / "contexts" / "meta" / digest / "meta.json", {}) or {}
        endpoint = (meta.get("Endpoints", {}).get("docker", {}) or {}).get("Host", "")
        if endpoint.startswith("unix://"):
            return endpoint[len("unix://"):], False
        if endpoint:
            return None, True
    return "/var/run/docker.sock", False


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _container_from_api(item: dict) -> Container:
    names = item.get("Names") or []
    return Container(
        id=item.get("Id", ""),
        name=names[0].lstrip("/") if names else "",
        image=item.get("Image", ""),
        state=item.get("State", ""),
        labels=item.get("Labels") or {},
        created=float(item.get("Created") or 0),
    )


class DockerAPI:
    def __init__(self, path: str, timeout: float = TIMEOUT):
        self.path = path
        self.timeout = timeout

    def _request(self, method: str, url: str, timeout: float | None = None) -> tuple:
        conn = _UnixConnection(self.path, timeout if timeout is not None else self.timeout)
        try:
            conn.request(method, url, headers={"Host": "docker"})
            response = conn.getresponse()
            return response.status, response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise DockerUnavailable(str(exc)) from exc
        finally:
            conn.close()

    def list_containers(self) -> list:
        status, body = self._request("GET", "/containers/json")
        if status != 200:
            raise DockerUnavailable(f"list returned {status}")
        try:
            return [_container_from_api(item) for item in json.loads(body)]
        except (ValueError, TypeError) as exc:
            raise DockerUnavailable("bad list payload") from exc

    def _post(self, cid: str, action: str, ok: tuple, timeout: float | None = None) -> bool:
        status, _ = self._request("POST", f"/containers/{cid}/{action}", timeout)
        if status in ok:
            return True
        if status in (304, 404, 409):
            return False
        raise DockerUnavailable(f"{action} returned {status}")

    def pause(self, cid: str) -> bool:
        return self._post(cid, "pause", (204,))

    def unpause(self, cid: str) -> bool:
        return self._post(cid, "unpause", (204,))

    def stop(self, cid: str, grace: int = 10) -> bool:
        # The daemon answers only after the grace period (and the SIGKILL that
        # may follow it), so the client must wait longer than that.
        return self._post(cid, f"stop?t={int(grace)}", (204,), timeout=int(grace) + self.timeout)


def _parse_cli_labels(text: str) -> dict:
    labels = {}
    for pair in (text or "").split(","):
        key, sep, value = pair.partition("=")
        if sep:
            labels[key] = value
    return labels


class DockerCLI:
    """Fallback for tcp/ssh daemons, through the docker binary."""

    def __init__(self, binary: str = "docker", timeout: float = TIMEOUT, runner=subprocess.run):
        self.binary = binary
        self.timeout = timeout
        self.runner = runner

    def _run(self, *args: str, timeout: float | None = None) -> str:
        try:
            result = self.runner([self.binary, *args], capture_output=True, text=True,
                                 timeout=timeout if timeout is not None else self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerUnavailable(str(exc)) from exc
        if result.returncode != 0:
            raise DockerUnavailable(result.stderr.strip()[:200])
        return result.stdout

    def list_containers(self) -> list:
        containers = []
        for line in self._run("ps", "--no-trunc", "--format", "{{json .}}").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            containers.append(Container(id=item.get("ID", ""), name=item.get("Names", ""), image=item.get("Image", ""),
                                        state=item.get("State", ""), labels=_parse_cli_labels(item.get("Labels", ""))))
        return containers

    def _act(self, *args: str, timeout: float | None = None) -> bool:
        try:
            self._run(*args, timeout=timeout)
        except DockerUnavailable:
            return False
        return True

    def pause(self, cid: str) -> bool:
        return self._act("pause", cid)

    def unpause(self, cid: str) -> bool:
        return self._act("unpause", cid)

    def stop(self, cid: str, grace: int = 10) -> bool:
        return self._act("stop", "-t", str(int(grace)), cid, timeout=int(grace) + self.timeout)


def client(env: dict | None = None, home: Path | None = None):
    path, use_cli = socket_path(env, home)
    if use_cli:
        return DockerCLI()
    if path and Path(path).exists():
        return DockerAPI(path)
    return None


def cgroup_mem(cid: str, root: Path | None = None) -> int | None:
    """Container working set in bytes from cgroupfs: memory.current minus
    inactive_file, the figure `docker stats` shows (page cache the kernel
    can drop isn't load). WSL2 distros share the docker-desktop kernel, so
    this is readable without asking the daemon."""
    root = Path(root) if root is not None else common.sys_root()
    for rel in (f"fs/cgroup/docker/{cid}", f"fs/cgroup/system.slice/docker-{cid}.scope"):
        try:
            current = int((root / rel / "memory.current").read_text().strip())
        except (OSError, ValueError):
            continue
        inactive = 0
        try:
            for line in (root / rel / "memory.stat").read_text().splitlines():
                key, _, value = line.partition(" ")
                if key == "inactive_file" and value.strip().isdigit():
                    inactive = int(value)
                    break
        except OSError:
            pass
        return max(0, current - inactive)
    return None


def docker_invocation(argv: list) -> tuple | None:
    """('run', name|None) or ('exec', container) for a docker client command
    line, else None."""
    if not argv or os.path.basename(argv[0]) != "docker":
        return None
    args, i = argv[1:], 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in GLOBAL_VALUE_FLAGS else 1
    if i < len(args) and args[i] == "container":
        i += 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    if sub in ("run", "create"):
        return "run", _run_name(rest)
    if sub == "exec":
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            j += 2 if rest[j] in EXEC_VALUE_FLAGS else 1
        return ("exec", rest[j]) if j < len(rest) else None
    return None


def _run_name(rest: list) -> str | None:
    """--name of a `docker run`/`create`, read only up to the image: what
    follows it is the container's own command line (`run alpine prog --name x`
    names nothing)."""
    name, j = None, 0
    while j < len(rest):
        arg = rest[j]
        if arg == "--" or not arg.startswith("-") or arg == "-":
            break
        if arg == "--name":
            name = rest[j + 1] if j + 1 < len(rest) else None
            j += 2
        elif arg.startswith("--name="):
            name, j = arg.split("=", 1)[1], j + 1
        elif "=" in arg or arg in RUN_BOOL_FLAGS:
            j += 1
        elif arg.startswith("--"):
            j += 2
        elif all(ch in RUN_BOOL_SHORT for ch in arg[1:]):
            j += 1
        elif all(ch in RUN_BOOL_SHORT for ch in arg[1:-1]):
            j += 2  # -e VALUE, or booleans ending in a value flag (-dp 80:80)
        else:
            j += 1  # value attached: -p8080:80
    return name


def _exec_target(target: str, by_name: dict, containers: list) -> str | None:
    """Exact name, or an ID prefix long enough to be meant (12+ hex) and
    matching exactly one container."""
    if target in by_name:
        return by_name[target]
    if len(target) < MIN_ID_PREFIX or any(ch not in "0123456789abcdef" for ch in target):
        return None
    matches = [c.id for c in containers if c.id.startswith(target)]
    return matches[0] if len(matches) == 1 else None


def _label_owner(labels: dict, by_pid: dict) -> str | None:
    pid = labels.get("dev.claude.pid", "")
    if not pid.isdigit() or int(pid) not in by_pid:
        return None
    session = by_pid[int(pid)]
    start = labels.get("dev.claude.pid_start", "")
    if start and (not start.isdigit() or int(start) != session.start):
        return None
    return session.key


def _label_client(labels: dict) -> tuple | None:
    pid, start = labels.get("dev.claude.client", ""), labels.get("dev.claude.client_start", "")
    return (int(pid), int(start)) if pid.isdigit() and start.isdigit() else None


def attribute(containers: list, sessions: list, procs: dict, work: dict) -> dict:
    """{container id: Attribution}. `work` maps session key -> its Bash-work
    PIDs (rg_procs.work_pids), where live docker clients are looked for."""
    by_pid = {s.pid: s for s in sessions}
    by_name = {c.name: c.id for c in containers}
    out = {}
    for c in containers:
        owner = _label_owner(c.labels, by_pid)
        out[c.id] = Attribution(owner=owner, refs={owner} if owner else set(),
                                client=_label_client(c.labels), via="label" if owner else "none",
                                role="server" if c.labels.get("dev.claude.role") == "server" else "work")
    for key, pids in work.items():
        for pid in pids:
            proc = procs.get(pid)
            invocation = docker_invocation(proc.cmdline) if proc else None
            if not invocation or not invocation[1]:
                continue
            kind, target = invocation
            # Ownership only ever comes from an exact name: a short name can
            # prefix-match somebody else's container ID.
            cid = by_name.get(target) if kind == "run" else _exec_target(target, by_name, containers)
            if cid is None:
                continue
            attr = out[cid]
            if kind == "run" and attr.owner is None:
                attr.owner, attr.via, attr.client = key, "name", (proc.pid, proc.start)
            attr.refs.add(key)
    return out


def owned_by(containers: list, attrs: dict, key: str, role: str | None = "work") -> list:
    """Containers attributed to session `key`: its work ("work"), its
    servers ("server"), or both (None)."""
    out = []
    for c in containers:
        attr = attrs.get(c.id)
        if attr and attr.owner == key and (role is None or attr.role == role):
            out.append(c)
    return out


def pausable(container: Container, attr: Attribution, targets: set, cfg: dict) -> tuple:
    """(bool, reason). Only a running container owned by a freeze target, with
    no reference from a session that stays running, outside shared compose
    services and the never_pause list."""
    if container.state != "running":
        return False, container.state or "not running"
    if attr.role == "server":
        return False, "session server"
    if not attr.owner:
        return False, "unattributed"
    if attr.owner not in targets or not attr.refs <= targets:
        return False, "shared with a running session"
    labels = container.labels
    if "com.docker.compose.service" in labels and labels.get("com.docker.compose.oneoff") != "True":
        return False, "compose service"
    for pattern in cfg.get("never_pause", []):
        if fnmatch.fnmatch(container.name, pattern) or fnmatch.fnmatch(container.image, pattern):
            return False, f"never_pause {pattern}"
    return True, "ok"
