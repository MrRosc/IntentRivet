"""Stage 5 checks: the owner binds once, connects channels and decides in Telegram.

The in-process stand of stage 2 (real executor, plugin and Telegram stand-in) plus the owner's
bot from executor/owner_bot.py, polled one step at a time. Run: python3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from test_stage1 import load
from test_stage2 import ROOT, TOKEN, Stand

OWNER, STRANGER = 111, 222


def units(text):
    return len(text.encode("utf-16-le")) // 2


class Stage5Test(Stand):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bot = load(f"owner_bot_{cls.__name__}", ROOT / "executor" / "owner_bot.py").OwnerBot(cls.ex)
        cls.ex.BOT = cls.bot
        threading.Thread(target=cls.bot.run_notices, daemon=True).start()  # as serve() does
        cls.token_file = Path(cls.tmp.name, "token")
        cls.token_file.write_text(TOKEN)

    def setUp(self):
        super().setUp()  # resets the stand-in, whose update ids start again at 1
        self.store("DELETE FROM settings WHERE name IN ('update_offset', 'add_channel', 'bind_code')",
                   "DELETE FROM cards")
        self.set_owner(OWNER)

    # -- helpers
    def store(self, *statements):
        with closing(self.ex.connect()) as db:
            for statement in statements:
                db.execute(*statement) if isinstance(statement, tuple) else db.execute(statement)

    def set_owner(self, user):
        with closing(self.ex.connect()) as db:
            self.ex.set_setting(db, "owner_id", user)

    def owner(self):
        with closing(self.ex.connect()) as db:
            return self.ex.owner_id(db)

    def stand_in(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.mock.main(*map(str, args))
        return out.getvalue().strip()

    def message(self, user, text):
        update_id = self.stand_in("message", user, text)
        self.bot.poll_once(0)
        return int(update_id)

    def press(self, user, message_id, data, chat=None):
        update_id = self.stand_in("press", user, chat or user, message_id, data)
        self.bot.poll_once(0)
        return int(update_id)

    def private(self):
        return [m for m in self.stats()["private_log"] if m["method"] == "sendMessage"]

    def said(self):
        return self.private()[-1]["text"]

    def told(self):
        """The last message to the owner once every queued outcome notice is through."""
        self.bot.notices.join()
        return self.said()

    def card(self):
        """The message that carries the newest decision buttons."""
        return [m for m in self.private() if self.decides(m)][-1]["message_id"]

    @staticmethod
    def decides(message):
        rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
        return [b.get("callback_data") for row in rows for b in row] == ["allow", "reject"]

    def bot_decide(self, version_id, channel):
        return {"inline_keyboard": [[{"text": f"Allow once → [{channel}] {version_id}", "callback_data": "allow"}],
                                    [{"text": "Reject", "callback_data": "reject"}]]}

    def permissions(self):
        with closing(self.ex.connect(read_only=True)) as db:
            return db.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]

    def bind(self):
        with mock.patch.object(self.ex, "TOKEN_PATH", str(self.token_file)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.ex.bind()
        return out.getvalue()

    # -- binding
    def test_owner_binds_once_with_a_one_time_code_only(self):
        self.store("DELETE FROM settings WHERE name = 'owner_id'")
        self.message(STRANGER, "/start guessed-code")
        self.assertIn("This link is not valid", self.said())
        self.assertIsNone(self.owner())

        printed = self.bind()
        code = re.search(r"https://t\.me/intentrivet_stand_bot\?start=([A-Za-z0-9_-]{20,64})\n", printed)[1]
        self.assertNotIn(code, "".join(self.logged))  # the code reaches no log
        with closing(self.ex.connect(read_only=True)) as db:
            self.assertNotIn(code, json.dumps(db.execute("SELECT * FROM settings").fetchall()))  # only its SHA-256
        self.message(STRANGER, "/start wrong-" + code[6:])
        self.assertIsNone(self.owner())

        with mock.patch.object(self.ex, "BIND_TTL", -1):  # an expired link
            code = re.search(r"\?start=(\S+)", self.bind())[1]
        self.message(OWNER, f"/start {code}")
        self.assertIn("expired or was used already", self.said())
        self.assertIsNone(self.owner())

        code = re.search(r"\?start=(\S+)", self.bind())[1]
        self.message(OWNER, f"/start {code}")
        self.assertEqual(self.owner(), OWNER)
        self.assertIn("You are now the owner of this bot.", self.said())
        self.message(STRANGER, f"/start {code}")  # the same link again: used up, and an owner exists
        self.assertEqual(self.said(), "This is a private bot.")
        self.assertEqual(self.owner(), OWNER)
        self.assertIn("already has an owner (Telegram user id 111); no link is issued", self.bind())
        with closing(self.ex.connect(read_only=True)) as db:
            self.assertIsNone(self.ex.get_setting(db, "bind_code"))

    def test_messages_count_only_from_the_owners_private_chat(self):
        before = len(self.private())
        update = {"message": {"message_id": 1, "text": "Add channel", "from": {"id": OWNER, "is_bot": False},
                              "chat": {"id": -100555, "type": "supergroup"}}}  # the owner, but in a group
        with closing(self.ex.connect()) as db, self.ex.transaction(db):
            self.assertEqual(self.bot.apply(db, update, None), [])
        self.message(STRANGER, "Add channel")
        self.assertEqual(self.said(), "This is a private bot.")
        with closing(self.ex.connect(read_only=True)) as db:
            self.assertIsNone(self.ex.get_setting(db, "add_channel"))
        self.assertEqual(len(self.private()), before + 1)

    # -- channels
    def test_add_channel_needs_the_phrase_the_rights_and_a_free_name(self):
        self.message(OWNER, "Add channel")
        phrase = re.search(r"Connect check [0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}", self.said())[0]
        sent = len(self.private())
        self.stand_in("post", -1000000000301, "News", "Connect check 0000-0000-0000")  # another text
        self.bot.poll_once(0)
        self.assertEqual(len(self.private()), sent)

        real = self.ex.telegram_call

        def no_rights(method, payload, *args, **kwargs):
            result = real(method, payload, *args, **kwargs)
            return {**result, "can_post_messages": False} if method == "getChatMember" else result
        self.stand_in("post", -1000000000301, "News", phrase)
        with mock.patch.object(self.ex, "telegram_call", no_rights):
            self.bot.poll_once(0)
        self.assertIn("the bot is not an administrator allowed to post messages there", self.said())

        self.stand_in("post", -1000000000001, "Test again", phrase)  # the channel connected as test
        self.bot.poll_once(0)
        self.assertIn("already connected as [test]; nothing changed", self.said())

        self.message(OWNER, "/add_channel")
        phrase = re.search(r"Connect check \S+", self.said())[0]
        title = "News <b>bold</b> \u202emirror"
        self.stand_in("post", -1000000000301, title, phrase)
        self.bot.poll_once(0)
        found = self.private()[-1]
        self.assertIn("Title: News <b>bold</b> \\u202emirror\nchat_id: -1000000000301", found["text"])
        self.assertNotIn("parse_mode", found)
        self.assertNotIn("news", self.ex_channels())  # nothing is connected before the owner confirms
        self.message(OWNER, "test")
        self.assertIn("[test] is already the name of IntentRivet Test Channel (local stand)", self.said())
        self.message(OWNER, "Bad name!")
        self.assertIn("A name has 1 to 32 characters", self.said())
        self.message(OWNER, "news")
        self.assertIn("Connected [news]: News <b>bold</b> \\u202emirror (chat_id -1000000000301)", self.said())
        self.assertEqual(self.ex_channels()["news"], {"title": title, "chat_id": -1000000000301})
        self.assertEqual(self.ex_channels()["test"]["chat_id"], -1000000000001)  # the others stay
        self.stand_in("post", -1000000000302, "Late", phrase)  # the phrase is used up
        self.bot.poll_once(0)
        self.assertNotIn(-1000000000302, [c["chat_id"] for c in self.ex_channels().values()])
        listed = json.loads(self.tools_list({}))
        self.assertIn({"key": "news", "title": "News <b>bold</b> \\u202emirror", "chat_id": -1000000000301},
                      listed["channels"])
        self.assertFalse(json.loads(self.tools_list({"key": "x", "chat_id": 1}))["ok"])
        self.message(OWNER, "Channels")
        self.assertIn("[news] News <b>bold</b> \\u202emirror, chat_id -1000000000301", self.said())

    def ex_channels(self):
        with closing(self.ex.connect(read_only=True)) as db:
            return self.ex.channel_map(db)

    def tools_list(self, args):
        from test_stage1 import FakeContext
        ctx = FakeContext()
        plugin = load("intentrivet_list", ROOT / "plugin" / "__init__.py")
        plugin.register(ctx)
        return ctx.tools["intentrivet_list_channels"](args)

    def test_legacy_channels_are_imported_once_and_the_store_stays_the_source(self):
        legacy = Path(self.tmp.name, "legacy.json")
        legacy.write_text(json.dumps({"channels": {"test": {"title": "Edited title", "chat_id": -1000000000001}}}))
        with closing(self.ex.connect()) as db:
            self.assertIn("differs from the store and is ignored", self.ex.import_legacy_channels(db, legacy))
            self.assertIsNone(self.ex.import_legacy_channels(db, ROOT / "stand" / "channels.json"))
            self.assertEqual(self.ex.channel_map(db)["test"]["title"], "IntentRivet Test Channel (local stand)")

    # -- preview, decisions, publication
    def test_long_preview_comes_complete_in_marked_parts_with_buttons_last(self):
        text = "\n".join(f"Line {i}: " + "x" * 60 + ("\u001b[31m" if i == 7 else "") for i in range(60))[:4096]
        result = self.prepare({"channel": "test", "text": text})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["owner_preview"].startswith("Delivered to the owner in Telegram"))
        parts = [m for m in self.private() if result["version_id"] in m["text"]]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(m.get("reply_markup") is None for m in parts[:-1]))
        self.assertEqual(parts[-1]["reply_markup"], self.bot_decide(result["version_id"], "test"))
        self.assertTrue(all(len(m["text"].encode("utf-16-le")) // 2 <= 4096 and "parse_mode" not in m for m in parts))
        self.assertIn(f"Permission request {result['version_id']} (part 1 of {len(parts)})\n"
                      "Channel: [test] IntentRivet Test Channel (local stand)\nchat_id: -1000000000001\n", parts[0]["text"])
        body = parts[0]["text"].split("----- BEGIN TEXT -----\n", 1)[1]
        body += "".join(m["text"].split("text continued:\n", 1)[1] for m in parts[1:])
        self.assertEqual(body.split("\n----- END TEXT -----\n", 1)[0], self.ex.visible(text))
        self.assertIn("\\u001b[31m", body)
        self.assertEqual(self.record(result["version_id"])["action"]["text"], text)  # the send stays exact

    def test_allow_once_publishes_nothing_and_binds_this_version_to_its_chat(self):
        v = self.new_post("Demo announcement.\nAllowed for test only.")["version_id"]
        self.press(OWNER, self.card(), "allow")
        self.assertIn(f"Allowed one publication of {v} to [test] IntentRivet Test Channel (local stand) "
                      "(chat_id -1000000000001). Nothing is sent yet", self.said())
        edits = [m for m in self.stats()["private_log"] if m["method"] == "editMessageReplyMarkup"]
        self.assertEqual(edits[-1]["reply_markup"]["inline_keyboard"][0][0]["text"], "Allowed once")
        self.assertEqual(self.stats()["calls"], 0)  # nothing published by the button
        self.assertEqual(self.record(v)["status"], "Allowed: one publication")
        refused = self.publish({"version_id": v, "channel": "main"})
        self.assertIn("(403)", refused["error"])
        self.assertIn("'test', not 'main'", refused["error"])
        self.assertIsNone(self.record(v)["publication"]["used_at"])  # the permission is not used
        self.store(("UPDATE channels SET chat_id = -1000000000999 WHERE key = 'test'",))  # a remapped key
        try:
            published = self.publish({"version_id": v, "channel": "test"})
        finally:
            self.store(("UPDATE channels SET chat_id = -1000000000001 WHERE key = 'test'",))
        self.assertEqual(published["outcome"], "Published", published)
        s = self.stats()
        self.assertEqual((s["calls"], s["accepted"], s["posts"][-1]["chat_id"]), (1, 1, -1000000000001))
        self.assertIn(f"Published {v} to [test]", self.told())
        self.assertIn("(409)", self.publish({"version_id": v, "channel": "test"})["error"])
        self.assertEqual(self.stats()["calls"], 1)

    def test_the_answer_to_hermes_never_waits_for_the_owners_notice(self):
        real, release = self.ex.telegram_call, threading.Event()

        def held(method, payload, *args, **kwargs):  # the owner's notice hangs until released
            if method == "sendMessage" and str(payload.get("text")).startswith("Published"):
                release.wait(60)
            return real(method, payload, *args, **kwargs)
        v = self.new_post("Notice held.")["version_id"]
        self.press(OWNER, self.card(), "allow")
        with mock.patch.object(self.ex, "telegram_call", held):
            started = time.monotonic()
            published = self.publish({"version_id": v, "channel": "test"})  # the plugin waits 10 s at most
            self.assertEqual(published["outcome"], "Published", published)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIn("(409)", self.publish({"version_id": v, "channel": "test"})["error"])
            self.assertEqual((self.record(v)["status"], self.stats()["calls"]), ("Published", 1))
            self.assertFalse([m for m in self.private() if m["text"].startswith("Published")])  # still held
            release.set()
            self.assertIn(f"Published {v} to [test]", self.told())

        v = self.new_post("Notice refused.")["version_id"]
        self.press(OWNER, self.card(), "allow")
        self.stand_in("private", "fail")  # Telegram refuses the owner's notice
        published = self.publish({"version_id": v, "channel": "test"})
        self.assertEqual(published["outcome"], "Published", published)
        self.bot.notices.join()
        self.assertIn("owner bot: send failed: sendMessage: Forbidden", self.logged[-1])
        self.assertIn("(409)", self.publish({"version_id": v, "channel": "test"})["error"])
        self.assertEqual((self.record(v)["status"], self.stats()["calls"]), ("Published", 2))

        idle = type(self.bot)(self.ex)  # no thread delivers its notices: they wait, up to a bound
        for _ in range(idle.notices.maxsize + 1):
            idle.notify_outcome(self.record(v), "Published", "message_id 2")
        self.assertEqual((idle.notices.qsize(), idle.notices.maxsize), (100, 100))
        self.assertIn(f"outcome notice of {v} dropped: 100 notices are waiting already", self.logged[-1])

    def test_reject_stale_card_strangers_and_double_presses_change_no_decision(self):
        v1 = self.new_post("Draft one.")
        card1 = self.card()
        self.press(STRANGER, card1, "allow", chat=OWNER)
        self.press(STRANGER, card1, "allow")  # in the stranger's own chat
        answers = [m for m in self.stats()["private_log"] if m["method"] == "answerCallbackQuery"]
        self.assertEqual([a["text"] for a in answers[-2:]], ["Not allowed."] * 2)
        v2 = self.prepare({"channel": "test", "text": "Draft two.", "draft_id": v1["draft_id"]})["version_id"]
        card2 = self.card()
        self.press(OWNER, card1, "allow")  # the stale card
        self.assertIn(f"Not allowed: {v1['version_id']} is no longer the latest version", self.said())
        self.assertEqual(self.private()[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"open:{v2}")
        self.assertIsNone(self.record(v1["version_id"])["publication"])
        before = self.permissions()
        self.press(OWNER, card2, "reject")
        self.assertEqual(self.record(v2)["status"], "Rejected")
        self.press(OWNER, card2, "allow")  # after Reject: refused, the decision stands
        self.assertEqual((self.record(v2)["status"], self.permissions()), ("Rejected", before))
        self.assertIn("was rejected", self.publish({"version_id": v2, "channel": "test"})["error"])
        with closing(self.ex.connect(read_only=True)) as db:
            self.assertNotIn(v2, self.ex.pending_requests(db))
        v3 = self.new_post("Draft three.")["version_id"]
        card3 = self.card()
        self.press(OWNER, card3, "allow")
        self.press(OWNER, card3, "allow")  # a double press
        self.press(OWNER, card3, "reject")  # Reject after Allow once
        self.assertEqual((self.record(v3)["status"], self.permissions()), ("Allowed: one publication", before + 1))
        self.press(OWNER, 99999, "allow")  # no such card
        answers = [m for m in self.stats()["private_log"] if m["method"] == "answerCallbackQuery"]
        self.assertEqual(answers[-1]["text"], "This button is not active.")
        self.assertEqual(self.stats()["calls"], 0)

    def test_a_redelivered_update_changes_nothing_also_after_a_restart(self):
        v = self.new_post("Redelivery.")["version_id"]
        pressed = self.press(OWNER, self.card(), "allow")
        sent, before = len(self.stats()["private_log"]), self.permissions()
        self.stand_in("redeliver", pressed)
        self.bot.poll_once(0)
        restarted = type(self.bot)(self.ex)  # a new process reads the stored offset
        self.stand_in("redeliver", pressed)
        restarted.poll_once(0)
        self.assertEqual((len(self.stats()["private_log"]), self.permissions()), (sent, before))
        self.assertEqual(self.record(v)["status"], "Allowed: one publication")

    def test_a_failed_preview_keeps_the_version_and_pending_requests_reopens_it(self):
        self.stand_in("private", "fail")
        result = self.prepare({"channel": "test", "text": "Preview cannot be delivered."})
        self.assertTrue(result["ok"])
        self.assertTrue(result["owner_preview"].startswith("NOT delivered: Telegram did not take part 1 of 1"))
        v = result["version_id"]
        self.assertEqual(self.record(v)["status"], "Awaiting approval")
        self.stand_in("private", "ok")
        self.message(OWNER, "Pending requests")
        listing = self.private()[-1]
        self.assertIn(v, listing["text"])
        before = self.versions()
        self.press(OWNER, listing["message_id"], f"open:{v}")
        self.assertIn(f"Permission request {v}\n", self.private()[-1]["text"])
        self.assertEqual(self.versions(), before)  # reopening creates no version
        self.press(OWNER, self.card(), "allow")
        self.assertEqual(self.record(v)["status"], "Allowed: one publication")
        self.store("DELETE FROM settings WHERE name = 'owner_id'")
        result = self.prepare({"channel": "test", "text": "No owner yet."})
        self.assertIn("no owner is bound to the bot yet", result["owner_preview"])

    def versions(self):
        with closing(self.ex.connect(read_only=True)) as db:
            return db.execute("SELECT COUNT(*) FROM versions").fetchone()[0]

    def pending_ids(self):
        with closing(self.ex.connect(read_only=True)) as db:
            return self.ex.pending_requests(db)

    def test_pending_requests_come_in_pages_and_a_late_one_opens_and_is_decided(self):
        decide_all = ("INSERT INTO rejections SELECT version_id, 'test cleanup' FROM versions v WHERE NOT EXISTS "
                      "(SELECT 1 FROM permissions p WHERE p.version_id = v.version_id) AND NOT EXISTS "
                      "(SELECT 1 FROM rejections r WHERE r.version_id = v.version_id)")
        self.store(decide_all)  # what earlier tests left pending
        self.addCleanup(self.store, decide_all, "DELETE FROM channels WHERE key LIKE 'wide-%'")
        wide = "wide-" + "w" * 27  # the longest channel name, its title all escapes (6 units each)
        self.store(("INSERT INTO channels (key, title, chat_id, added_at) VALUES (?, ?, -1000000000400, 'now')",
                    (wide, "\u202e" * 128)))
        with closing(self.ex.connect()) as db:
            made = [self.ex.create_version(db, {"channel": (wide, "test")[i % 2],
                                                "text": "\u0007" * 70 + f" request {i}\nsecond line"})["version_id"]
                    for i in range(89)]
            for version_id in made[:30]:  # 30 outcomes unknown, listed on the first page
                db.execute("INSERT INTO permissions VALUES (?, -1000000000001, 'now', 'now', ?, 'lost', 'now')",
                           (version_id, self.ex.OUTCOME_UNKNOWN))
        self.stand_in("private", "fail")
        late = self.prepare({"channel": wide, "text": "Late request, its preview not delivered."})
        self.stand_in("private", "ok")
        self.assertTrue(late["owner_preview"].startswith("NOT delivered"), late)
        pending = self.pending_ids()
        self.assertEqual((len(pending), pending[-1]), (60, late["version_id"]))

        self.message(OWNER, "Pending requests")
        listed = []
        for page in range(1, 7):
            shown = self.private()[-1]
            text, keyboard = shown["text"], shown["reply_markup"]["inline_keyboard"]
            self.assertLessEqual(units(text), 4000)  # Telegram allows 4096
            self.assertTrue(text.startswith(f"Pending requests {page * 10 - 9}-{page * 10} of 60 (page {page} of 6)"))
            self.assertNotIn("\u202e", text)
            self.assertEqual("Outcome unknown (check the channel; never sent again)" in text, page == 1)
            first_page = text if page == 1 else first_page
            listed += [row[0]["callback_data"][len("open:"):] for row in keyboard[:-1]]
            turn = {b["text"]: b["callback_data"] for b in keyboard[-1]}
            self.assertEqual(sorted(turn), (["Next"] if page == 1 else ["Next", "Previous"] if page < 6 else ["Previous"]))
            if page < 6:
                self.press(OWNER, shown["message_id"], turn["Next"])
        with closing(self.ex.connect(read_only=True)) as db:
            unknown = db.execute("SELECT COUNT(*) FROM permissions WHERE outcome = 'Outcome unknown'").fetchone()[0]
        self.assertIn(f"never sent again): {', '.join(made[20:30])} (and {unknown - 10} earlier)", first_page)
        self.assertEqual(listed, pending)  # every request, once, oldest first

        self.press(OWNER, shown["message_id"], f"open:{late['version_id']}")  # the last page, without the others
        self.assertIn(f"Permission request {late['version_id']}\n", self.private()[-1]["text"])
        self.press(OWNER, self.card(), "allow")
        self.assertEqual(self.record(late["version_id"])["status"], "Allowed: one publication")
        self.assertEqual(self.pending_ids(), pending[:-1])  # the earlier 59 still wait, undecided
        self.press(OWNER, shown["message_id"], "pending:99")  # a page gone: the last one there is
        self.assertTrue(self.said().startswith("Pending requests 51-59 of 59 (page 6 of 6)"))

    def test_outcome_unknown_is_told_and_never_resent(self):
        self.stand_in("mode", "drop")
        v = self.new_post("Lost reply.")["version_id"]
        self.press(OWNER, self.card(), "allow")
        self.assertIn("Outcome unknown", self.publish({"version_id": v, "channel": "test"})["error"])
        self.assertIn(f"Outcome unknown: {v} to [test]", self.told())
        self.assertIn("will not be sent again", self.said())
        self.assertIn("(409)", self.publish({"version_id": v, "channel": "test"})["error"])
        self.assertEqual((self.stats()["calls"], self.stats()["accepted"]), (1, 1))

    def test_a_webhook_stops_polling_and_is_left_in_place(self):
        self.stand_in("webhook", "https://hooks.example.org/x")
        try:
            with self.assertRaises(self.ex.Rejected) as refused:
                self.bot.poll_once(0)
            self.assertIn("webhook is active", str(refused.exception))
            with mock.patch.object(self.ex, "TOKEN_PATH", str(self.token_file)), \
                    contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
                self.ex.check()
            self.assertIn("the bot has a webhook (host hooks.example.org)", out.getvalue())
            self.assertIn("it was left in place", out.getvalue())
        finally:
            self.stand_in("webhook", "none")

    def test_the_agent_has_no_route_to_decide_bind_or_change_channels(self):
        for path in ("/versions/post-1-v1/allow", "/versions/post-1-v1/reject", "/channels", "/owner",
                     "/updates", "/callback", "/telegram", "/bind"):
            self.assertEqual(self.http(path, {"version_id": "post-1-v1", "owner_id": STRANGER})[0], 404, path)
        status, listed = self.http_get("/channels")
        self.assertEqual(status, 200)
        self.assertNotIn("owner", json.dumps(listed))

    def http_get(self, path):
        import urllib.request
        from test_stage1 import AUTH
        with urllib.request.urlopen(urllib.request.Request(self.url + path, headers=AUTH)) as response:
            return response.status, json.loads(response.read())


class SetupScriptTest(unittest.TestCase):
    """stand/setup.sh and stand/live-setup.sh, copied into a temporary directory, with stub docker and
    podman commands and a pseudo-terminal: the engine is checked before the token prompt, and a token
    that cannot be handed to the executor's user leaves the stored one in place."""

    DOCKER = """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
case $1 in
  version) [ "$STUB_COMPONENTS" != unreachable ] || exit 1; echo "$STUB_COMPONENTS" ;;
  info) echo "$STUB_ENGINE_ROOT" ;;
  *) exit 1 ;;
esac
"""
    PODMAN = """#!/bin/sh
echo "podman $*" >> "$STUB_LOG"
case "$1 $3" in
  "info {{.Host.Security.Rootless}}") echo "$STUB_ROOTLESS" ;;
  "info {{.Store.GraphRoot}}") echo /home/user/.local/share/containers/storage ;;
  unshare*) [ "$STUB_CHOWN" = ok ] ;;
  *) exit 1 ;;
esac
"""
    TOKEN = "123456789:stub-token-of-the-setup-test"  # valid characters, not a real token's length

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, stubs = Path(tmp.name, "repo"), Path(tmp.name, "bin")
        (self.repo / "stand").mkdir(parents=True)
        stubs.mkdir()
        for name in ("setup.sh", "live-setup.sh", "check-engine.sh"):
            shutil.copy(ROOT / "stand" / name, self.repo / "stand" / name)
        for name, script in (("docker", self.DOCKER), ("podman", self.PODMAN)):
            (stubs / name).write_text(script)
            (stubs / name).chmod(0o755)
        self.log = Path(tmp.name, "calls.log")
        self.log.touch()
        self.token_file = self.repo / ".stand-live" / "telegram_bot_token"
        self.env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": tmp.name, "STUB_LOG": str(self.log),
                    "STUB_COMPONENTS": "Podman Engine;Conmon;OCI Runtime (runc);Engine;",
                    "STUB_ENGINE_ROOT": "/home/user/.local/share/containers/storage",
                    "STUB_ROOTLESS": "true", "STUB_CHOWN": "ok"}

    def run_script(self, name, token="", **stub):
        """Run in a pseudo-terminal; type token at the hidden prompt if it comes. Returns (code, output)."""
        master, slave = os.openpty()
        p = subprocess.Popen(["sh", f"stand/{name}"], cwd=self.repo, env={**self.env, **stub},
                             stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        out = b""
        try:
            while select.select([master], [], [], 10)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # the script and its programs closed the terminal
                    break
                out += chunk
                if out.endswith(b"(input hidden): "):
                    os.write(master, token.encode() + b"\n")
            p.wait(10)
        finally:
            p.kill()
            os.close(master)
        return p.returncode, out.decode().replace("\r\n", "\n")

    def store_token(self, value):
        self.token_file.parent.mkdir(mode=0o700)
        self.token_file.write_text(value)

    def test_an_unsupported_engine_stops_before_the_prompt_and_changes_nothing(self):
        self.store_token("111:stored\n")
        cases = {"The Docker CLI reaches no container engine": {"STUB_COMPONENTS": "unreachable"},
                 "reaches Docker Engine or another engine, not Podman": {
                     "STUB_COMPONENTS": "Engine;containerd;runc;docker-init;"},
                 "podman does not run rootless for this user": {"STUB_ROOTLESS": "false"},
                 "reaches a Podman other than this user's rootless Podman": {
                     "STUB_ENGINE_ROOT": "/var/lib/containers/storage"}}
        for problem, stub in cases.items():
            for script in ("live-setup.sh", "setup.sh"):
                code, out = self.run_script(script, self.TOKEN, **stub)
                self.assertEqual(code, 1, (script, out))
                self.assertIn(problem, out)
                self.assertIn("Nothing was changed.", out)
                self.assertNotIn("input hidden", out)
        calls = self.log.read_text()
        self.assertNotIn("unshare", calls)  # Podman's namespace is never used for another engine
        self.assertNotIn("compose", calls)  # nothing is built or started
        self.assertEqual(self.token_file.read_text(), "111:stored\n")
        self.assertEqual(sorted(os.listdir(self.token_file.parent)), ["telegram_bot_token"])

    def test_a_token_that_cannot_be_handed_over_keeps_the_stored_one(self):
        self.store_token("111:stored\n")
        code, out = self.run_script("live-setup.sh", self.TOKEN, STUB_CHOWN="fail")
        self.assertEqual(code, 1, out)
        self.assertIn("Could not hand the token file to the executor's container user", out)
        self.assertIn("a stored token is kept", out)
        self.assertNotIn(self.TOKEN, out)
        self.assertEqual(self.token_file.read_text(), "111:stored\n")
        self.assertEqual(sorted(os.listdir(self.token_file.parent)), ["telegram_bot_token"])

        code, out = self.run_script("live-setup.sh", self.TOKEN)  # rootless Podman, the handover works
        self.assertEqual(code, 0, out)
        self.assertNotIn(self.TOKEN, out)  # not echoed, never printed
        self.assertIn(f"telegram_bot_token  {len(self.TOKEN)} bytes, sha256 ", out)
        self.assertEqual(self.token_file.read_text(), self.TOKEN + "\n")
        self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o400)
        self.assertEqual(sorted(os.listdir(self.token_file.parent)), ["channels.json", "telegram_bot_token"])
        self.assertIn("podman unshare chown 10001:10001 .stand-live/telegram_bot_token.new", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
