#!/usr/bin/env python3
"""Per-session stdio end of the shared ruby-lsp hub.

Claude Code spawns this through `run-ruby-tool.sh lsp` on the first Ruby edit
and keeps it for the whole session. It holds no LSP state: it connects to the
checkout's hub (lsp_hub.py), starting the hub when nothing answers, and pumps
bytes both ways. The hub decides when the real ruby-lsp runs.

  - stdin EOF (session over)  -> half-close the socket, exit 0
  - hub EOF (hub went away)   -> exit 1; Claude Code restarts the bridge, and
                                 the new bridge starts a new hub
  - shared mode unavailable   -> run the hub in-process ("solo"): still lazy
    (RUBY_LSP_PLUGIN_SHARED=0,      and idle-stopping, but not shared
     no unix sockets, or the
     hub never came up)

Standard library only. Log lines go to stderr; stdout carries JSON-RPC only.
"""

import os
import socket
import sys
import threading
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

import lsp_hub  # noqa: E402

HUB_SCRIPT = os.path.join(SCRIPTS_DIR, "lsp_hub.py")
CONNECT_DEADLINE = 45.0  # covers an exiting hub that is still stopping its backend
RESPAWN_INTERVAL = 2.0
MAX_SOCKET_PATH = 100  # sun_path is 108 bytes on Linux, 104 on macOS


def log(text):
    sys.stderr.write("[ruby-lsp] %s\n" % text)
    sys.stderr.flush()


def shared_unavailable(sock_path):
    """Why the shared hub can't be used here, or None when it can."""
    if os.environ.get("RUBY_LSP_PLUGIN_SHARED", "1") == "0":
        return "RUBY_LSP_PLUGIN_SHARED=0"
    if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
        return "no unix sockets on this platform"
    if len(os.fsencode(sock_path)) > MAX_SOCKET_PATH:
        return "hub socket path is too long: %s" % sock_path
    return None


def spawn_hub(project_dir):
    """Start the hub fully detached (double fork), without any CLAUDE_* variable.

    A plain child would sit in this session's process tree; the double fork
    reparents it to init, so stopping or freezing one session never takes the
    shared hub with it. The hub's own flock keeps it a singleton, so a spare
    spawn just exits.
    """
    argv = [sys.executable, HUB_SCRIPT, "serve", "--project", project_dir]
    env = lsp_hub.clean_env()
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            if os.fork() > 0:
                os._exit(0)
            os.chdir(project_dir)
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            os.closerange(3, 1024)
            os.execve(sys.executable, argv, env)
        finally:
            os._exit(127)
    os.waitpid(pid, 0)


def connect(sock_path, project_dir, deadline=CONNECT_DEADLINE):
    stop_at = time.monotonic() + deadline
    last_spawn = None
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(sock_path)
            return sock
        except OSError:
            sock.close()
        now = time.monotonic()
        if now >= stop_at:
            return None
        if last_spawn is None or now - last_spawn >= RESPAWN_INTERVAL:
            spawn_hub(project_dir)
            last_spawn = now
        time.sleep(0.05)


def pump(sock):
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()

    def upstream():
        try:
            while True:
                data = os.read(stdin_fd, 65536)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    sender = threading.Thread(target=upstream, daemon=True)
    sender.start()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            view = memoryview(data)
            while view:
                view = view[os.write(stdout_fd, view):]
    except OSError:
        pass
    if sender.is_alive():
        log("hub closed the connection; exiting so Claude Code restarts the server")
        return 1
    return 0


def main():
    project_dir = os.getcwd()
    try:
        sock_path = os.path.join(lsp_hub.state_dir(project_dir), "hub.sock")
        reason = shared_unavailable(sock_path)
    except OSError as exc:
        reason = "cannot create the hub state dir: %s" % exc
    if reason is None:
        sock = connect(sock_path, project_dir)
        if sock is not None:
            return pump(sock)
        reason = "hub did not come up within %d s" % CONNECT_DEADLINE
    log("running solo (%s)" % reason)
    return lsp_hub.solo(project_dir)


if __name__ == "__main__":
    sys.exit(main())
