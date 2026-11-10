"""Stage 2 checks: one operator-allowed publication, enforced by the executor.

Real executor and Telegram stand-in HTTP servers, the real plugin handlers.
Run: python3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_stage1 import AUTH, FakeContext, connect_agent, load

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "stage2-test-token"
STAGE1_SCHEMA = """CREATE TABLE versions (version_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL,
    version INTEGER NOT NULL, channel TEXT NOT NULL, channel_title TEXT NOT NULL, action TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (draft_id, version))"""


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    handler.log_message = lambda *a: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Stand(unittest.TestCase):
    """Real executor and Telegram stand-in HTTP servers, the real plugin handlers; no tests here."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["INTENTRIVET_DB"] = f"{cls.tmp.name}/db.sqlite3"
        cls.ex = load(f"executor_{cls.__name__}", ROOT / "executor" / "executor.py")
        cls.logged = []
        cls.ex.log = cls.logged.append  # the executor's diagnostics lines
        with closing(cls.ex.connect()) as db:  # the schema and the channels, as serve() does
            cls.ex.import_legacy_channels(db, ROOT / "stand" / "channels.json")
        cls.mock = load(f"telegram_mock_{cls.__name__}", ROOT / "stand" / "telegram_mock.py")
        cls.mock.STATE = Path(cls.tmp.name)
        cls.mock.Handler.token = TOKEN
        cls.mock_server = serve(cls.mock.Handler)
        cls.ex.TELEGRAM_API = f"http://127.0.0.1:{cls.mock_server.server_port}"
        cls.ex.BOT_TOKEN = TOKEN
        cls.server = serve(cls.ex.Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        connect_agent(cls.ex, cls.url)
        ctx = FakeContext()
        plugin = load(f"intentrivet_{cls.__name__}", ROOT / "plugin" / "__init__.py")
        plugin.register(ctx)
        cls.prepare = staticmethod(lambda args: json.loads(ctx.tools["intentrivet_prepare_post"](args)))
        cls.publish = staticmethod(lambda args: json.loads(ctx.tools["intentrivet_publish_post"](args)))
        cls.show = staticmethod(ctx.commands["intentrivet"])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.mock_server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.mock.main("reset")

    def stats(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.mock.main("stats")
        return json.loads(out.getvalue())

    def http(self, path, body):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                         headers=AUTH)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def record(self, version_id):
        with closing(self.ex.connect(read_only=True)) as db:
            return self.ex.fetch(db, version_id)

    def allow(self, version_id):
        with closing(self.ex.connect()) as db:
            self.ex.allow(db, self.ex.fetch(db, version_id))

    def new_post(self, text="Line one\nLine two", channel="test"):
        result = self.prepare({"channel": channel, "text": text})
        self.assertTrue(result["ok"], result)
        return result


class Stage2Test(Stand):
    def test_publish_needs_an_operator_permission(self):
        post = self.new_post()
        result = self.publish({"version_id": post["version_id"], "channel": "test"})
        self.assertFalse(result["ok"])
        self.assertIn("Executor (403): A publication of", result["error"])
        self.assertIn("is not allowed", result["error"])
        self.assertEqual(self.stats()["calls"], 0)

    def test_permission_is_bound_to_the_version_and_channel(self):
        v1 = self.new_post("Approved line\nOld second line")
        v2 = self.prepare({"channel": "test", "text": "Approved line\nNew second line", "draft_id": v1["draft_id"]})
        self.allow(v2["version_id"])
        attempts = [
            ("/publications", {"version_id": v2["version_id"], "channel": "main"}, 403),
            ("/publications", {"version_id": v1["version_id"], "channel": "test"}, 403),
            ("/publications", {"version_id": v2["version_id"], "channel": "test", "chat_id": -1000000000002}, 400),
            ("/publications", {"version_id": v2["version_id"], "channel": "test", "text": "other"}, 400),
        ]
        for path, body, expected in attempts:
            self.assertEqual(self.http(path, body)[0], expected, body)
        self.assertFalse(self.publish({"version_id": v2["version_id"], "channel": "test", "text": "x"})["ok"])
        self.assertEqual(self.stats()["calls"], 0)
        self.assertEqual(self.record(v2["version_id"])["status"], self.ex.ALLOWED)

        sent = self.publish({"version_id": v2["version_id"], "channel": "test"})
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(sent["outcome"], "Published")
        stats = self.stats()
        self.assertEqual((stats["calls"], stats["accepted"]), (1, 1))
        self.assertEqual(stats["posts"][0]["text"], "Approved line\nNew second line")
        self.assertEqual(stats["posts"][0]["chat_id"], -1000000000001)
        shown = self.show(v2["version_id"])  # what the operator sees with /intentrivet in Hermes
        self.assertIn("Status:   Published\nAllowed:  one publication, ", shown)
        self.assertIn("; message_id 1", shown)

        again = self.publish({"version_id": v2["version_id"], "channel": "test"})
        self.assertFalse(again["ok"])
        self.assertIn("Executor (409): The one publication allowed", again["error"])
        self.assertEqual(self.stats()["calls"], 1)

    def test_allow_refuses_a_version_superseded_after_it_was_shown(self):
        v1 = self.new_post()
        with closing(self.ex.connect()) as db:
            shown = self.ex.fetch(db, v1["version_id"])
            v2 = self.prepare({"channel": "test", "text": "changed", "draft_id": v1["draft_id"]})
            with self.assertRaises(self.ex.Superseded) as changed:
                self.ex.allow(db, shown)
        self.assertEqual(changed.exception.latest, v2["version_id"])
        self.assertIsNone(self.record(v1["version_id"])["publication"])
        self.assertIsNone(self.record(v2["version_id"])["publication"])

    def test_operator_cli_shows_the_change_and_does_not_switch_silently(self):
        v1 = self.new_post("Shown first\nsecond")
        prompts, created = [], {}

        def operator(prompt):
            prompts.append(prompt)
            if prompt.startswith("Select"):
                listing = out.getvalue().rsplit("Permission requests", 1)[1]
                line = next((l for l in listing.splitlines() if v1["draft_id"] + "-" in l), None)
                return line.split("]")[0].strip(" [") if line and not created.get("done") else ""
            if prompt.startswith("Allow one publication") and "v2" not in created:
                created["v2"] = self.prepare({"channel": "test", "text": "Shown second\nsecond",
                                              "draft_id": v1["draft_id"]})["version_id"]  # Hermes, meanwhile
                return "allow"
            if prompt.startswith("Allow one publication"):
                created["done"] = True
                return "allow"
            return "y"  # "Open <newer version>? [y/N]"

        with mock.patch("builtins.input", operator), contextlib.redirect_stdout(io.StringIO()) as out:
            self.ex.requests()
        text, v2 = out.getvalue(), created["v2"]
        self.assertIn(f"Not allowed: {v1['version_id']} is no longer the latest version", text)
        self.assertIn(f"Open {v2}? [y/N]: ", prompts)
        self.assertIn("----- BEGIN TEXT -----\nShown second\nsecond\n----- END TEXT -----", text)
        self.assertIn(f"Allowed one publication of {v2}", text)
        self.assertIsNone(self.record(v1["version_id"])["publication"])
        self.assertEqual(self.record(v2)["status"], self.ex.ALLOWED)

    def test_concurrent_publish_attempts_send_once(self):
        post = self.new_post()
        self.allow(post["version_id"])
        barrier, codes = threading.Barrier(8), []

        def attempt():
            barrier.wait()
            codes.append(self.http("/publications", {"version_id": post["version_id"], "channel": "test"})[0])

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(codes), [200] + [409] * 7)
        self.assertEqual((self.stats()["calls"], self.stats()["accepted"]), (1, 1))

    def test_lost_response_is_outcome_unknown_and_never_resent(self):
        post = self.new_post()
        self.allow(post["version_id"])
        self.mock.main("mode", "drop")
        result = self.publish({"version_id": post["version_id"], "channel": "test"})
        self.assertIn("Executor (502): Outcome unknown", result["error"])
        self.assertEqual(self.record(post["version_id"])["status"], "Outcome unknown")
        self.mock.main("mode", "ok")
        self.assertIn("(409)", self.publish({"version_id": post["version_id"], "channel": "test"})["error"])
        self.assertEqual((self.stats()["calls"], self.stats()["accepted"]), (1, 1))

    def test_ok_reply_without_a_message_is_outcome_unknown_and_never_resent(self):
        for mode in ("no-result", "null-result"):  # HTTP 200 {"ok": true} / {"ok": true, "result": null}
            with self.subTest(mode=mode):
                self.mock.main("reset")
                self.mock.main("mode", mode)
                post = self.new_post()
                self.allow(post["version_id"])
                body = {"version_id": post["version_id"], "channel": "test"}
                status, reply = self.http("/publications", body)
                self.assertEqual(status, 502)
                self.assertIn("Outcome unknown: Telegram replied ok but without a valid message_id", reply["error"])
                self.assertIn("it will not be sent again", reply["error"])
                publication = self.record(post["version_id"])["publication"]
                self.assertEqual(publication["outcome"], "Outcome unknown")
                self.assertIsNotNone(publication["used_at"])
                self.mock.main("mode", "ok")
                status, reply = self.http("/publications", body)
                self.assertEqual(status, 409)
                self.assertIn("already used: Outcome unknown", reply["error"])
                self.assertEqual((self.stats()["calls"], self.stats()["accepted"]), (1, 1))

    def test_internal_error_during_the_send_is_outcome_unknown(self):
        post = self.new_post()
        self.allow(post["version_id"])
        body = {"version_id": post["version_id"], "channel": "test"}
        with mock.patch.object(self.ex, "send_telegram", side_effect=RecursionError):
            status, reply = self.http("/publications", body)
        self.assertEqual(status, 502)
        self.assertIn("Outcome unknown: internal error during the send (RecursionError)", reply["error"])
        self.assertEqual(self.record(post["version_id"])["status"], "Outcome unknown")
        self.assertEqual(self.http("/publications", body)[0], 409)

    def test_control_characters_are_shown_escaped_and_kept_exact(self):
        # ESC clear screen and cursor home, CR, backspace, C1 CSI, DEL, bidi override, U+2028.
        text = "Real first line\x1b[2J\x1b[H\rAllowed: fake\b\b\x9b2K\x7f\u202e\u2028end\nSecond line"
        shown = ("Real first line\\u001b[2J\\u001b[H\\rAllowed: fake\\b\\b\\u009b2K\\u007f\\u202e\\u2028end"
                 "\nSecond line")
        raw = set("\x1b\r\b\x9b\x7f\u202e\u2028")
        block = f"Text:     2 line(s), {len(text)} characters\n----- BEGIN TEXT -----\n{shown}\n----- END TEXT -----"
        # A record already in the store, written before this fix: displayed safely all the same.
        action = json.dumps({"method": "sendMessage", "chat_id": -1000000000001, "text": text}, ensure_ascii=False)
        row = ("legacy-1-v1", "legacy-1", 1, "test", "IntentRivet Test Channel (local stand)", action,
               "Awaiting approval", "2026-11-06T18:33:58+00:00")
        with closing(self.ex.connect()) as db:
            db.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)

        def operator(prompt):  # Permission requests -> legacy-1-v1 -> allow -> quit
            if prompt.startswith("Allow one publication"):
                return "allow"
            if "Allowed one publication of legacy-1-v1" in out.getvalue():
                return ""
            listing = out.getvalue().rsplit("Permission requests", 1)[1]
            return next(l for l in listing.splitlines() if " legacy-1-v1 " in l).split("]")[0].strip(" [")

        with mock.patch("builtins.input", operator), contextlib.redirect_stdout(io.StringIO()) as out:
            self.ex.requests()
        with contextlib.redirect_stdout(io.StringIO()) as dumped:
            self.ex.show("legacy-1-v1")
        prepared = self.new_post(text)  # the same text through the agent's tool
        views = {"Permission requests": out.getvalue(), "executor show": dumped.getvalue(),
                 "/intentrivet": self.show("legacy-1-v1"), "tool preview": prepared["preview"]}
        for name, view in views.items():
            self.assertFalse(raw & set(view), name)
        self.assertIn("legacy-1-v1 -> IntentRivet Test Channel (local stand) [test]: "
                      + shown.split("\n")[0] + "\n", views["Permission requests"])
        for name in ("Permission requests", "/intentrivet", "tool preview"):
            self.assertIn(block, views[name], name)
        self.assertEqual(json.loads(views["executor show"]), self.record("legacy-1-v1"))

        self.assertEqual(self.publish({"version_id": "legacy-1-v1", "channel": "test"})["outcome"], "Published")
        self.assertEqual(self.stats()["posts"][0]["text"], text)  # the send is exact
        self.assertEqual(self.record(prepared["version_id"])["action"]["text"], text)
        with closing(self.ex.connect(read_only=True)) as db:  # the stored snapshot is unchanged
            self.assertEqual(db.execute("SELECT * FROM versions WHERE version_id = 'legacy-1-v1'").fetchone(), row)

    def test_send_interrupted_by_a_restart_becomes_outcome_unknown(self):
        post = self.new_post()
        self.allow(post["version_id"])
        with closing(self.ex.connect()) as db:
            self.ex.claim(db, post["version_id"], "test")  # used and committed; then the process dies
            self.assertEqual(self.ex.recover_interrupted(db), 1)
        self.assertEqual(self.record(post["version_id"])["status"], "Outcome unknown")
        self.assertIn("(409)", self.publish({"version_id": post["version_id"], "channel": "test"})["error"])
        self.assertEqual(self.stats()["calls"], 0)

    def test_unreachable_telegram_fails_without_a_send(self):
        post = self.new_post()
        self.allow(post["version_id"])
        with mock.patch.object(self.ex, "TELEGRAM_API", "http://127.0.0.1:9"):
            result = self.publish({"version_id": post["version_id"], "channel": "test"})
        self.assertIn("Executor (502): Failed: Telegram not reachable", result["error"])
        self.assertEqual(self.record(post["version_id"])["status"], "Failed")

    def test_stand_in_counts_but_refuses_a_wrong_token(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.mock_server.server_port}/botWRONG/sendMessage",
                                         data=b'{"chat_id": 1, "text": "x"}', method="POST")
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request)
        self.assertEqual(refused.exception.code, 401)
        self.assertEqual((self.stats()["calls"], self.stats()["accepted"]), (1, 0))

    def test_stage1_store_upgrades_in_place(self):
        path = f"{self.tmp.name}/stage1.sqlite3"
        action = json.dumps({"method": "sendMessage", "chat_id": -1000000000001, "text": "From stage 1"})
        row = ("post-1-v1", "post-1", 1, "test", "IntentRivet Test Channel (local stand)", action,
               "Awaiting approval", "2026-11-06T18:33:58+00:00")
        with closing(sqlite3.connect(path)) as old:
            old.execute(STAGE1_SCHEMA)
            old.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
            old.commit()
        with mock.patch.object(self.ex, "DB_PATH", path), closing(self.ex.connect()) as db:
            self.assertEqual(db.execute("SELECT * FROM versions").fetchall(), [row])
            record = self.ex.fetch(db, "post-1-v1")
            self.assertEqual((record["status"], record["publication"]), ("Awaiting approval", None))
            self.assertEqual(self.ex.pending_requests(db), ["post-1-v1"])
            self.ex.allow(db, record)
            self.assertEqual(self.ex.publish(db, {"version_id": "post-1-v1", "channel": "test"})[0], 200)
            self.assertEqual(db.execute("SELECT * FROM versions").fetchall(), [row])
        self.assertEqual(self.stats()["posts"][0]["text"], "From stage 1")


if __name__ == "__main__":
    unittest.main()
