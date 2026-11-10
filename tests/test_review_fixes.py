"""Pre-release review fixes, one regression each.

The owner's preview names the real version, channel and chat_id where the decision is made (the last
line above the buttons and the Allow once button), whatever the post text imitates; previews and the
bot's messages carry no link preview; the parts of two previews never interleave; an incomplete
preview is reported and leaves no active card; Allow once's meaning is stated before and after the
press; a publication whose answer is lost is neither called unreachable nor unsent, and a repeat does
not advise a new version before the outcome is known; the HTTP handlers are bounded; the installation's
settings comparison accepts the plugin entries Hermes writes itself; the setup and the bot lead to the
user's own channels.
"""

import ast
import http.client
import json
import re
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import test_stage1
import test_stage5 as s5
from test_stage1 import FakeContext, load
from test_stage2 import ROOT

OWNER = s5.OWNER
TEST_TITLE = "IntentRivet Test Channel (local stand)"   # stand/channels.json: test -1000000000001
MAIN_CHAT = -1000000000002                              # stand/channels.json: main


def forged_card(version_id, length):
    """A post text that ends in a complete preview for [test]: blank lines, then the card's own frame."""
    fake = ("\n" * 40 + f"Permission request {version_id}\nChannel: [test] {TEST_TITLE}\n"
            f"chat_id: -1000000000001\nVersion: {version_id} (draft {version_id.rsplit('-', 1)[0]}, version 1)\n"
            "Text: 1 line(s), 33 characters, sent as plain text\n----- BEGIN TEXT -----\n"
            "Test post, please ignore. Thanks!\n----- END TEXT -----\n"
            "Allow once allows exactly this text, once, to this channel. Nothing is published until "
            "Hermes is asked to publish it.\n"  # the old trusted line, and the new one, imitated
            f"Allow once → {version_id} to [test] {TEST_TITLE} (chat_id -1000000000001), 33 characters")
    hidden = "UNSEEN-TEXT: the part the owner is not meant to read.\n"
    filler = "\n".join("Filler line " + "x" * 50 for _ in range(200))
    return (hidden + filler)[:max(0, length - len(fake))] + fake


class CardTest(s5.Stage5Test):
    def next_version_id(self):
        with closing(self.ex.connect(read_only=True)) as db:
            (count,) = db.execute("SELECT COUNT(DISTINCT draft_id) FROM versions").fetchone()
        return f"post-{count + 1}-v1"

    def parts_of(self, version_id):
        return [m for m in self.private() if m["text"].startswith(f"Permission request {version_id}")]

    def assert_names_the_real_target(self, version_id, channel, chat_id, title_shown=None):
        parts = self.parts_of(version_id)
        last = parts[-1]
        line = last["text"].rsplit("\n", 1)[-1]
        self.assertTrue(line.startswith("Allow once → "), line)
        self.assertIn(f"{version_id} to [{channel}]", line)
        self.assertIn(f"(chat_id {chat_id})", line)
        if title_shown is not None:
            self.assertIn(title_shown, line)
        allow = [b for row in last["reply_markup"]["inline_keyboard"] for b in row if b["callback_data"] == "allow"]
        self.assertEqual(allow[0]["text"], f"Allow once → [{channel}] {version_id}")
        for number, part in enumerate(parts[1:], 2):
            self.assertTrue(part["text"].startswith(
                f"Permission request {version_id} (part {number} of {len(parts)}) to [{channel}], text continued:\n"))

    def test_a_forged_card_in_the_text_does_not_change_what_the_buttons_say(self):
        for length in (3955, 900):  # two parts, and one part with the forgery right above the buttons
            version_id = self.next_version_id()
            result = self.prepare({"channel": "main", "text": forged_card(version_id, length)})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["version_id"], version_id)
            self.assertEqual(len(self.parts_of(version_id)), 2 if length > 2000 else 1)
            self.assert_names_the_real_target(version_id, "main", MAIN_CHAT)
            self.assertIn("[test]", self.parts_of(version_id)[-1]["text"])  # the forgery is still shown, as text

    def test_a_long_or_escaped_title_is_shown_escaped_and_clipped_next_to_the_buttons(self):
        title = "\u202eLong title with an escape " * 8 + "\x1b[31m"
        self.store(("INSERT OR IGNORE INTO channels (key, title, chat_id, added_at) VALUES ('odd', ?, -1000000000555, 'x')",
                    (title,)))
        result = self.prepare({"channel": "odd", "text": "A short post."})
        self.assertTrue(result["ok"], result)
        self.assert_names_the_real_target(result["version_id"], "odd", -1000000000555, "\\u202eLong title")
        line = self.parts_of(result["version_id"])[-1]["text"].rsplit("\n", 1)[-1]
        self.assertNotIn("\u202e", line)
        self.assertNotIn("\x1b", line)
        self.assertIn("…", line)  # clipped
        self.assertLess(len(line), 260)

    def test_previews_and_the_bots_messages_carry_no_link_preview(self):
        sent, real = [], self.ex.telegram_call

        def spy(method, payload, *args, **kwargs):
            if method == "sendMessage":
                sent.append(payload)
            return real(method, payload, *args, **kwargs)
        with mock.patch.object(self.ex, "telegram_call", spy):
            version_id = self.new_post("Details: https://example.com/page")["version_id"]
            self.press(OWNER, self.card(), "allow")
            self.message(OWNER, "Pending requests")
        self.assertGreaterEqual(len(sent), 3)
        for payload in sent:
            self.assertEqual(payload.get("link_preview_options"), {"is_disabled": True}, payload["text"][:70])
        self.assertIn(version_id, sent[0]["text"])

    def test_two_previews_sent_at_once_never_interleave(self):
        order, real = [], self.ex.telegram_call

        def slow(method, payload, *args, **kwargs):
            if method == "sendMessage" and payload.get("chat_id") == OWNER:
                time.sleep(0.05)
                order.append(payload["text"].split("\n", 1)[0])
            return real(method, payload, *args, **kwargs)
        long = lambda tag: "\n".join(f"{tag} line {i} " + "x" * 60 for i in range(50))  # two parts each
        with closing(self.ex.connect()) as db:
            first = self.ex.create_version(db, {"channel": "main", "text": long("MAIN")})["version_id"]
            second = self.ex.create_version(db, {"channel": "test", "text": long("TEST")})["version_id"]
        with mock.patch.object(self.ex, "telegram_call", slow):
            threads = [threading.Thread(target=self.bot.deliver_card, args=(v,)) for v in (first, second)]
            threads[0].start()
            time.sleep(0.02)
            threads[1].start()
            for thread in threads:
                thread.join()
        ids = [re.search(r"post-\d+-v\d+", line)[0] for line in order]
        self.assertEqual(len(ids), 4, order)
        self.assertEqual(sum(a != b for a, b in zip(ids, ids[1:])), 1, ids)  # one block per preview

    def test_an_incomplete_preview_is_reported_and_leaves_no_active_card(self):
        real = self.ex.telegram_call

        def second_part_fails(method, payload, *args, **kwargs):
            if method == "sendMessage" and "(part 2 of" in payload.get("text", ""):
                raise self.ex.Rejected(502, "sendMessage: Telegram not reachable (TimeoutError: timed out)")
            return real(method, payload, *args, **kwargs)
        with closing(self.ex.connect()) as db:
            version_id = self.ex.create_version(db, {"channel": "test", "text": "\n".join(
                f"line {i} " + "x" * 60 for i in range(55))})["version_id"]
        with mock.patch.object(self.ex, "telegram_call", second_part_fails):
            delivered, detail = self.bot.deliver_card(version_id)
        self.assertFalse(delivered)
        self.assertIn("part 2 of", detail)
        with closing(self.ex.connect(read_only=True)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM cards WHERE version_id = ?", (version_id,)).fetchone())
        notice = self.said()
        self.assertIn(f"The preview of {version_id} to [test] did not arrive complete", notice)
        self.assertIn("Pending requests", notice)

    def test_allow_once_meaning_is_stated_before_and_after_the_press(self):
        version_id = self.new_post("A short post.")["version_id"]
        tail = self.parts_of(version_id)[-1]["text"].split("----- END TEXT -----\n", 1)[1]
        for phrase in ("one attempt to publish", "at once, without asking you again", "The button itself sends nothing",
                       "does not expire and cannot be withdrawn"):
            self.assertIn(phrase, tail)
        self.press(OWNER, self.card(), "allow")
        said = self.said()
        for phrase in ("Nothing is sent yet", "at any time, without asking you again", "cannot be withdrawn"):
            self.assertIn(phrase, said)
        help_text = type(self.bot).say.__globals__["HELP"]
        self.assertIn("without asking you again", help_text)

    def test_a_repeat_while_sending_or_after_an_unknown_outcome_does_not_advise_a_new_version(self):
        version_id = self.new_post("Pending send.")["version_id"]
        self.press(OWNER, self.card(), "allow")
        self.store(("UPDATE permissions SET used_at = '2026-01-01T00:00:00+00:00', outcome = ? WHERE version_id = ?",
                    (self.ex.PUBLISHING, version_id)))
        busy = self.publish({"version_id": version_id, "channel": "test"})["error"]
        self.assertIn("(409)", busy)
        self.assertIn("in progress", busy)
        self.assertNotIn("Nothing was sent", busy)
        self.assertNotIn("needs a new version", busy)
        self.assertIn("do not prepare a new version", busy)
        self.store(("UPDATE permissions SET outcome = ?, detail = 'no response' WHERE version_id = ?",
                    (self.ex.OUTCOME_UNKNOWN, version_id)))
        unknown = self.publish({"version_id": version_id, "channel": "test"})["error"]
        self.assertIn("already used: Outcome unknown (no response)", unknown)
        self.assertIn("check the channel", unknown)
        self.assertIn("This request sent nothing", unknown)
        self.assertNotIn("Another publication needs a new version", unknown)
        self.assertEqual(self.stats()["calls"], 0)

    def test_add_channel_guides_through_the_rights_the_real_check_post_and_the_next_channel(self):
        self.message(OWNER, "Add channel")
        guide = self.said()
        for phrase in ("administrator", "right to post messages", "a real post", "subscribers can see",
                       "Hermes' own bot", "must not be an administrator", "30 minutes"):
            self.assertIn(phrase, guide)
        phrase = re.search(r"Connect check [0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}", guide)[0]
        self.stand_in("post", -1000000000777, "My own channel", phrase)
        self.bot.poll_once(0)
        self.message(OWNER, "my-channel")
        done = self.said()
        self.assertTrue(done.startswith("Connected [my-channel]: My own channel (chat_id -1000000000777)"), done)
        self.assertIn("delete the check text", done)
        self.assertIn("Add channel again", done)
        help_text = type(self.bot).say.__globals__["HELP"]
        for phrase in ("publisher bot", "its own bot", "Add channel"):
            self.assertIn(phrase, help_text)

    def test_the_users_own_channel_end_to_end(self):
        """Bind, connect an own channel under an own name, prepare, allow, publish: no test or main."""
        self.store("DELETE FROM settings WHERE name = 'owner_id'")
        code = re.search(r"\?start=(\S+)", self.bind())[1]
        self.message(OWNER, f"/start {code}")
        self.assertIn("You are now the owner of this bot.", self.said())
        self.message(OWNER, "Add channel")
        phrase = re.search(r"Connect check \S+", self.said())[0]
        self.stand_in("post", -1000000000888, "Field notes", phrase)
        self.bot.poll_once(0)
        self.message(OWNER, "field-notes")
        result = self.prepare({"channel": "field-notes", "text": "Our first post."})
        self.assertTrue(result["ok"], result)
        self.assert_names_the_real_target(result["version_id"], "field-notes", -1000000000888)
        self.press(OWNER, self.card(), "allow")
        published = self.publish({"version_id": result["version_id"], "channel": "field-notes"})
        self.assertTrue(published["ok"], published)
        self.assertEqual(published["outcome"], "Published")
        self.assertEqual(self.stats()["calls"], 1)


class BoundedHandlersTest(s5.Stage5Test):
    def test_http_handlers_are_bounded_and_the_rest_are_refused_at_once(self):
        with mock.patch.object(self.ex, "MAX_HANDLERS", 2):
            server = self.ex.BoundedServer(("127.0.0.1", 0), self.ex.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port
        idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]  # slow clients: say nothing
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", port))
        extra.settimeout(3)
        self.assertTrue(extra.recv(512).startswith(b"HTTP/1.0 503 "))  # at once, nothing read
        extra.close()
        for sock in idle:
            sock.close()
        time.sleep(0.5)
        with self.assertRaises(urllib.error.HTTPError) as refused:  # served again: 401, no key
            urllib.request.urlopen(f"http://127.0.0.1:{port}/channels", timeout=5)
        self.assertEqual(refused.exception.code, 401)
        self.assertIn("BoundedServer((", (ROOT / "executor" / "executor.py").read_text().split("def serve()")[1])


class LostAnswerTest(unittest.TestCase):
    def test_a_lost_answer_to_a_publication_is_neither_unreachable_nor_unsent(self):
        release = threading.Event()

        class Silent(BaseHTTPRequestHandler):  # reads the request, never answers within the timeout
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                release.wait(10)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Silent)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        plugin = load("intentrivet_review_lost_answer", ROOT / "plugin" / "__init__.py")
        ctx = FakeContext()
        plugin.register(ctx)
        with mock.patch.dict(test_stage1.SECRETS, {"INTENTRIVET_URL": f"http://127.0.0.1:{server.server_port}",
                                                   "INTENTRIVET_AGENT_KEY": "k"}), \
                mock.patch.object(plugin, "PUBLISH_TIMEOUT", 1):
            result = json.loads(ctx.tools["intentrivet_publish_post"]({"version_id": "post-7-v2", "channel": "news"}))
        self.assertFalse(result["ok"])
        error = result["error"]
        self.assertNotIn("unreachable", error)
        self.assertNotIn("Nothing was sent", error)
        for phrase in ("may be in progress or done", "Do not publish it again", "do not prepare a new version",
                       "/intentrivet post-7-v2"):
            self.assertIn(phrase, error)
        self.assertEqual(plugin.PUBLISH_TIMEOUT, 60)


class PluginTransportTest(unittest.TestCase):
    """The plugin's answer when the executor's own complete answer is missing: a change (publish,
    prepare) is never called unreachable or unsent then; a read is diagnosed; complete answers keep
    their text."""

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        plugin = load(f"intentrivet_transport_{self._testMethodName}", ROOT / "plugin" / "__init__.py")
        ctx = FakeContext()
        plugin.register(ctx)
        secrets = mock.patch.dict(test_stage1.SECRETS, {"INTENTRIVET_URL": f"http://127.0.0.1:{server.server_port}",
                                                        "INTENTRIVET_AGENT_KEY": "k"})
        secrets.start()
        self.addCleanup(secrets.stop)
        return lambda tool, args: json.loads(ctx.tools[tool](args))

    def assert_outcome_unknown(self, result, version_id):
        self.assertFalse(result["ok"])
        self.assertNotIn("unreachable", result["error"])
        self.assertNotIn("Nothing was sent", result["error"])
        for phrase in ("may be in progress or done", "Do not publish it again", f"/intentrivet {version_id}"):
            self.assertIn(phrase, result["error"])

    def test_an_error_after_the_body_went_out_is_not_called_unreachable(self):
        received, arrived = [], threading.Event()

        class Receiver(BaseHTTPRequestHandler):  # reads the whole body, then gets no chance to answer
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                arrived.set()
                time.sleep(0.5)

            def log_message(self, *args):
                pass
        call = self.serve(Receiver)
        real_send = http.client.HTTPConnection.send

        def send_then_fail(connection, data):
            real_send(connection, data)
            if b'"version_id"' in bytes(data) or b'"text"' in bytes(data):  # the body is out; only now it breaks
                raise OSError("connection lost after the body was sent")
        with mock.patch.object(http.client.HTTPConnection, "send", send_then_fail):
            published = call("intentrivet_publish_post", {"version_id": "post-3-v1", "channel": "news"})
            self.assertTrue(arrived.wait(5))
            prepared = call("intentrivet_prepare_post", {"channel": "news", "text": "A post."})
        deadline = time.time() + 5  # the client failed right after sending: the server may still be reading
        while len(received) < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(received, [{"version_id": "post-3-v1", "channel": "news"},
                                    {"channel": "news", "text": "A post."}])  # the executor got each whole body
        self.assert_outcome_unknown(published, "post-3-v1")
        self.assertFalse(prepared["ok"])
        self.assertNotIn("unreachable", prepared["error"])
        self.assertIn("may have been stored", prepared["error"])

    def test_a_cut_error_answer_is_a_result_and_complete_answers_keep_their_text(self):
        def full(code, reason, text):
            data = json.dumps({"error": text}).encode()
            return (f"HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n").encode() + data
        cut = (b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n"
               b'{"error": "Outcome unkn')  # 23 of the 100 bytes announced, then the connection closes
        replies = {"post-4-v1": cut, "read": cut,
                   "post-5-v1": full(409, "Conflict", "The one publication allowed for post-5-v1 was already used: "
                                                      "Published (message_id 3). This request sent nothing."),
                   "post-6-v1": full(503, "Service Unavailable", "The executor is busy: too many connections at "
                                                                 "once. Nothing was done; try again."),
                   "post-7-v1": full(403, "Forbidden", "A publication of post-7-v1 is not allowed.")}

        class Answers(BaseHTTPRequestHandler):
            def do_POST(self):
                self.answer(replies[json.loads(self.rfile.read(int(self.headers["Content-Length"])))["version_id"]])

            def do_GET(self):
                self.answer(replies["read"])

            def answer(self, raw):
                self.wfile.write(raw)
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *args):
                pass
        call = self.serve(Answers)
        self.assert_outcome_unknown(call("intentrivet_publish_post", {"version_id": "post-4-v1", "channel": "news"}),
                                    "post-4-v1")
        listed = call("intentrivet_list_channels", {})
        self.assertFalse(listed["ok"])
        self.assertIn("Executor (502): HTTP 502, without a complete error message", listed["error"])
        for version_id, expected in (("post-5-v1", "Executor (409): The one publication allowed for post-5-v1"),
                                     ("post-6-v1", "Executor (503): The executor is busy"),
                                     ("post-7-v1", "Executor (403): A publication of post-7-v1")):
            result = call("intentrivet_publish_post", {"version_id": version_id, "channel": "news"})
            self.assertFalse(result["ok"])
            self.assertTrue(result["error"].startswith(expected), result["error"])


class SettingsComparisonTest(unittest.TestCase):
    """stand/e2e_existing.py kept(): IntentRivet only, as Hermes writes it on enable."""

    @classmethod
    def setUpClass(cls):
        source = (ROOT / "stand" / "e2e_existing.py").read_text()
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "kept")
        namespace = {"json": json}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "e2e_existing.kept", "exec"), namespace)
        cls.kept = staticmethod(namespace["kept"])

    @staticmethod
    def snapshot(config, added=()):
        return {"config": config, "env": ["h0 TELEGRAM_BOT_TOKEN", *added], "memories": {"MEMORY.md": "m"},
                "soul": "s", "plugins": {"other": {"__init__.py": "p"}}}

    def test_the_entries_hermes_writes_on_enable_are_accepted_and_nothing_else(self):
        added = ("h1 INTENTRIVET_URL", "h2 INTENTRIVET_AGENT_KEY")
        enable = {"disabled": [], "enabled": ["intentrivet"], "entries": {"intentrivet": {"allow_tool_override": False}}}
        before = self.snapshot({"model": {"default": "m"}})
        self.assertTrue(self.kept(before, self.snapshot({"model": {"default": "m"}, "plugins": enable}, added)))
        other = self.snapshot({"model": {"default": "m"}, "plugins": {"enabled": ["other"]}})
        self.assertTrue(self.kept(other, self.snapshot({"model": {"default": "m"}, "plugins": {
            **enable, "enabled": ["intentrivet", "other"]}}, added)))
        for changed in ({"model": {"default": "other"}, "plugins": enable},
                        {"model": {"default": "m"}, "plugins": {**enable, "entries": {"intentrivet": {
                            "allow_tool_override": True}}}},
                        {"model": {"default": "m"}, "plugins": {**enable, "disabled": ["other"]}}):
            self.assertFalse(self.kept(before, self.snapshot(changed, added)), changed)
        self.assertFalse(self.kept(before, self.snapshot({"model": {"default": "m"}, "plugins": enable})))


class SetupGuidanceTest(s5.SetupScriptTest):
    """stand/setup.sh to the end, with a stub engine: what it explains first, and the steps it leaves."""

    DOCKER = """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
case $1 in
  version) echo "$STUB_COMPONENTS" ;;
  info) echo "$STUB_ENGINE_ROOT" ;;
  compose) case "$*" in
      *" check") echo "RESULT: ready" ;;
      *" bind") echo "  https://t.me/example_publisher_bot?start=stub-code" ;;
    esac ;;
  *) exit 1 ;;
esac
"""

    def test_setup_explains_the_two_bots_and_leads_to_the_users_own_channels(self):
        code, out = self.run_script("setup.sh", self.TOKEN)
        self.assertEqual(code, 0, out)
        intro, _, rest = out.partition("(input hidden)")
        for phrase in ("its own Telegram bot", "the one you chat with", "never allow them", "publisher bot",
                       "Where to put the executor", "must not administer them"):
            self.assertIn(phrase, intro)
        for phrase in ("Next steps:", "Add channel", "right to post messages", "a real post",
                       "delete it once the bot confirms", "short key of your own", "Hermes' own bot",
                       "Add channel again", "executor.py agent-key", "Install, step 4",
                       "Executor autostart"):
            self.assertIn(phrase, rest)
        self.assertNotIn(self.TOKEN, out)


def load_tests(loader, standard_tests, pattern):
    """Only the tests defined here: the classes above reuse the stage 5 harness, not its tests."""
    suite = unittest.TestSuite()
    for case in (CardTest, BoundedHandlersTest, LostAnswerTest, PluginTransportTest, SettingsComparisonTest,
                 SetupGuidanceTest):
        suite.addTests(case(name) for name in case.__dict__ if name.startswith("test_"))
    return suite


if __name__ == "__main__":
    unittest.main()
