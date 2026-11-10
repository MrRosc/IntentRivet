"""Stage 3 checks: the Telegram check, verified replies, diagnostics, clean input.

The same in-process stand as stage 2 (real executor, Telegram stand-in and plugin); no network.
Run: python3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest import mock

from test_stage1 import AUTH
from test_stage2 import ROOT, TOKEN, Stand

TEXT = "\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 IntentRivet.\n\u041e\u0434\u043d\u0430 \u043f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u044f \u043f\u043e \u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u044e \u043e\u043f\u0435\u0440\u0430\u0442\u043e\u0440\u0430. 🚀"


class Stage3Test(Stand):
    def post_raw(self, path, data):
        request = urllib.request.Request(self.url + path, data=data, method="POST", headers=AUTH)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def versions(self):
        with closing(self.ex.connect(read_only=True)) as db:
            return db.execute("SELECT COUNT(*) FROM versions").fetchone()[0]

    def check(self, read=None):
        token = Path(self.tmp.name, "token")
        token.write_text(TOKEN)
        with mock.patch.object(self.ex, "TOKEN_PATH", str(token)), \
                mock.patch.object(self.ex, "telegram_call", read or self.ex.telegram_call), \
                contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as done:
            self.ex.check()
        return done.exception.code, out.getvalue()

    def test_unpaired_surrogate_is_refused_and_valid_unicode_kept(self):
        before = self.versions()
        for data in (b'{"channel": "test", "text": "a\\ud800b"}',
                     b'{"channel": "test", "text": "x", "draft_id": "\\udfff"}'):
            status, reply = self.post_raw("/versions", data)
            self.assertEqual(status, 400, data)
            self.assertIn("unpaired surrogate (such as \\ud800)", reply["error"])
        self.assertEqual(self.post_raw("/versions", b"[" * 5000)[0], 400)  # nested too deep to parse
        self.assertEqual(self.versions(), before)
        # The next valid request works; valid Unicode, escaped as a surrogate pair or raw, is kept exactly.
        data = ('{"channel": "test", "text": "\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 IntentRivet.\\n\u041e\u0434\u043d\u0430 \u043f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u044f \u043f\u043e \u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u044e '
                '\u043e\u043f\u0435\u0440\u0430\u0442\u043e\u0440\u0430. \\ud83d\\ude80"}').encode()
        status, created = self.post_raw("/versions", data)
        self.assertEqual(status, 201, created)
        self.assertEqual(self.record(created["version_id"])["action"]["text"], TEXT)

    def test_connection_check_reads_and_sends_nothing(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("Bot:      IntentRivet stand-in (@intentrivet_stand_bot, id 1000001)", out)
        self.assertIn("Webhook:  none (the executor polls the bot's updates)", out)
        self.assertIn("Owner:    not bound yet (executor.py bind)", out)
        self.assertIn("Channel:  [test] Stand-in channel -1000000000001\n"
                      "          chat_id -1000000000001, type channel, private\n"
                      "          bot: administrator, can_post_messages: True\n", out)
        self.assertIn("Channel:  [main] Stand-in channel -1000000000002\n", out)
        self.assertIn("RESULT: ready\n", out)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(self.stats()["calls"], 0)  # no sendMessage

        real_call = self.ex.telegram_call

        def group_without_rights(method, payload, *args, **kwargs):
            result = real_call(method, payload, *args, **kwargs)
            changed = {"getChat": {"type": "supergroup"}, "getChatMember": {"can_post_messages": False}}
            return {**result, **changed.get(method, {})}

        code, out = self.check(group_without_rights)
        self.assertEqual(code, 1)
        self.assertIn("is not a channel", out)
        self.assertIn("the bot is not an administrator allowed to post messages", out)
        self.assertIn("RESULT: not ready", out)

    def test_reply_with_another_text_is_outcome_unknown_and_never_resent(self):
        self.mock.main("mode", "altered")
        post = self.new_post(TEXT)
        self.allow(post["version_id"])
        body = {"version_id": post["version_id"], "channel": "test"}
        status, reply = self.http("/publications", body)
        self.assertEqual(status, 502)
        self.assertIn("Outcome unknown: Telegram reported message_id 1, but its chat or text differs "
                      "from the stored snapshot", reply["error"])
        self.mock.main("mode", "ok")
        self.assertEqual(self.http("/publications", body)[0], 409)
        self.assertEqual((self.stats()["calls"], self.stats()["accepted"]), (1, 1))

    def test_send_attempts_are_logged_without_token_or_text(self):
        self.logged.clear()
        post = self.new_post(TEXT)
        v = post["version_id"]
        self.allow(v)
        sent = self.publish({"version_id": v, "channel": "test"})
        self.assertEqual(sent["detail"], "message_id 1, https://t.me/c/0000000001/1")
        self.assertEqual(self.http("/publications", {"version_id": v, "channel": "test"})[0], 409)
        self.assertEqual(self.logged, [f"sendMessage attempt: {v} -> chat_id -1000000000001",
                                       f"sendMessage result: {v}: Published; message_id 1, https://t.me/c/0000000001/1"])
        self.assertNotIn(TOKEN, "\n".join(self.logged))
        self.assertNotIn("IntentRivet.", "\n".join(self.logged))

    def test_post_link(self):
        link = self.ex.post_link
        self.assertEqual(link({"id": -1001234567890, "username": "intentrivet_test"}, 7), "https://t.me/intentrivet_test/7")
        self.assertEqual(link({"id": -1001234567890}, 7), "https://t.me/c/1234567890/7")
        self.assertEqual(link({"id": -1001234567890, "username": "bad/name"}, 7), "https://t.me/c/1234567890/7")
        self.assertIsNone(link({"id": 12345}, 7))

    def test_live_permission_requests_refuse_piped_input(self):
        with mock.patch.object(self.ex, "TELEGRAM_API", "https://api.telegram.org"), \
                mock.patch("sys.stdin", io.StringIO("1\nallow\n")), self.assertRaises(SystemExit) as refused:
            self.ex.requests()
        self.assertIn("interactive terminal", str(refused.exception.code))


if __name__ == "__main__":
    import unittest
    unittest.main()
