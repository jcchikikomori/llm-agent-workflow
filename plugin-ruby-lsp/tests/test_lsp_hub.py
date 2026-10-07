#!/usr/bin/env python3
"""Behavior tests for scripts/lsp_hub.py and scripts/lsp_bridge.py.

tests/fake_lsp_server.py stands in for ruby-lsp through RUBY_LSP_PLUGIN_BACKEND,
so no Docker or Ruby is needed. Each test runs a real hub process against a
temp HOME and talks LSP to it, over the hub's unix socket or a bridge's stdio.
The fake logs every message it receives, which is what most assertions read.

  python3 -m unittest discover -s plugin-ruby-lsp/tests
"""

import asyncio
import fcntl
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lsp_hub  # noqa: E402
import lsp_jsonrpc as rpc  # noqa: E402

FAKE = TESTS / "fake_lsp_server.py"
URI = "file:///project/app/models/order.rb"
TIMEOUT = 10.0
CACHED = {
    "capabilities": {
        "positionEncoding": "utf-16",
        "textDocumentSync": {"openClose": True, "change": 2},
        "hoverProvider": True,
    },
    "serverInfo": {"name": "cached"},
}


def sock_recv(sock):
    def recv(timeout):
        sock.settimeout(timeout)
        try:
            return sock.recv(65536)
        except socket.timeout:
            return None

    return recv


def pipe_recv(fd):
    def recv(timeout):
        ready, _, _ = select.select([fd], [], [], timeout)
        return os.read(fd, 65536) if ready else None

    return recv


class Conn:
    """A test-side LSP client over any byte transport."""

    def __init__(self, send, recv):
        self._send = send
        self._recv = recv
        self.buf = bytearray()
        self.inbox = []
        self.next_id = 0

    def send(self, message):
        self._send(rpc.encode(message))

    def request(self, method, params=None, message_id=None):
        if message_id is None:
            self.next_id += 1
            message_id = self.next_id
        message = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return message_id

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def _frame(self):
        sep = self.buf.find(b"\r\n\r\n")
        if sep < 0:
            return None
        length = int(bytes(self.buf[:sep]).split(b":")[1])
        end = sep + 4 + length
        if len(self.buf) < end:
            return None
        body = bytes(self.buf[sep + 4:end])
        del self.buf[:end]
        return json.loads(body)

    def wait_for(self, predicate, timeout=TIMEOUT):
        for index, message in enumerate(self.inbox):
            if predicate(message):
                return self.inbox.pop(index)
        deadline = time.monotonic() + timeout
        while True:
            frame = self._frame()
            while frame is not None:
                if predicate(frame):
                    return frame
                self.inbox.append(frame)
                frame = self._frame()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("no matching message within %ss; inbox: %r" % (timeout, self.inbox))
            data = self._recv(remaining)
            if data is None:
                continue
            if not data:
                raise EOFError("connection closed; inbox: %r" % self.inbox)
            self.buf += data

    def response(self, message_id, timeout=TIMEOUT):
        return self.wait_for(lambda m: m.get("id") == message_id and "method" not in m, timeout)

    def diagnostics(self, timeout=TIMEOUT):
        return self.wait_for(lambda m: m.get("method") == "textDocument/publishDiagnostics", timeout)

    def assert_no(self, predicate, wait=0.6):
        try:
            message = self.wait_for(predicate, wait)
        except AssertionError:
            return
        raise AssertionError("unexpected message: %r" % message)


def initialize(conn):
    message_id = conn.request("initialize", {"processId": 4242, "rootUri": "file:///project", "capabilities": {}})
    result = conn.response(message_id)["result"]
    conn.notify("initialized", {})
    return result


def did_open(conn, text, uri=URI):
    conn.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "ruby", "version": 1, "text": text}})


def did_change(conn, text, version=2, uri=URI):
    # Claude Code sends the whole text with no range.
    conn.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]})


def hover(conn, message_id=None):
    params = {"textDocument": {"uri": URI}, "position": {"line": 0, "character": 0}}
    return conn.request("textDocument/hover", params, message_id)


def diagnostic_text(message):
    return message["params"]["diagnostics"][0]["message"]


class HubTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.fake_log = self.tmp / "fake.log"
        inherited = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("CLAUDE_", "RUBY_LSP_PLUGIN_", "FAKE_LSP_"))
        }
        self.env = {
            **inherited,
            "HOME": str(self.tmp),
            "RUBY_LSP_PLUGIN_BACKEND": "%s %s" % (sys.executable, FAKE),
            "RUBY_LSP_PLUGIN_IDLE_MINUTES": "0",
            "RUBY_LSP_PLUGIN_HUB_LINGER": "30",
            "FAKE_LSP_LOG": str(self.fake_log),
        }
        self.state = self.tmp / ".claude" / ".ruby-lsp-plugin" / "hubs" / lsp_hub.project_key(str(self.project))
        self.state.mkdir(parents=True, mode=0o700)
        self.sock_path = self.state / "hub.sock"
        (self.state / "capabilities.json").write_text(json.dumps({"result": CACHED}))
        self.hubs = []
        self.bridges = []
        self.socks = []

    def tearDown(self):
        for sock in self.socks:
            sock.close()
        for proc in self.bridges:
            if proc.poll() is None:
                proc.kill()
            proc.wait(TIMEOUT)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()
        for proc in self.hubs:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(TIMEOUT)
        self.kill_detached_hub()
        for pid in self.backend_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def hub_running(self):
        lock_path = self.state / "hub.lock"
        if not lock_path.exists():
            return False
        with open(lock_path) as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return False

    def hub_pid(self):
        return int((self.state / "hub.lock").read_text().strip())

    def kill_detached_hub(self):
        if self.hub_running():
            os.kill(self.hub_pid(), signal.SIGTERM)
            self.wait_until(lambda: not self.hub_running(), what="detached hub to exit")

    def hub_log(self):
        path = self.state / "hub.log"
        return path.read_text() if path.exists() else "<no hub.log>"

    def wait_until(self, condition, timeout=TIMEOUT, what="condition"):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting for %s\nhub.log:\n%s" % (what, self.hub_log()))
            time.sleep(0.05)

    def events(self):
        if not self.fake_log.exists():
            return []
        return [json.loads(line) for line in self.fake_log.read_text().splitlines() if line.strip()]

    def received(self, method=None, pid=None):
        return [
            event["message"] for event in self.events()
            if event["kind"] == "recv"
            and (method is None or event["message"].get("method") == method)
            and (pid is None or event["pid"] == pid)
        ]

    def backend_pids(self):
        pids = []
        for event in self.events():
            if event["kind"] == "start" and event["pid"] not in pids:
                pids.append(event["pid"])
        return pids

    def start_hub(self):
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "lsp_hub.py"), "serve", "--project", str(self.project)],
            cwd=self.project, env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.hubs.append(proc)
        return proc

    def connect(self):
        deadline = time.monotonic() + TIMEOUT
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(self.sock_path))
                break
            except OSError:
                sock.close()
                if time.monotonic() > deadline:
                    raise AssertionError("hub socket never answered\nhub.log:\n%s" % self.hub_log())
                time.sleep(0.05)
        self.socks.append(sock)
        return Conn(sock.sendall, sock_recv(sock))

    def start_bridge(self, **env):
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "lsp_bridge.py")],
            cwd=self.project, env={**self.env, **env},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.bridges.append(proc)

        def send(data):
            proc.stdin.write(data)
            proc.stdin.flush()

        return proc, Conn(send, pipe_recv(proc.stdout.fileno()))


class HubStartTests(HubTestCase):
    def test_initialize_from_cache_starts_no_backend(self):
        self.start_hub()
        conn = self.connect()
        self.assertEqual(initialize(conn), CACHED)
        time.sleep(0.5)
        self.assertEqual(self.backend_pids(), [])

    def test_did_open_starts_backend_and_replays_in_order(self):
        self.start_hub()
        conn = self.connect()
        initialize(conn)
        did_open(conn, "x = 1\n")
        diagnostics = conn.diagnostics()
        self.assertEqual(diagnostic_text(diagnostics), "text=x = 1\n")
        self.assertNotIn("version", diagnostics["params"])
        methods = [message.get("method") for message in self.received()]
        self.assertEqual(methods[:3], ["initialize", "initialized", "textDocument/didOpen"])
        params = self.received("initialize")[0]["params"]
        self.assertIsNone(params["processId"])
        self.assertEqual(params["rootUri"], "file:///project")

    def test_whole_text_change_reaches_backend_as_one_ranged_edit(self):
        self.start_hub()
        conn = self.connect()
        initialize(conn)
        did_open(conn, "a\n😀b")
        conn.diagnostics()
        did_change(conn, "c\n")
        self.assertEqual(diagnostic_text(conn.diagnostics()), "text=c\n")
        change = self.received("textDocument/didChange")[0]["params"]
        # "😀" is two UTF-16 units, so the old text ends at line 1, character 3.
        self.assertEqual(change["contentChanges"][0]["range"], {
            "start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 3},
        })
        self.assertEqual(change["textDocument"]["version"], 2)

    def test_cold_request_starts_backend_and_keeps_client_id(self):
        self.start_hub()
        conn = self.connect()
        initialize(conn)
        hover(conn, "client-7")
        self.assertEqual(conn.response("client-7")["result"], {"contents": "hover:<closed>"})
        self.assertNotEqual(self.received("textDocument/hover")[0]["id"], "client-7")

    def test_uncached_initialize_waits_for_backend_and_writes_cache(self):
        (self.state / "capabilities.json").unlink()
        self.start_hub()
        conn = self.connect()
        result = initialize(conn)
        self.assertEqual(result["serverInfo"]["name"], "fake-lsp")
        self.assertEqual(len(self.backend_pids()), 1)
        cache = self.state / "capabilities.json"
        self.wait_until(cache.exists, what="capabilities cache")
        self.assertEqual(json.loads(cache.read_text())["result"], result)
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)

    def test_server_requests_are_answered_by_the_hub(self):
        self.start_hub()
        conn = self.connect()
        initialize(conn)
        did_open(conn, "x\n")
        conn.diagnostics()
        self.wait_until(lambda: len([m for m in self.received() if m.get("id") in ("s1", "s2")]) == 2,
                        what="answers to server requests")
        answers = {m["id"]: m for m in self.received() if m.get("id") in ("s1", "s2")}
        self.assertIsNone(answers["s1"]["result"])
        self.assertEqual(answers["s2"]["result"], [None])
        conn.assert_no(lambda m: m.get("method") in ("client/registerCapability", "workspace/configuration"))

    def test_second_hub_for_same_checkout_exits(self):
        first = self.start_hub()
        self.connect()
        second = self.start_hub()
        self.assertEqual(second.wait(TIMEOUT), 0)
        self.assertIsNone(first.poll())
        self.assertEqual(self.hub_pid(), first.pid)

    def test_state_dir_is_private(self):
        self.start_hub()
        self.connect()
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.sock_path.stat().st_mode & 0o777, 0o600)


class HubSharingTests(HubTestCase):
    def test_two_clients_share_one_backend_without_id_clashes(self):
        self.start_hub()
        a, b = self.connect(), self.connect()
        initialize(a)
        initialize(b)
        hover(a, 1)
        hover(b, 1)
        self.assertIn("result", a.response(1))
        self.assertIn("result", b.response(1))
        self.assertEqual(len(self.backend_pids()), 1)
        backend_ids = [message["id"] for message in self.received("textDocument/hover")]
        self.assertEqual(len(set(backend_ids)), 2)

    def test_diagnostics_go_only_to_clients_holding_the_same_text(self):
        self.start_hub()
        a, b = self.connect(), self.connect()
        initialize(a)
        initialize(b)
        did_open(a, "one\n")
        a.diagnostics()
        did_open(b, "one\n")
        did_change(a, "two\n")
        self.assertEqual(diagnostic_text(a.diagnostics()), "text=two\n")
        b.assert_no(lambda m: m.get("method") == "textDocument/publishDiagnostics")
        did_change(b, "three\n")
        self.assertEqual(diagnostic_text(b.diagnostics()), "text=three\n")
        a.assert_no(lambda m: m.get("method") == "textDocument/publishDiagnostics")

    def test_last_close_closes_document_in_backend(self):
        self.start_hub()
        a, b = self.connect(), self.connect()
        initialize(a)
        initialize(b)
        did_open(a, "x\n")
        a.diagnostics()
        did_open(b, "x\n")
        a.notify("textDocument/didClose", {"textDocument": {"uri": URI}})
        hover(b, 9)
        self.assertEqual(b.response(9)["result"], {"contents": "hover:x\n"})
        b.notify("textDocument/didClose", {"textDocument": {"uri": URI}})
        self.wait_until(lambda: self.received("textDocument/didClose"), what="didClose")
        self.assertEqual(len(self.received("textDocument/didClose")), 1)

    def test_shutdown_and_exit_close_only_that_client(self):
        self.start_hub()
        a, b = self.connect(), self.connect()
        initialize(a)
        initialize(b)
        self.assertIsNone(a.response(a.request("shutdown"))["result"])
        a.notify("exit")
        with self.assertRaises(EOFError):
            a.wait_for(lambda m: False)
        hover(b, 3)
        self.assertIn("result", b.response(3))
        self.assertEqual(self.received("shutdown"), [])


class PullDiagnosticsTests(HubTestCase):
    """ruby-lsp 0.26 only answers textDocument/diagnostic; Claude Code only reads pushes."""

    def setUp(self):
        super().setUp()
        self.env["FAKE_LSP_PULL"] = "1"

    def test_pulled_diagnostics_are_pushed_to_the_writer(self):
        self.start_hub()
        conn = self.connect()
        initialize(conn)
        did_open(conn, "one\n")
        self.assertEqual(diagnostic_text(conn.diagnostics()), "text=one\n")
        did_change(conn, "two\n")
        self.assertEqual(diagnostic_text(conn.diagnostics()), "text=two\n")
        self.assertEqual(len(self.received("textDocument/diagnostic")), 2)
        refresh = self.received("initialize")[0]["params"]["capabilities"]["workspace"]["diagnostics"]
        self.assertEqual(refresh, {"refreshSupport": True})

    def test_refresh_request_re_pulls_open_documents(self):
        self.start_hub()
        conn = self.connect()
        initialize(conn)
        did_open(conn, "x\n")
        conn.diagnostics()
        self.assertEqual(conn.response(conn.request("fake/refresh"))["result"], "refreshed")
        self.assertEqual(diagnostic_text(conn.diagnostics()), "text=x\n")
        self.wait_until(lambda: [m for m in self.received() if m.get("id") == "r1"], what="refresh answer")
        self.assertEqual(len(self.received("textDocument/diagnostic")), 2)



class RecordingPeer:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def close(self):
        self.closed = True


class InProcessHubTests(unittest.TestCase):
    """Orderings a real socket and pipe can't pin down, driven by hand."""

    def run_hub(self, scenario):
        async def main():
            hub = lsp_hub.Hub("/project", ["unused"], log=lambda text: None)
            hub.init_params = {}
            hub._apply_capabilities({"capabilities": {"diagnosticProvider": {}, "textDocumentSync": 2}})
            hub.state = "ready"
            hub.backend = lsp_hub.Backend(None, RecordingPeer())
            client = hub.add_client(RecordingPeer())
            scenario(hub, client)

        asyncio.run(main())

    def test_stale_pull_is_dropped(self):
        def scenario(hub, client):
            hub.on_client_message(client, {"method": "textDocument/didOpen", "params": {
                "textDocument": {"uri": URI, "languageId": "ruby", "version": 1, "text": "a\n"}}})
            hub.on_client_message(client, {"method": "textDocument/didChange", "params": {
                "textDocument": {"uri": URI, "version": 2}, "contentChanges": [{"text": "b\n"}]}})
            pulls = [m["id"] for m in hub.backend.peer.sent if m.get("method") == "textDocument/diagnostic"]
            self.assertEqual(len(pulls), 2)
            report = {"kind": "full", "items": [{"message": "old"}]}
            hub._backend_message(hub.backend, {"id": pulls[0], "result": report})
            self.assertEqual(client.peer.sent, [])
            report = {"kind": "full", "items": [{"message": "new"}]}
            hub._backend_message(hub.backend, {"id": pulls[1], "result": report})
            self.assertEqual(client.peer.sent[0]["params"]["diagnostics"], [{"message": "new"}])

        self.run_hub(scenario)

    def test_null_report_pushes_nothing(self):
        def scenario(hub, client):
            hub.on_client_message(client, {"method": "textDocument/didOpen", "params": {
                "textDocument": {"uri": URI, "languageId": "erb", "version": 1, "text": "<%= 1 %>"}}})
            pull = [m["id"] for m in hub.backend.peer.sent if m.get("method") == "textDocument/diagnostic"][0]
            hub._backend_message(hub.backend, {"id": pull, "result": None})
            self.assertEqual(client.peer.sent, [])

        self.run_hub(scenario)

    def test_backend_exit_ignores_pending_pulls(self):
        def scenario(hub, client):
            hub.on_client_message(client, {"method": "textDocument/didOpen", "params": {
                "textDocument": {"uri": URI, "languageId": "ruby", "version": 1, "text": "x\n"}}})
            hub._backend_gone("test", crashed=False)
            self.assertEqual(client.peer.sent, [])
            self.assertEqual(hub.state, "stopped")

        self.run_hub(scenario)


class HubLifecycleTests(HubTestCase):
    def test_idle_stop_then_restart_replays_current_text(self):
        self.env["RUBY_LSP_PLUGIN_IDLE_MINUTES"] = "0.02"  # 1.2 s
        self.start_hub()
        conn = self.connect()
        initialize(conn)
        did_open(conn, "a = 1\n")
        conn.diagnostics()
        self.wait_until(lambda: self.received("exit"), what="idle stop")
        self.assertEqual(len(self.received("shutdown")), 1)
        did_change(conn, "a = 2\n")
        self.assertEqual(diagnostic_text(conn.diagnostics()), "text=a = 2\n")
        pids = self.backend_pids()
        self.assertEqual(len(pids), 2)
        replay = self.received(pid=pids[1])
        self.assertEqual([m.get("method") for m in replay[:3]],
                         ["initialize", "initialized", "textDocument/didOpen"])
        self.assertEqual(replay[2]["params"]["textDocument"]["text"], "a = 2\n")
        self.assertEqual(replay[2]["params"]["textDocument"]["version"], 2)

    def test_in_flight_request_holds_off_idle_stop(self):
        self.env["RUBY_LSP_PLUGIN_IDLE_MINUTES"] = "0.02"  # 1.2 s
        self.start_hub()
        conn = self.connect()
        initialize(conn)
        message_id = conn.request("fake/slow", {"seconds": 2.5})
        self.assertEqual(conn.response(message_id)["result"], "slow-done")
        time.sleep(0.6)
        self.assertEqual(self.received("shutdown"), [])
        self.wait_until(lambda: self.received("shutdown"), what="idle stop after the request")

    def test_backend_crash_fails_pending_request_and_next_write_restarts(self):
        self.start_hub()
        conn = self.connect()
        initialize(conn)
        did_open(conn, "x\n")
        conn.diagnostics()
        reply = conn.response(conn.request("fake/crash"))
        self.assertEqual(reply["error"]["code"], -32803)
        did_change(conn, "y\n")
        self.assertEqual(diagnostic_text(conn.diagnostics()), "text=y\n")
        self.assertEqual(len(self.backend_pids()), 2)

    def test_crash_loop_keeps_backend_down_until_a_new_session(self):
        self.start_hub()
        a = self.connect()
        initialize(a)
        for _ in range(3):
            self.assertEqual(a.response(a.request("fake/crash"))["error"]["code"], -32803)
        reply = a.response(hover(a))
        self.assertEqual(reply["error"]["code"], -32803)
        self.assertEqual(len(self.backend_pids()), 3)
        b = self.connect()
        initialize(b)
        self.assertIn("result", b.response(hover(b)))
        self.assertEqual(len(self.backend_pids()), 4)

    def test_hub_exits_after_last_client_and_stops_backend(self):
        self.env["RUBY_LSP_PLUGIN_HUB_LINGER"] = "0.5"
        hub = self.start_hub()
        conn = self.connect()
        initialize(conn)
        did_open(conn, "x\n")
        conn.diagnostics()
        self.socks.pop().close()
        self.assertEqual(hub.wait(TIMEOUT), 0)
        self.assertFalse(self.sock_path.exists())
        self.assertEqual(len(self.received("shutdown")), 1)
        self.assertEqual(len(self.received("exit")), 1)


class BridgeTests(HubTestCase):
    def test_bridges_share_one_detached_hub(self):
        self.env["RUBY_LSP_PLUGIN_HUB_LINGER"] = "0.5"
        first, a = self.start_bridge()
        second, b = self.start_bridge()
        initialize(a)
        initialize(b)
        did_open(a, "shared\n")
        a.diagnostics()
        hover(b, 5)
        self.assertEqual(b.response(5)["result"], {"contents": "hover:shared\n"})
        self.assertEqual(len(self.backend_pids()), 1)
        hub_pid = self.hub_pid()
        self.assertNotIn(hub_pid, (first.pid, second.pid))
        stat = Path("/proc/%d/stat" % hub_pid)
        if stat.exists():
            parent = int(stat.read_text().rsplit(")", 1)[1].split()[1])
            self.assertNotIn(parent, (first.pid, second.pid))
            environ = Path("/proc/%d/environ" % hub_pid).read_bytes()
            self.assertNotIn(b"CLAUDE_", environ)
        first.stdin.close()
        second.stdin.close()
        self.assertEqual(first.wait(TIMEOUT), 0)
        self.assertEqual(second.wait(TIMEOUT), 0)
        self.wait_until(lambda: not self.hub_running(), what="hub to exit after the last bridge")

    def test_bridge_does_not_pass_claude_env_to_hub(self):
        _, conn = self.start_bridge(CLAUDE_PID="12345", CLAUDE_PROJECT_DIR=str(self.project))
        initialize(conn)
        environ_path = Path("/proc/%d/environ" % self.hub_pid())
        if not environ_path.exists():
            self.skipTest("no /proc")
        self.assertNotIn(b"CLAUDE_", environ_path.read_bytes())

    def test_bridge_exits_1_when_hub_dies(self):
        proc, conn = self.start_bridge()
        initialize(conn)
        os.kill(self.hub_pid(), signal.SIGKILL)
        self.assertEqual(proc.wait(TIMEOUT), 1)
        self.assertIn("hub closed the connection", proc.stderr.read().decode())

    def test_solo_mode_needs_no_socket(self):
        proc, conn = self.start_bridge(RUBY_LSP_PLUGIN_SHARED="0")
        self.assertEqual(initialize(conn), CACHED)
        did_open(conn, "solo\n")
        self.assertEqual(diagnostic_text(conn.diagnostics()), "text=solo\n")
        self.assertFalse(self.sock_path.exists())
        proc.stdin.close()
        self.assertEqual(proc.wait(TIMEOUT), 0)
        self.assertIn("running solo (RUBY_LSP_PLUGIN_SHARED=0)", proc.stderr.read().decode())
        self.assertEqual(len(self.received("shutdown")), 1)
        self.assertEqual(len(self.received("exit")), 1)


class KeyTests(unittest.TestCase):
    def test_key_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real)
            self.assertEqual(lsp_hub.project_key(str(link)), lsp_hub.project_key(str(real)))
            self.assertRegex(lsp_hub.container_name(str(real)), r"^ruby-lsp-[0-9a-f]{12}$")

    def test_backend_override_is_split_like_a_shell(self):
        old = os.environ.get("RUBY_LSP_PLUGIN_BACKEND")
        os.environ["RUBY_LSP_PLUGIN_BACKEND"] = "python3 'my server.py' --x"
        try:
            self.assertEqual(lsp_hub.backend_command(), (["python3", "my server.py", "--x"], False))
        finally:
            if old is None:
                del os.environ["RUBY_LSP_PLUGIN_BACKEND"]
            else:
                os.environ["RUBY_LSP_PLUGIN_BACKEND"] = old

    def test_idle_minutes_parse(self):
        old = os.environ.get("RUBY_LSP_PLUGIN_IDLE_MINUTES")
        try:
            for raw, seconds in (("2", 120.0), ("0", 0.0), ("junk", 900.0), ("-5", 0.0)):
                os.environ["RUBY_LSP_PLUGIN_IDLE_MINUTES"] = raw
                self.assertEqual(lsp_hub.idle_seconds(), seconds, raw)
        finally:
            if old is None:
                os.environ.pop("RUBY_LSP_PLUGIN_IDLE_MINUTES", None)
            else:
                os.environ["RUBY_LSP_PLUGIN_IDLE_MINUTES"] = old


if __name__ == "__main__":
    unittest.main()
