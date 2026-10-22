#!/usr/bin/env python3
"""Shared, lazy, idle-stopping ruby-lsp for every Claude Code session in one checkout.

Claude Code treats an LSP server that exits as a crash and restarts it, so the
process it spawns (lsp_bridge.py) must stay up for the whole session. The heavy
part, the real ruby-lsp backend, lives behind this hub and comes and goes:

  - initialize is answered from a per-checkout capabilities cache, so a session
    that never writes Ruby never starts the backend;
  - the backend starts on the first document notification (Claude Code sends
    didOpen/didChange/didSave only for Write, Edit and the LSP tool, never for a
    plain Read) or the first request;
  - on start, the hub replays initialize, initialized, and didOpen for the
    documents that triggered the start; other documents follow when touched;
  - after RUBY_LSP_PLUGIN_IDLE_MINUTES without client traffic the backend gets
    shutdown + exit, then `docker rm -f` on its container as a fallback;
  - the hub exits RUBY_LSP_PLUGIN_HUB_LINGER seconds after its last client.

Request ids are rewritten per client, publishDiagnostics goes only to clients
whose copy of the document matches the hub's, and server-to-client requests
are answered by the hub itself.

  lsp_hub.py serve --project DIR   detached daemon; lsp_bridge.py starts it

Standard library only. State lives in ~/.claude/.ruby-lsp-plugin/hubs/<key>/.
"""

import argparse
import asyncio
import collections
import hashlib
import json
import os
import shlex
import shutil
import sys
import threading
import time
import traceback

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

import lsp_jsonrpc as rpc  # noqa: E402

WRAPPER = os.path.join(SCRIPTS_DIR, "run-ruby-tool.sh")

REQUEST_CANCELLED = -32800
REQUEST_FAILED = -32803
METHOD_NOT_FOUND = -32601

START_TIMEOUT = 600.0
SHUTDOWN_GRACE = 5.0
DOCKER_RM_TIMEOUT = 30.0
CRASH_LIMIT = 3
CRASH_WINDOW = 300.0
LOG_ROTATE_BYTES = 1024 * 1024

DOCUMENT_NOTIFICATIONS = (
    "textDocument/didOpen",
    "textDocument/didChange",
    "textDocument/didSave",
    "textDocument/didClose",
)
NULL_ANSWERS = (
    "client/registerCapability",
    "client/unregisterCapability",
    "window/workDoneProgress/create",
    "window/showMessageRequest",
)


class BackendGone(Exception):
    """The backend exited before answering."""


class BackendError(Exception):
    """The backend answered with a JSON-RPC error."""


def project_key(project_dir):
    real = os.path.realpath(project_dir)
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:12]


def container_name(project_dir):
    """Must match hub_container() in run-ruby-tool.sh: Reek finds the container by it."""
    return "ruby-lsp-" + project_key(project_dir)


def state_dir(project_dir):
    path = os.path.join(os.path.expanduser("~"), ".claude", ".ruby-lsp-plugin", "hubs", project_key(project_dir))
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _float_env(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return float(default)


def idle_seconds():
    return _float_env("RUBY_LSP_PLUGIN_IDLE_MINUTES", 15) * 60.0


def linger_seconds():
    return _float_env("RUBY_LSP_PLUGIN_HUB_LINGER", 60)


def backend_command():
    """(argv, is_default). RUBY_LSP_PLUGIN_BACKEND swaps the backend, mostly for tests."""
    override = os.environ.get("RUBY_LSP_PLUGIN_BACKEND", "").strip()
    if override:
        return shlex.split(override), False
    return ["bash", WRAPPER, "lsp-backend"], True


def clean_env():
    """The hub serves many sessions, so it must not look like part of any one of them.

    resource-guard freezes processes whose environ carries CLAUDE_PID; a hub
    frozen with one session would hang every other session in the checkout.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")}


def _request(message_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _notification(method, params=None):
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def _response(message_id, result):
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id, code, text):
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def _document_uri(message):
    params = message.get("params")
    if isinstance(params, dict) and isinstance(params.get("textDocument"), dict):
        return params["textDocument"].get("uri")
    return None


class Log:
    def __init__(self, stream):
        self.stream = stream

    def __call__(self, text):
        try:
            self.stream.write("[ruby-lsp] %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
            self.stream.flush()
        except (OSError, ValueError):
            pass


class Peer:
    """One end of an LSP stream. Writes go through a queue, so a slow peer never blocks the hub."""

    def __init__(self, write, close):
        self._write = write
        self._close = close
        self._queue = asyncio.Queue()
        self.closed = False
        self.task = asyncio.ensure_future(self._pump())

    def send(self, message):
        if not self.closed:
            self._queue.put_nowait(rpc.encode(message))

    def close(self):
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(None)

    async def _pump(self):
        while True:
            data = await self._queue.get()
            if data is None:
                break
            try:
                await self._write(data)
            except (OSError, RuntimeError):
                break
        self.closed = True
        try:
            self._close()
        except (OSError, RuntimeError):
            pass


class Client:
    def __init__(self, client_id, peer):
        self.id = client_id
        self.peer = peer
        self.docs = {}  # uri -> text as this client last sent it


class Backend:
    def __init__(self, proc, peer):
        self.proc = proc
        self.peer = peer
        self.stopping = False


class Hub:
    def __init__(self, project_dir, command, log, idle=900.0, linger=60.0, env=None,
                 cache_path=None, container=None, backend_stderr=None):
        self.project_dir = project_dir
        self.command = command
        self.log = log
        self.idle = idle
        self.linger = linger
        self.env = dict(env if env is not None else os.environ)
        self.cache_path = cache_path
        self.container = container
        self.backend_stderr = backend_stderr
        if container:
            self.env["RUBY_LSP_PLUGIN_HUB_CONTAINER"] = container

        self.clients = {}
        self.next_client = 0
        self.docs = {}  # uri -> {"text", "languageId"}: the newest text any client sent
        self.versions = {}  # uri -> last version sent to the backend
        self.backend = None
        self.state = "stopped"  # stopped -> starting -> ready -> stopping -> stopped
        self.backend_docs = {}  # uri -> text the backend holds
        self.touched = set()  # uris to sync once the backend is ready
        self.pending = {}  # backend id -> ("client", client, id) | ("future", future)
        self.queue = []  # (client, request) waiting for a ready backend
        self.init_waiters = []  # (client, id) waiting for an uncached initialize
        self.init_params = None
        self.next_id = 0
        self.last_activity = time.monotonic()
        self.crashes = collections.deque()
        self.crash_locked = False
        self.restart_wanted = False
        self.linger_handle = None
        self.done = asyncio.Event()
        self.tasks = set()
        self.encoding = "utf-16"
        self.sync_kind = 2
        self.init_result = self._load_cache()
        self._apply_capabilities(self.init_result)

    # -- clients -------------------------------------------------------------

    def add_client(self, peer):
        self.next_client += 1
        client = Client(self.next_client, peer)
        self.clients[client.id] = client
        if self.linger_handle is not None:
            self.linger_handle.cancel()
            self.linger_handle = None
        if self.crash_locked:
            self.crash_locked = False
            self.crashes.clear()
            self.log("new session connected, backend may start again")
        return client

    def remove_client(self, client):
        if self.clients.pop(client.id, None) is None:
            return
        client.peer.close()
        uris = list(client.docs)
        client.docs.clear()
        for uri in uris:
            self._release(uri)
        for backend_id, entry in list(self.pending.items()):
            if entry[0] == "client" and entry[1] is client:
                del self.pending[backend_id]
                if self.backend is not None:
                    self.backend.peer.send(_notification("$/cancelRequest", {"id": backend_id}))
        self.queue = [(owner, message) for owner, message in self.queue if owner is not client]
        self.init_waiters = [(owner, mid) for owner, mid in self.init_waiters if owner is not client]
        if not self.clients:
            self.schedule_exit()

    def schedule_exit(self):
        if self.linger_handle is None and not self.clients:
            self.linger_handle = asyncio.get_running_loop().call_later(self.linger, self._linger_expired)

    def _linger_expired(self):
        self.linger_handle = None
        if not self.clients:
            self.done.set()

    def on_client_message(self, client, message):
        if client.id not in self.clients:
            return
        self.last_activity = time.monotonic()
        method = message.get("method")
        if not isinstance(method, str):
            return  # a response: the hub answers server requests itself, so none are expected
        if "id" in message:
            self._client_request(client, message)
        else:
            self._client_notification(client, method, message)

    def _client_request(self, client, message):
        method = message["method"]
        if method == "initialize":
            self.init_params = message.get("params") or {}
            if self.init_result is not None:
                client.peer.send(_response(message["id"], self.init_result))
            elif self._ensure_backend():
                self.init_waiters.append((client, message["id"]))
            else:
                client.peer.send(_error(message["id"], REQUEST_FAILED, "ruby-lsp backend is unavailable"))
            return
        if method == "shutdown":
            client.peer.send(_response(message["id"], None))
            return
        if self.state == "ready":
            self._forward_request(client, message)
        elif self._ensure_backend():
            self.queue.append((client, message))
        else:
            client.peer.send(_error(message["id"], REQUEST_FAILED, "ruby-lsp backend is unavailable"))

    def _client_notification(self, client, method, message):
        params = message.get("params") or {}
        if method in ("initialized", "$/setTrace"):
            return
        if method == "exit":
            self.remove_client(client)
        elif method == "$/cancelRequest":
            self._cancel(client, params.get("id"))
        elif method in DOCUMENT_NOTIFICATIONS:
            self._document(client, method, params)
        elif self.state == "ready":
            self.backend.peer.send(message)

    def _cancel(self, client, client_id):
        for backend_id, entry in self.pending.items():
            if entry[0] == "client" and entry[1] is client and entry[2] == client_id:
                self.backend.peer.send(_notification("$/cancelRequest", {"id": backend_id}))
                return
        for index, (owner, message) in enumerate(self.queue):
            if owner is client and message.get("id") == client_id:
                del self.queue[index]
                client.peer.send(_error(client_id, REQUEST_CANCELLED, "request cancelled"))
                return

    # -- documents -----------------------------------------------------------

    def _document(self, client, method, params):
        document = params.get("textDocument") or {}
        uri = document.get("uri")
        if not isinstance(uri, str):
            return
        if method == "textDocument/didClose":
            client.docs.pop(uri, None)
            self._release(uri)
            return
        if method == "textDocument/didSave":
            if uri in client.docs:
                self._touch(uri, save=params)
            return
        if method == "textDocument/didOpen":
            text = document.get("text", "")
            language = document.get("languageId", "ruby")
        else:
            base = client.docs.get(uri)
            if base is None:
                base = self.docs.get(uri, {}).get("text", "")
            text = rpc.apply_changes(base, params.get("contentChanges") or [], self.encoding)
            language = self.docs.get(uri, {}).get("languageId", "ruby")
        client.docs[uri] = text
        self.docs[uri] = {"text": text, "languageId": language}
        self._touch(uri)

    def _touch(self, uri, save=None):
        """A client wrote this document: sync it now, or start the backend and sync it then."""
        if self.state == "ready":
            self._sync_doc(uri)
            if save is not None:
                self.backend.peer.send(_notification("textDocument/didSave", save))
            return
        self.touched.add(uri)
        self._ensure_backend()

    def _release(self, uri):
        if any(uri in client.docs for client in self.clients.values()):
            return
        self.docs.pop(uri, None)
        self.touched.discard(uri)
        if self.state == "ready" and self.backend_docs.pop(uri, None) is not None:
            self.backend.peer.send(_notification("textDocument/didClose", {"textDocument": {"uri": uri}}))

    def _sync_doc(self, uri):
        """Bring the backend's copy of uri up to date with the hub's, in one message at most.

        Claude Code sends every didChange as the whole text with no range, and
        each session numbers versions on its own. The backend sees one version
        sequence owned by the hub, and each change as a single ranged edit that
        replaces the whole old text (ruby-lsp only handles ranged edits).
        """
        if uri is None or self.backend is None:
            return
        doc = self.docs.get(uri)
        if doc is None:
            return
        held = self.backend_docs.get(uri)
        if held == doc["text"]:
            return
        version = self.versions.get(uri, 0) + 1
        self.versions[uri] = version
        if held is None:
            self.backend.peer.send(_notification("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": doc["languageId"], "version": version, "text": doc["text"],
            }}))
        else:
            change = {"text": doc["text"]}
            if self.sync_kind == 2:
                change["range"] = {
                    "start": {"line": 0, "character": 0},
                    "end": rpc.end_position(held, self.encoding),
                }
            self.backend.peer.send(_notification("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [change],
            }))
        self.backend_docs[uri] = doc["text"]
        if self.pull_diagnostics:
            self._pull_diagnostics(uri)

    def _pull_diagnostics(self, uri):
        """Ask a pull-only backend for diagnostics; the answer is pushed to clients.

        ruby-lsp (0.26) only serves textDocument/diagnostic, and Claude Code only
        listens for publishDiagnostics, so without this no RuboCop offense would
        ever reach Claude.
        """
        backend_id = self._new_id()
        self.pending[backend_id] = ("pull", uri, self.docs[uri]["text"])
        self.backend.peer.send(_request(backend_id, "textDocument/diagnostic", {"textDocument": {"uri": uri}}))

    # -- backend -------------------------------------------------------------

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def _spawn_task(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _ensure_backend(self):
        """True when the backend is up or on its way; False when it may not start."""
        if self.state in ("ready", "starting"):
            return True
        if self.state == "stopping":
            self.restart_wanted = True
            return True
        if self.crash_locked or self.init_params is None:
            return False
        self.state = "starting"
        self._spawn_task(self._start())
        return True

    def _forward_request(self, client, message):
        self._sync_doc(_document_uri(message))
        backend_id = self._new_id()
        self.pending[backend_id] = ("client", client, message["id"])
        forwarded = dict(message)
        forwarded["id"] = backend_id
        self.backend.peer.send(forwarded)

    async def _start(self):
        self.log("starting backend: %s" % " ".join(self.command))
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self.backend_stderr,
                cwd=self.project_dir,
                env=self.env,
            )
        except OSError as exc:
            self._backend_gone("cannot start backend: %s" % exc, crashed=True)
            return

        async def write(data):
            proc.stdin.write(data)
            await proc.stdin.drain()

        backend = Backend(proc, Peer(write, proc.stdin.close))
        self.backend = backend
        self._spawn_task(self._read_backend(backend))

        params = json.loads(json.dumps(self.init_params))
        params["processId"] = None  # the client pid means nothing inside a container
        # Lets ruby-lsp ask for a re-pull after a .rubocop.yml change; see _answer.
        capabilities = params.setdefault("capabilities", {})
        capabilities.setdefault("workspace", {})["diagnostics"] = {"refreshSupport": True}
        future = asyncio.get_running_loop().create_future()
        init_id = self._new_id()
        self.pending[init_id] = ("future", future)
        backend.peer.send(_request(init_id, "initialize", params))
        try:
            result = await asyncio.wait_for(future, START_TIMEOUT)
        except BackendGone:
            return
        except (BackendError, asyncio.TimeoutError) as exc:
            self.log("backend failed to initialize: %s" % (exc or "timed out"))
            self._kill(backend)
            return
        if self.backend is not backend:
            return

        self.init_result = result
        self._apply_capabilities(result)
        self._save_cache(result)
        backend.peer.send(_notification("initialized", {}))
        self.state = "ready"
        self.last_activity = time.monotonic()
        self.log("backend ready")
        waiters, self.init_waiters = self.init_waiters, []
        for client, message_id in waiters:
            client.peer.send(_response(message_id, result))
        touched, self.touched = self.touched, set()
        for uri in sorted(touched):
            self._sync_doc(uri)
        queued, self.queue = self.queue, []
        for client, message in queued:
            if client.id in self.clients:
                self._forward_request(client, message)

    async def _read_backend(self, backend):
        try:
            while True:
                message = await rpc.read_message(backend.proc.stdout)
                if message is None:
                    break
                self._backend_message(backend, message)
        except rpc.FramingError as exc:
            self.log("backend sent a bad frame: %s" % exc)
            self._kill(backend)
        except (OSError, ValueError) as exc:
            self.log("backend read failed: %s" % exc)
            self._kill(backend)
        code = await backend.proc.wait()
        if self.backend is backend and not backend.stopping:
            self._backend_gone("backend exited with code %s" % code, crashed=True)

    def _backend_message(self, backend, message):
        if self.backend is not backend:
            return
        method = message.get("method")
        if not isinstance(method, str):
            self._backend_response(message)
        elif "id" in message:
            backend.peer.send(self._answer(message))
        elif method == "textDocument/publishDiagnostics":
            self._route_diagnostics(message.get("params") or {})
        elif method in ("window/logMessage", "window/showMessage"):
            self.log("backend: %s" % str((message.get("params") or {}).get("message", ""))[:300])

    def _backend_response(self, message):
        entry = self.pending.pop(message.get("id"), None)
        if entry is None:
            return
        if entry[0] == "future":
            future = entry[1]
            if not future.done():
                if "error" in message:
                    future.set_exception(BackendError(json.dumps(message["error"])))
                else:
                    future.set_result(message.get("result"))
            return
        if entry[0] == "pull":
            _, uri, text = entry
            result = message.get("result")
            doc = self.docs.get(uri)
            # A report for text that has changed since is stale; the newer pull is on its way.
            if isinstance(result, dict) and result.get("kind") == "full" and doc is not None and doc["text"] == text:
                self._route_diagnostics({"uri": uri, "diagnostics": result.get("items") or []})
            return
        _, client, client_id = entry
        self.last_activity = time.monotonic()  # the idle window starts when a slow request ends
        reply = {"jsonrpc": "2.0", "id": client_id}
        if "error" in message:
            reply["error"] = message["error"]
        else:
            reply["result"] = message.get("result")
        client.peer.send(reply)

    def _answer(self, message):
        """Answer a server-to-client request the way Claude Code would, without bothering a client."""
        method = message["method"]
        params = message.get("params") or {}
        if method == "workspace/configuration":
            return _response(message["id"], [None for _ in params.get("items") or []])
        if method == "workspace/applyEdit":
            return _response(message["id"], {"applied": False, "failureReason": "not supported by the ruby-lsp hub"})
        if method == "workspace/diagnostic/refresh" and self.pull_diagnostics:
            for uri in sorted(self.backend_docs):
                self._pull_diagnostics(uri)
            return _response(message["id"], None)
        if method in NULL_ANSWERS or (method.startswith("workspace/") and method.endswith("/refresh")):
            return _response(message["id"], None)
        return _error(message["id"], METHOD_NOT_FOUND, "unhandled method %s" % method)

    def _route_diagnostics(self, params):
        """Send diagnostics only to clients whose copy of the document is the one the hub holds.

        Session B's copy goes stale when session A edits the same file; B should
        not get diagnostics for A's edits.
        """
        uri = params.get("uri")
        doc = self.docs.get(uri)
        if doc is None:
            return
        routed = {key: value for key, value in params.items() if key != "version"}
        message = _notification("textDocument/publishDiagnostics", routed)
        for client in self.clients.values():
            if client.docs.get(uri) == doc["text"]:
                client.peer.send(message)

    def _backend_gone(self, reason, crashed):
        self.log(reason)
        self.backend = None
        self.state = "stopped"
        self.backend_docs = {}
        pending, self.pending = self.pending, {}
        failure = "ruby-lsp backend stopped: %s" % reason
        for entry in pending.values():
            if entry[0] == "future":
                if not entry[1].done():
                    entry[1].set_exception(BackendGone(reason))
            elif entry[0] == "client":
                entry[1].peer.send(_error(entry[2], REQUEST_FAILED, failure))
        if not crashed:
            return
        queued, self.queue = self.queue, []
        for client, message in queued:
            client.peer.send(_error(message["id"], REQUEST_FAILED, failure))
        waiters, self.init_waiters = self.init_waiters, []
        for client, message_id in waiters:
            client.peer.send(_error(message_id, REQUEST_FAILED, failure))
        self.touched.clear()
        now = time.monotonic()
        self.crashes.append(now)
        while self.crashes and now - self.crashes[0] > CRASH_WINDOW:
            self.crashes.popleft()
        if len(self.crashes) >= CRASH_LIMIT and not self.crash_locked:
            self.crash_locked = True
            self.log("backend crashed %d times in %d s; it stays down until a new session connects"
                     % (CRASH_LIMIT, CRASH_WINDOW))

    def _kill(self, backend):
        try:
            backend.proc.kill()
        except ProcessLookupError:
            pass

    async def _stop(self, reason):
        if self.state != "ready":
            return
        backend = self.backend
        backend.stopping = True
        self.state = "stopping"
        self.log("stopping backend (%s)" % reason)
        future = asyncio.get_running_loop().create_future()
        shutdown_id = self._new_id()
        self.pending[shutdown_id] = ("future", future)
        backend.peer.send(_request(shutdown_id, "shutdown"))
        try:
            await asyncio.wait_for(future, SHUTDOWN_GRACE)
        except (BackendGone, BackendError, asyncio.TimeoutError):
            pass
        backend.peer.send(_notification("exit"))
        backend.peer.close()
        try:
            await asyncio.wait_for(backend.proc.wait(), SHUTDOWN_GRACE)
        except asyncio.TimeoutError:
            self._kill(backend)
            await backend.proc.wait()
        await self._remove_container()
        if self.backend is backend:
            self._backend_gone("backend stopped (%s)" % reason, crashed=False)
        if self.restart_wanted:
            self.restart_wanted = False
            self._ensure_backend()

    async def _remove_container(self):
        """Killing `docker compose run` can leave its container running; remove it by name."""
        if not self.container or shutil.which("docker") is None:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", self.container,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return
        try:
            await asyncio.wait_for(proc.wait(), DOCKER_RM_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()

    async def idle_watch(self):
        if self.idle <= 0:
            return
        interval = min(30.0, max(0.1, self.idle / 4))
        while True:
            await asyncio.sleep(interval)
            if self.state != "ready":
                continue
            if any(entry[0] == "client" for entry in self.pending.values()):
                continue
            if time.monotonic() - self.last_activity >= self.idle:
                await self._stop("idle for %d s" % self.idle)

    async def close(self):
        if self.state == "ready":
            await self._stop("hub exiting")
        elif self.backend is not None:
            backend = self.backend
            backend.stopping = True
            self._kill(backend)
            await backend.proc.wait()
            await self._remove_container()
        for client in list(self.clients.values()):
            client.peer.close()
        peers = [client.peer.task for client in self.clients.values()]
        if peers:
            await asyncio.wait(peers, timeout=1.0)

    # -- capabilities cache --------------------------------------------------

    def _apply_capabilities(self, result):
        capabilities = (result or {}).get("capabilities") or {}
        self.encoding = capabilities.get("positionEncoding") or "utf-16"
        # Present means supported, even as an empty {} of options.
        self.pull_diagnostics = capabilities.get("diagnosticProvider") not in (None, False)
        sync = capabilities.get("textDocumentSync")
        if isinstance(sync, dict):
            self.sync_kind = sync.get("change", 1)
        elif isinstance(sync, int):
            self.sync_kind = sync
        else:
            self.sync_kind = 1

    def _load_cache(self):
        if not self.cache_path:
            return None
        try:
            with open(self.cache_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        result = data.get("result") if isinstance(data, dict) else None
        if isinstance(result, dict) and isinstance(result.get("capabilities"), dict):
            return result
        return None

    def _save_cache(self, result):
        if not self.cache_path or not isinstance(result, dict) or result == self._load_cache():
            return
        tmp = "%s.%d.tmp" % (self.cache_path, os.getpid())
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"result": result, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, handle)
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            self.log("cannot write capabilities cache: %s" % exc)


def build_hub(project_dir, log, shared, cache_dir=None, backend_stderr=None):
    command, is_default = backend_command()
    container = None
    if is_default:
        container = container_name(project_dir)
        if not shared:
            # Two solo sessions in one checkout must not remove each other's container.
            container = "%s-solo-%d" % (container, os.getpid())
    return Hub(
        project_dir,
        command,
        log,
        idle=idle_seconds(),
        linger=linger_seconds() if shared else 0.0,
        cache_path=os.path.join(cache_dir, "capabilities.json") if cache_dir else None,
        container=container,
        backend_stderr=backend_stderr,
    )


async def _serve(project_dir, directory, log, log_file):
    sock_path = os.path.join(directory, "hub.sock")
    hub = build_hub(project_dir, log, shared=True, cache_dir=directory, backend_stderr=log_file)

    async def handle(reader, writer):
        async def write(data):
            writer.write(data)
            await writer.drain()

        client = hub.add_client(Peer(write, writer.close))
        try:
            while True:
                message = await rpc.read_message(reader)
                if message is None:
                    break
                hub.on_client_message(client, message)
        except (rpc.FramingError, OSError) as exc:
            log("client %d dropped: %s" % (client.id, exc))
        finally:
            hub.remove_client(client)

    if os.path.exists(sock_path):
        os.unlink(sock_path)
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(handle, path=sock_path)
    finally:
        os.umask(old_umask)
    log("hub up for %s (idle stop %s s, linger %s s)" % (project_dir, hub.idle, hub.linger))
    hub.schedule_exit()  # the bridge that spawned the hub connects within milliseconds
    idle_task = asyncio.ensure_future(hub.idle_watch())
    await hub.done.wait()
    server.close()
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    idle_task.cancel()
    await hub.close()


def serve(project_dir):
    import fcntl

    directory = state_dir(project_dir)
    lock = open(os.path.join(directory, "hub.lock"), "a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # another hub owns this checkout
    lock.seek(0)
    lock.truncate()
    lock.write("%d\n" % os.getpid())
    lock.flush()
    log_path = os.path.join(directory, "hub.log")
    try:
        if os.path.getsize(log_path) > LOG_ROTATE_BYTES:
            os.replace(log_path, log_path + ".1")
    except OSError:
        pass
    log_file = open(log_path, "a", buffering=1, encoding="utf-8")
    log = Log(log_file)
    try:
        asyncio.run(_serve(project_dir, directory, log, log_file))
    except Exception:  # noqa: BLE001 -- a daemon has nowhere else to report
        log("hub crashed:\n%s" % traceback.format_exc())
        return 1
    finally:
        log("hub exited")
    return 0


async def _solo(project_dir, stdin, stdout):
    """The hub in-process, with stdio as its only client: lazy and idle-stopping, not shared."""
    log = Log(sys.stderr)
    try:
        cache_dir = state_dir(project_dir)
    except OSError:
        cache_dir = None
    hub = build_hub(project_dir, log, shared=False, cache_dir=cache_dir)
    loop = asyncio.get_running_loop()

    def write_sync(data):
        stdout.write(data)
        stdout.flush()

    async def write(data):
        await loop.run_in_executor(None, write_sync, data)

    client = hub.add_client(Peer(write, lambda: None))

    def read_stdin():
        try:
            while True:
                message = rpc.read_message_sync(stdin)
                if message is None:
                    break
                loop.call_soon_threadsafe(hub.on_client_message, client, message)
        except (rpc.FramingError, OSError, ValueError) as exc:
            log("stdin dropped: %s" % exc)
        loop.call_soon_threadsafe(hub.remove_client, client)

    threading.Thread(target=read_stdin, daemon=True).start()
    idle_task = asyncio.ensure_future(hub.idle_watch())
    await hub.done.wait()
    idle_task.cancel()
    await hub.close()


def solo(project_dir, stdin=None, stdout=None):
    asyncio.run(_solo(project_dir, stdin or sys.stdin.buffer, stdout or sys.stdout.buffer))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve", help="run the detached hub for one checkout")
    serve_parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    return serve(os.path.abspath(args.project))


if __name__ == "__main__":
    sys.exit(main())
