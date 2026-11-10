"""Stage 1 checks against a real executor HTTP server and the real plugin handlers.

Run: python3 -m unittest discover -s tests
"""

import hashlib
import importlib.util
import json
import os
import socket
import sys
import types
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = "First line: \u0442\u0435\u0441\u0442 🚀\nSecond line"
AGENT_KEY = "agent-key-of-the-tests"
AUTH = {"Authorization": f"Bearer {AGENT_KEY}"}
# Test double of Hermes' agent.secret_scope: the plugin's secrets come from SECRETS (the active profile's
# .env, as get_secret resolves it), never from the process environment.
SECRETS = {}
_scope = types.ModuleType("agent.secret_scope")
_scope.get_secret = lambda name, default=None: SECRETS.get(name, default)
sys.modules.setdefault("agent", types.ModuleType("agent")).secret_scope = _scope
sys.modules.setdefault("agent.secret_scope", _scope)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def connect_agent(executor, url):
    """Give the executor an agent key and the plugin (through get_secret, as Hermes resolves the
    profile's .env) the executor's address and that key."""
    with closing(executor.connect()) as db:
        executor.set_setting(db, "agent_key", hashlib.sha256(AGENT_KEY.encode()).hexdigest())
    SECRETS.update(INTENTRIVET_URL=url, INTENTRIVET_AGENT_KEY=AGENT_KEY)


class FakeContext:
    """Records what the plugin registers, like Hermes' PluginContext does."""

    def __init__(self):
        self.tools, self.commands, self.cli = {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = handler

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, **kwargs):
        self.cli[name] = (setup_fn, handler_fn)


class Stage1Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["INTENTRIVET_DB"] = f"{cls.tmp.name}/db.sqlite3"
        cls.executor = load("executor", ROOT / "executor" / "executor.py")
        cls.default_timeout = cls.executor.Handler.timeout
        cls.executor.Handler.timeout = 0.5  # keep the stalled-request test fast
        with closing(cls.executor.connect()) as db:  # the schema and the channels, as serve() does
            cls.executor.import_legacy_channels(db, ROOT / "stand" / "channels.json")
        cls.server = HTTPServer(("127.0.0.1", 0), cls.executor.Handler)
        cls.server.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        connect_agent(cls.executor, cls.url)
        ctx = FakeContext()
        plugin = load("intentrivet", ROOT / "plugin" / "__init__.py")
        plugin.register(ctx)
        cls.plugin = plugin
        cls.prepare = staticmethod(ctx.tools["intentrivet_prepare_post"])
        cls.show = staticmethod(ctx.commands["intentrivet"])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def http(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=AUTH)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def count(self):
        with closing(self.executor.connect(read_only=True)) as db:
            return db.execute("SELECT COUNT(*) FROM versions").fetchone()[0]

    def raw(self, data, *, finish=False):
        """Send raw request bytes, with the agent key after the request line; return the status line."""
        line, rest = data.split(b"\r\n", 1)
        data = line + f"\r\nAuthorization: Bearer {AGENT_KEY}\r\n".encode() + rest
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5) as s:
            s.sendall(data)
            if finish:
                s.shutdown(socket.SHUT_WR)
            return s.recv(4096).split(b"\r\n", 1)[0].decode()

    def stored(self, version_id):
        status, body = self.http("GET", f"/versions/{version_id}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_prepare_then_revise_creates_new_immutable_version(self):
        first = json.loads(self.prepare({"channel": "test", "text": TEXT}))
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["status"], "Awaiting approval")
        self.assertIn(f"draft_id '{first['draft_id']}', channel 'test'", first["to_revise"])
        self.assertEqual(first["operator_command"], f"/intentrivet {first['version_id']}")
        v1 = self.stored(first["version_id"])
        self.assertEqual(v1["action"], {"method": "sendMessage", "chat_id": -1000000000001, "text": TEXT})

        revised_text = "First line: \u0442\u0435\u0441\u0442 🚀\nSecond line, revised"
        second = json.loads(self.prepare({"channel": "test", "text": revised_text,
                                          "draft_id": first["draft_id"]}))
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["draft_id"], first["draft_id"])
        self.assertNotEqual(second["version_id"], first["version_id"])
        self.assertEqual(self.stored(second["version_id"])["action"]["text"], revised_text)
        self.assertEqual(self.stored(first["version_id"]), v1)

        preview = self.show(f"{second['version_id']}\n/exit")
        self.assertIn(f"----- BEGIN TEXT -----\n{revised_text}\n----- END TEXT -----", preview)
        self.assertIn("Status:   Awaiting approval", preview)
        self.assertIn("chat_id:  -1000000000001", preview)

    def test_command_shows_the_exact_version_across_drafts(self):
        a = json.loads(self.prepare({"channel": "test", "text": "Draft A"}))
        b = json.loads(self.prepare({"channel": "test", "text": "Draft B"}))
        self.assertNotEqual(a["draft_id"], b["draft_id"])
        shown = self.show(a["operator_command"].removeprefix("/intentrivet "))
        self.assertIn(f"Version:  {a['version_id']}", shown)
        self.assertIn("\nDraft A\n", shown)
        self.assertNotIn("Draft B", shown)
        for no_id in ("", "   ", "\n"):
            self.assertEqual(self.show(no_id), self.plugin.USAGE)
        self.assertEqual(self.http("GET", "/versions/latest")[0], 404)

    def test_plugin_rejects_approval_and_chat_id_arguments(self):
        for extra in ({"approved": True}, {"chat_id": 42}, {"status": "Approved"}):
            result = json.loads(self.prepare({"channel": "test", "text": "x", **extra}))
            self.assertFalse(result["ok"])
            self.assertIn("Unsupported argument", result["error"])

    def test_executor_rejects_approval_fields_independently(self):
        status, body = self.http("POST", "/versions", {"channel": "test", "text": "x", "approved": True})
        self.assertEqual(status, 400)
        self.assertIn("approval cannot be set", body)

    def test_executor_has_no_approve_send_or_update_operation(self):
        created = json.loads(self.prepare({"channel": "test", "text": "x"}))
        version_id = created["version_id"]
        before = self.stored(version_id)
        for method, path in (("POST", f"/versions/{version_id}/approve"), ("POST", "/send"),
                             ("PUT", f"/versions/{version_id}"), ("PATCH", f"/versions/{version_id}"),
                             ("DELETE", f"/versions/{version_id}")):
            status, _ = self.http(method, path, {"status": "Approved"})
            self.assertIn(status, (404, 501), (method, path))
        self.assertEqual(self.stored(version_id), before)

    def test_executor_rejects_wrong_types(self):
        for body in ({"channel": ["test"], "text": "x"}, {"channel": "test", "text": "x", "draft_id": ["post-1"]}):
            status, _ = self.http("POST", "/versions", body)
            self.assertEqual(status, 400, body)

    def test_falsy_draft_id_is_rejected_without_insert(self):
        before = self.count()
        for draft_id in (False, 0, [], {}, ""):
            status, body = self.http("POST", "/versions", {"channel": "test", "text": "x", "draft_id": draft_id})
            self.assertEqual(status, 400, draft_id)
            self.assertIn("draft_id must be a non-empty string", body)
        self.assertEqual(self.count(), before)

    def test_bad_content_length_is_rejected_before_reading(self):
        before = self.count()
        head = b"POST /versions HTTP/1.1\r\nHost: t\r\n"
        for lengths in ([b"abc"], [b"-1"], [b"+5"], [b"1.5"], [b"0x10"], [b""], [b"5, 5"], [], [b"2", b"2"]):
            request = head + b"".join(b"Content-Length: " + v + b"\r\n" for v in lengths) + b"\r\n{}"
            self.assertEqual(self.raw(request), "HTTP/1.0 400 Bad Request", lengths)
        self.assertEqual(self.count(), before)
        self.assertTrue(json.loads(self.prepare({"channel": "test", "text": "next"}))["ok"])

    def test_short_body_rejected_even_when_json_is_complete(self):
        before = self.count()
        body = b'{"channel":"test","text":"short"}'
        request = b"POST /versions HTTP/1.0\r\nContent-Length: 100\r\n\r\n" + body
        self.assertEqual(self.raw(request, finish=True), "HTTP/1.0 400 Bad Request")
        self.assertEqual(self.count(), before)

    def test_huge_decimal_length_is_bounded_before_conversion(self):
        before = self.count()
        head = b"POST /versions HTTP/1.0\r\nContent-Length: "
        self.assertIn(" 413 ", self.raw(head + b"9" * 5000 + b"\r\n\r\n"))
        self.assertEqual(self.count(), before)
        body = b'{"channel":"test","text":"next"}'
        padded = b"0" * 5000 + str(len(body)).encode()
        self.assertIn(" 201 ", self.raw(head + padded + b"\r\n\r\n" + body))

    def test_stalled_request_is_dropped_and_the_next_one_served(self):
        self.assertIsNotNone(self.default_timeout)  # the stdlib default (None) waits forever
        self.assertLessEqual(self.default_timeout, 30)
        port = self.server.server_port
        key = f"Authorization: Bearer {AGENT_KEY}\r\n".encode()  # past the key check: the body stalls
        for partial in (b"POST /versions HTTP/1.1\r\nHost: t\r\n" + key + b"Content-Length: 100\r\n\r\n{\"channel\"",
                        b"POST /versions HTTP/1.1\r\nHost: t\r\n"):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as stalled:
                stalled.sendall(partial)
                start = time.monotonic()
                self.assertTrue(json.loads(self.prepare({"channel": "test", "text": "next"}))["ok"])
                self.assertLess(time.monotonic() - start, 5)
                self.assertEqual(stalled.recv(1), b"")  # the server dropped the stalled request

    def test_missing_or_unknown_channel_lists_configured_channels(self):
        for args in ({"channel": "news", "text": "x"}, {"text": "x"}):
            result = json.loads(self.prepare(args))
            self.assertFalse(result["ok"])
            self.assertIn("test (IntentRivet Test Channel (local stand))", result["error"])


if __name__ == "__main__":
    unittest.main()
