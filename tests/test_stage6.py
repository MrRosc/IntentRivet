"""Stage 6 checks: Hermes reaches the executor at a configured address, with the agent key.

The in-process stand of stage 2 (real executor and plugin, Telegram stand-in). Run:
python3 -m unittest discover -s tests
"""

import contextlib
import hashlib
import io
import json
import os
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

from test_stage1 import AGENT_KEY, SECRETS, FakeContext, load
from test_stage2 import ROOT, Stand, serve


class AgentKeyTest(Stand):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ctx = FakeContext()
        cls.plugin = load("intentrivet_stage6", ROOT / "plugin" / "__init__.py")
        cls.plugin.register(ctx)
        cls.tools, cls.cli = ctx.tools, ctx.cli

    def request(self, method, path, body=None, headers=()):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method)
        for name, value in headers:
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read()), response.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read()), e.headers

    def versions(self):
        with closing(self.ex.connect(read_only=True)) as db:
            return db.execute("SELECT COUNT(*) FROM versions").fetchone()[0]

    def stored_key(self, value):
        with closing(self.ex.connect()) as db:
            self.ex.set_setting(db, "agent_key", value)

    def tearDown(self):
        self.stored_key(hashlib.sha256(AGENT_KEY.encode()).hexdigest())
        SECRETS.update(INTENTRIVET_URL=self.url, INTENTRIVET_AGENT_KEY=AGENT_KEY)

    def check(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.cli["intentrivet"][1](None)
        return code, out.getvalue()

    def test_every_request_needs_the_agent_key_and_it_gives_no_owner_operation(self):
        post = {"channel": "test", "text": "Needs the key"}
        before = self.versions()
        cases = {"missing": [], "wrong": [("Authorization", "Bearer not-the-key")],
                 "not bearer": [("Authorization", f"Basic {AGENT_KEY}")]}
        for case, headers in cases.items():
            for method, path, body in (("GET", "/channels", None), ("GET", "/versions/post-1-v1", None),
                                       ("POST", "/versions", post), ("POST", "/publications", {})):
                status, reply, response = self.request(method, path, body, headers)
                self.assertEqual(status, 401, (case, path))
                self.assertEqual(response["WWW-Authenticate"], "Bearer")
                self.assertIn("Agent key", reply["error"])
        twice = f"Authorization: Bearer {AGENT_KEY}\r\n".encode() * 2  # urllib would send only one
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5) as s:
            s.sendall(b"GET /channels HTTP/1.0\r\n" + twice + b"\r\n")
            self.assertIn(b" 401 ", s.recv(4096).split(b"\r\n", 1)[0])
        self.assertEqual(self.versions(), before)  # nothing was read or stored without the key
        key = [("Authorization", f"Bearer {AGENT_KEY}")]
        self.assertEqual(self.request("GET", "/channels", headers=key)[0], 200)
        for path in ("/versions/post-1-v1/allow", "/permissions", "/channels", "/owner", "/bind", "/updates"):
            self.assertEqual(self.request("POST", path, {"version_id": "post-1-v1"}, key)[0], 404, path)

        self.stored_key(None)  # a new installation: no key issued yet, so nothing is accepted
        status, reply, _ = self.request("GET", "/channels", headers=key)
        self.assertEqual(status, 401)
        self.assertIn("executor.py agent-key", reply["error"])

    def test_agent_key_is_shown_once_stored_as_a_hash_and_replaced_only_on_confirmation(self):
        def agent_key(tty=False, answer=None):
            with mock.patch.object(self.ex.sys.stdin, "isatty", return_value=tty), \
                    mock.patch.object(self.ex, "ask", return_value=answer), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.ex.agent_key()
            return out.getvalue()

        def works(key):
            return self.request("GET", "/channels", headers=[("Authorization", f"Bearer {key}")])[0] == 200

        self.stored_key(None)
        first = agent_key().split("\n\n  ")[1].strip()  # no key yet: issued without a confirmation
        with closing(self.ex.connect(read_only=True)) as db:
            self.assertEqual(self.ex.get_setting(db, "agent_key"), hashlib.sha256(first.encode()).hexdigest())
        self.assertTrue(works(first))
        self.assertFalse(works(AGENT_KEY))

        with self.assertRaises(SystemExit) as refused:  # a script cannot replace it by accident
            agent_key(tty=False)
        self.assertIn("interactive terminal", str(refused.exception))
        self.assertIn("unchanged", agent_key(tty=True, answer="no"))
        self.assertTrue(works(first))
        second = agent_key(tty=True, answer="replace").split("\n\n  ")[1].strip()
        self.assertNotEqual(first, second)
        self.assertTrue(works(second))
        self.assertFalse(works(first))  # the previous key stops working at once

    def test_the_plugin_reads_its_address_and_key_with_get_secret_only(self):
        prepare = self.tools["intentrivet_prepare_post"]
        with mock.patch.dict(os.environ, {"INTENTRIVET_URL": "http://127.0.0.1:9", "INTENTRIVET_AGENT_KEY": "x"}):
            self.assertTrue(json.loads(prepare({"channel": "test", "text": "From the profile"}))["ok"])
        with mock.patch.dict(SECRETS, clear=True), mock.patch.dict(os.environ, {"INTENTRIVET_URL": self.url,
                                                                                 "INTENTRIVET_AGENT_KEY": AGENT_KEY}):
            self.assertIn("INTENTRIVET_URL is not set", json.loads(prepare({"channel": "test", "text": "x"}))["error"])
        for env, expected in (({"INTENTRIVET_URL": ""}, "INTENTRIVET_URL is not set"),
                              ({"INTENTRIVET_URL": "127.0.0.1:8787"}, "must start with http:// or https://"),
                              ({"INTENTRIVET_AGENT_KEY": " "}, "INTENTRIVET_AGENT_KEY is not set"),
                              ({"INTENTRIVET_AGENT_KEY": "old-key"}, "Executor (401): Agent key not accepted"),
                              ({"INTENTRIVET_URL": "http://127.0.0.1:9"}, "Executor unreachable at http://127.0.0.1:9")):
            with mock.patch.dict(SECRETS, env):
                result = json.loads(prepare({"channel": "test", "text": "x"}))
                self.assertFalse(result["ok"], env)
                # A prepare changes something: without the executor's answer it is never called unreachable.
                self.assertIn("No confirmed answer from the executor at http://127.0.0.1:9" if "unreachable" in expected
                              else expected, result["error"])
                code, said = self.check()
                self.assertEqual(code, 1)
                self.assertIn("IntentRivet: not connected. ", said)
                self.assertIn(expected, said)
                self.assertNotIn(AGENT_KEY, said)
        with mock.patch.dict(SECRETS, {"INTENTRIVET_URL": self.url + "/"}):  # a trailing slash is fine
            self.assertTrue(json.loads(prepare({"channel": "test", "text": "Configured"}))["ok"])

    def test_check_reports_the_connection_and_the_channels(self):
        code, said = self.check()
        self.assertEqual(code, 0)
        self.assertIn(f"connected to the executor at {self.url}; it accepted the agent key.", said)
        self.assertIn("[test] IntentRivet Test Channel (local stand), [main]", said)
        self.assertNotIn(AGENT_KEY, said)
        setup, _ = self.cli["intentrivet"]
        import argparse
        parser = argparse.ArgumentParser()
        setup(parser)
        self.assertEqual(parser.parse_args(["check"]).action, "check")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["allow"])

    def test_a_redirect_is_not_followed_so_the_key_goes_nowhere_else(self):
        seen = []

        class Elsewhere(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"channels": []}')

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{elsewhere.server_port}/channels")
                self.end_headers()

        elsewhere, redirect = serve(Elsewhere), serve(Redirect)
        try:
            with mock.patch.dict(SECRETS, {"INTENTRIVET_URL": f"http://127.0.0.1:{redirect.server_port}"}):
                result = json.loads(self.tools["intentrivet_list_channels"]({}))
        finally:
            elsewhere.shutdown()
            redirect.shutdown()
        self.assertFalse(result["ok"])
        self.assertIn("Executor (307)", result["error"])
        self.assertEqual(seen, [])



class CheckIsolationTest(Stand):
    """stand/check-isolation.sh other-host against stub ssh commands and the real executor's API: only
    a verified check passes; one that could not run is UNKNOWN, never PASS."""

    def isolation(self, ssh_script, url=None):
        with tempfile.TemporaryDirectory() as stub:
            Path(stub, "ssh").write_text("#!/bin/sh\n" + ssh_script)
            Path(stub, "ssh").chmod(0o755)
            args = ["sh", str(ROOT / "stand" / "check-isolation.sh"), "other-host", "executor-host", url or self.url]
            done = subprocess.run(args, capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"})
        return done.returncode, done.stdout

    LOGGED_IN = 'echo \'Authenticated to executor-host ([100.64.0.2]:22) using "publickey".\' >&2\n'

    def test_a_check_that_could_not_run_is_never_a_pass(self):
        code, said = self.isolation("exit 127\n")  # the review's case: an ssh that did nothing
        self.assertEqual(code, 1)
        self.assertIn("UNKNOWN  ssh exited 127 (login confirmed: no)", said)
        self.assertIn("RESULT: incomplete", said)
        self.assertNotIn("runs no command", said)
        code, said = self.isolation("exit 1\n")  # exit 1, but no login in ssh's log
        self.assertEqual(code, 1)
        self.assertIn("UNKNOWN  ssh exited 1 (login confirmed: no)", said)

    def test_a_confirmed_forward_only_login_and_the_api_pass_and_say_what_is_not_checked(self):
        code, said = self.isolation(self.LOGGED_IN + "exit 1\n")
        self.assertEqual(code, 0, said)
        self.assertIn(f"PASS  the executor's API answers at {self.url} (401 without the agent key)", said)
        self.assertIn("PASS  the SSH login to executor-host is confirmed and runs no command", said)
        self.assertIn("Not checked: other SSH keys", said)
        self.assertNotIn("isolated", said.split("RESULT:")[1])

    def test_a_shell_no_login_or_no_executor_fails(self):
        code, said = self.isolation(self.LOGGED_IN + "echo shell-allowed\n")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  the SSH login to executor-host runs commands", said)
        self.assertIn("RESULT: NOT isolated", said)
        code, said = self.isolation("echo 'Permission denied (publickey).' >&2\nexit 255\n")
        self.assertIn("FAIL  no SSH login to executor-host (ssh exit 255)", said)
        self.assertIn("RESULT: not connected", said)
        self.assertEqual(code, 1)
        code, said = self.isolation(self.LOGGED_IN + "exit 1\n", url="http://127.0.0.1:9")
        self.assertIn("FAIL  the executor's API is not reachable at http://127.0.0.1:9", said)
        self.assertIn("RESULT: not connected", said)
        self.assertNotIn("bypass", said)
        self.assertEqual(code, 1)
        stand_in = f"http://127.0.0.1:{self.mock_server.server_port}"  # answers, but it is not the executor
        code, said = self.isolation(self.LOGGED_IN + "exit 1\n", url=stand_in)
        self.assertIn(f"FAIL  {stand_in} answers, but not as the executor's API", said)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
