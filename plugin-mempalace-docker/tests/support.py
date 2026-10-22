"""Shared fixtures for the mempalace-docker tests.

Nothing here talks to a real Docker daemon or GPU. A stub `docker` keeps a
fake hub container's state in a file and logs every call; stub `nvidia-smi`
and `sleep` make GPU detection and the health-wait loop deterministic and
instant. Every test gets its own HOME, state dir and XDG config dir.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
HUB = PLUGIN / "scripts" / "hub.sh"
HEADERS = PLUGIN / "scripts" / "hub-headers.sh"
CLI_SHIM = PLUGIN / "scripts" / "bin" / "mempalace"
PY_SHIM = PLUGIN / "scripts" / "bin" / "mempalace-python3"
HOOK = PLUGIN / "hooks" / "session_start_hook.py"
MARK_MINED = PLUGIN / "scripts" / "mark_mined.py"

CPU_IMAGE = "ghcr.io/mempalace/mempalace:latest"
FP_LABEL = "dev.mempalace-docker.fingerprint"

# State file: one line, "status|fingerprint|health". Absent = no container.
# health_seq: optional, one health value per line, popped by each inspect.
DOCKER_STUB = r"""#!/usr/bin/env bash
d="$STUB_DIR"
printf '%q ' "$@" >> "$d/calls.log"
printf '\n' >> "$d/calls.log"
state="$d/state"
case "$1" in
    info) printf '%s\n' "${STUB_INFO:- Runtimes: io.containerd.runc.v2 runc}"; exit 0 ;;
    image) exit "${STUB_IMAGE_EXIT:-1}" ;;
    volume) exit 0 ;;
    logs) echo "STUB LOG LINE: writer lease held by another process"; exit 0 ;;
    inspect)
        [ -f "$state" ] || exit 1
        IFS='|' read -r st fp hl < "$state"
        if [ -s "$d/health_seq" ]; then
            hl="$(head -n1 "$d/health_seq")"
            sed -i '1d' "$d/health_seq"
        fi
        printf '%s|%s|%s\n' "$st" "$fp" "$hl"
        exit 0 ;;
    run)
        fp=""
        prev=""
        for a in "$@"; do
            if [ "$prev" = "--label" ]; then
                case "$a" in dev.mempalace-docker.fingerprint=*) fp="${a#*=}" ;; esac
            fi
            prev="$a"
        done
        [ "${STUB_RUN_EXIT:-0}" = 0 ] || exit "$STUB_RUN_EXIT"
        printf '%s|%s|%s\n' "${STUB_RUN_STATUS:-running}" "$fp" "${STUB_RUN_HEALTH:-starting}" > "$state"
        echo 0123456789ab
        exit 0 ;;
    start)
        IFS='|' read -r st fp hl < "$state"
        printf 'running|%s|starting\n' "$fp" > "$state"
        exit 0 ;;
    stop)
        IFS='|' read -r st fp hl < "$state"
        printf 'exited|%s|unhealthy\n' "$fp" > "$state"
        exit 0 ;;
    rm) rm -f "$state"; exit 0 ;;
    exec) cat > "$d/exec_stdin"; echo "EXEC-OUT"; exit "${STUB_EXEC_EXIT:-0}" ;;
esac
exit 0
"""

NVIDIA_STUB = """#!/usr/bin/env bash
exit "${STUB_NVIDIA_EXIT:-1}"
"""

SLEEP_STUB = """#!/usr/bin/env bash
exit 0
"""

SYSTEM_TOOLS = (
    "bash", "python3", "sed", "sort", "cut", "cat", "mkdir", "ln", "chmod", "basename",
    "dirname", "git", "head", "tr", "date", "rm", "sha256sum", "grep", "uname", "du",
)


def system_path() -> str:
    dirs = []
    for tool in SYSTEM_TOOLS:
        found = shutil.which(tool)
        if found and str(Path(found).parent) not in dirs:
            dirs.append(str(Path(found).parent))
    return os.pathsep.join(dirs)


class HubTestCase(unittest.TestCase):
    """Temp HOME + stub binaries; helpers to drive the scripts and read the log."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.home = self.tmp / "home"
        (self.home / ".claude" / "projects").mkdir(parents=True)
        self.state = self.tmp / "state"
        self.xdg = self.tmp / "xdg"
        self.registry = self.xdg / "mempalace-docker" / "projects"
        self.stub_dir = self.tmp / "stub"
        self.stub_dir.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.write_stub("docker", DOCKER_STUB)
        self.write_stub("nvidia-smi", NVIDIA_STUB)
        self.write_stub("sleep", SLEEP_STUB)
        self.extra_env = {}

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------ setup

    def write_stub(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def env(self, **overrides):
        env = {
            "PATH": os.pathsep.join([str(self.bin), system_path()]),
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "MEMPALACE_DOCKER_STATE": str(self.state),
            "XDG_CONFIG_HOME": str(self.xdg),
            "STUB_DIR": str(self.stub_dir),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
        }
        env.update(self.extra_env)
        env.update({k: str(v) for k, v in overrides.items()})
        return env

    def project(self, *parts, git=False) -> Path:
        path = self.home.joinpath(*parts) if parts else self.home / "Projects" / "app"
        path.mkdir(parents=True, exist_ok=True)
        if git:
            subprocess.run(["git", "init", "-q", str(path)], check=True, env=self.env())
        return path

    def register(self, target: Path, name=None):
        self.registry.mkdir(parents=True, exist_ok=True)
        link = self.registry / (name or target.name)
        link.symlink_to(target)
        return link

    def set_container(self, status="running", fingerprint="", health="healthy"):
        (self.stub_dir / "state").write_text(f"{status}|{fingerprint}|{health}\n")

    def set_health_sequence(self, *values):
        (self.stub_dir / "health_seq").write_text("".join(f"{v}\n" for v in values))

    def container_state(self):
        path = self.stub_dir / "state"
        return path.read_text().strip() if path.exists() else None

    # -------------------------------------------------------------- run

    def run_script(self, script, *args, input=None, cwd=None, **env):
        return subprocess.run(
            ["bash", str(script), *map(str, args)],
            input=input,
            capture_output=True,
            text=True,
            env=self.env(**env),
            cwd=str(cwd or self.tmp),
            timeout=60,
        )

    def hub(self, *args, **env):
        return self.run_script(HUB, *args, **env)

    def fingerprint(self, **env) -> str:
        proc = self.hub("fingerprint", **env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    # ------------------------------------------------------------- calls

    def calls(self):
        log = self.stub_dir / "calls.log"
        if not log.exists():
            return []
        return [shlex.split(line) for line in log.read_text().splitlines() if line.strip()]

    def calls_of(self, verb):
        return [c for c in self.calls() if c and c[0] == verb]

    def run_call(self):
        runs = self.calls_of("run")
        self.assertEqual(len(runs), 1, f"expected exactly one docker run, got {self.calls()}")
        return runs[0]

    def mount_args(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-v"]

    def assert_flag(self, argv, flag, value):
        pairs = list(zip(argv, argv[1:]))
        self.assertIn((flag, value), pairs, f"{flag} {value} not in {argv}")
