#!/usr/bin/env python3
"""Minimal LSP server standing in for ruby-lsp in the hub tests.

Every message it receives is appended to $FAKE_LSP_LOG as one JSON line
({"pid", "kind", "message"}), so a test can assert what the hub sent and in
which order. Diagnostics carry the document text ("text=<text>"), which shows
whether the hub kept the backend's copy in sync.

  fake/crash    -> exit 3 at once (a backend crash)
  fake/slow     -> sleep params.seconds, then answer
  fake/refresh  -> send workspace/diagnostic/refresh, then answer

FAKE_LSP_PULL=1 behaves like ruby-lsp 0.26: it advertises diagnosticProvider,
never pushes diagnostics, and answers textDocument/diagnostic instead.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import lsp_jsonrpc as rpc  # noqa: E402

CAPABILITIES = {
    "capabilities": {
        "positionEncoding": "utf-16",
        "textDocumentSync": {"openClose": True, "change": 2, "save": True},
        "hoverProvider": True,
    },
    "serverInfo": {"name": "fake-lsp", "version": "1"},
}


def record(kind, message=None):
    path = os.environ.get("FAKE_LSP_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "kind": kind, "message": message}) + "\n")


def send(message):
    sys.stdout.buffer.write(rpc.encode(message))
    sys.stdout.buffer.flush()


def respond(message_id, result):
    send({"jsonrpc": "2.0", "id": message_id, "result": result})


PULL = os.environ.get("FAKE_LSP_PULL") == "1"


def diagnostics_for(text):
    return [{
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
        "severity": 2,
        "message": "text=" + text,
    }]


def publish(uri, text, version):
    if PULL:
        return
    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {
        "uri": uri, "version": version, "diagnostics": diagnostics_for(text),
    }})


def main():
    time.sleep(float(os.environ.get("FAKE_LSP_START_DELAY", "0")))
    record("start")
    docs = {}
    while True:
        message = rpc.read_message_sync(sys.stdin.buffer)
        if message is None:
            record("eof")
            return 0
        record("recv", message)
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            result = json.loads(json.dumps(CAPABILITIES))
            if PULL:
                result["capabilities"]["diagnosticProvider"] = {"interFileDependencies": False}
            respond(message_id, result)
        elif method == "initialized":
            # Server-to-client requests: the hub must answer these itself.
            send({"jsonrpc": "2.0", "id": "s1", "method": "client/registerCapability",
                  "params": {"registrations": []}})
            send({"jsonrpc": "2.0", "id": "s2", "method": "workspace/configuration",
                  "params": {"items": [{"section": "rubyLsp"}]}})
        elif method == "textDocument/didOpen":
            document = params["textDocument"]
            docs[document["uri"]] = document["text"]
            publish(document["uri"], document["text"], document["version"])
        elif method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            docs[uri] = rpc.apply_changes(docs[uri], params["contentChanges"])
            publish(uri, docs[uri], params["textDocument"]["version"])
        elif method == "textDocument/didClose":
            docs.pop(params["textDocument"]["uri"], None)
        elif method == "textDocument/hover":
            respond(message_id, {"contents": "hover:" + docs.get(params["textDocument"]["uri"], "<closed>")})
        elif method == "textDocument/diagnostic":
            uri = params["textDocument"]["uri"]
            report = {"kind": "full", "items": diagnostics_for(docs[uri])} if uri in docs else None
            respond(message_id, report)
        elif method == "fake/refresh":
            send({"jsonrpc": "2.0", "id": "r1", "method": "workspace/diagnostic/refresh"})
            respond(message_id, "refreshed")
        elif method == "fake/slow":
            time.sleep(params.get("seconds", 1))
            respond(message_id, "slow-done")
        elif method == "fake/crash":
            os._exit(3)
        elif method == "shutdown":
            respond(message_id, None)
        elif method == "exit":
            return 0
        elif method is not None and message_id is not None:
            send({"jsonrpc": "2.0", "id": message_id, "error": {"code": -32601, "message": "unknown"}})


if __name__ == "__main__":
    sys.exit(main())
