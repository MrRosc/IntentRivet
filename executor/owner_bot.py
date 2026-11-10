"""The owner's Telegram interface, inside the trusted executor: long polling of the publisher bot.

One owner, bound once with a one-time code that only the host operator sees (executor.py bind).
In the owner's private chat: Add channel, Channels and Pending requests, and a preview card with
Allow once / Reject for each prepared version. Allow once and Reject call the executor's own
allow() and reject(), the functions the operator CLI uses; Allow once publishes nothing, but from
then on Hermes may publish that version once, at any time. The card's last line, after the post
text, and the Allow once button name the version, the channel and its chat_id: no post text can
come after them. Previews go out one at a time and without link previews. Updates
count only from the owner's private chat, by the numeric user id Telegram sets in the update; a
button counts only on a stored preview card; a channel post counts only as the one-time phrase of
an Add channel in progress. Each update is applied in one transaction with the stored update
offset, so a redelivered update changes nothing; replies go out after the commit. This thread is
the bot's only getUpdates reader. A second thread delivers the outcome notices from a bounded
queue, so the answer to a publication never waits for the owner's chat.
"""

import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
from contextlib import closing

POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "25"))  # seconds of one long poll
ADD_CHANNEL_TTL = 1800      # seconds an Add channel in progress stays open
PART_LIMIT = 4000           # UTF-16 code units per preview message (Telegram allows 4096)
PAGE_SIZE = 10              # Pending requests per message, each with its Open button
NOTICES_MAX = 100           # outcome notices waiting for delivery; more are dropped (and logged)
CHANNEL_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MENU = {"keyboard": [[{"text": "Add channel"}, {"text": "Channels"}, {"text": "Pending requests"}]],
        "resize_keyboard": True, "is_persistent": True}
NO_LINK_PREVIEW = {"is_disabled": True}  # no page fetched from a link in the text sits in the owner's chat
ALLOW_MEANING = ("Allow once gives Hermes one attempt to publish exactly this text to this channel; Hermes may "
                 "use it at once, without asking you again. The button itself sends nothing. A permission "
                 "does not expire and cannot be withdrawn.")
HELP = ("This is your publisher bot: it posts to your channels only what you allow here. Hermes talks to you "
        "through its own bot; that bot, and any other account Hermes can use, must not be an administrator "
        "of your channels, or it could post there without you.\n"
        "Add channel: connect one of your channels (again for each further one). Channels: the connected "
        "ones. Pending requests: posts that wait for your decision.\n"
        "Ask Hermes to prepare a post: its exact preview comes here. Allow once gives Hermes one attempt to "
        "publish exactly that text to that channel; the button sends nothing, but Hermes may publish at once, "
        "without asking you again. A permission does not expire and cannot be withdrawn.")


def units(text):
    """Length as Telegram counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


class OwnerBot:
    def __init__(self, ex):
        self.ex = ex  # the executor module: its store, allow(), reject() and Bot API calls
        self.bot_id = None
        self.notices = queue.Queue(NOTICES_MAX)
        self.card_lock = threading.Lock()  # one preview at a time: the parts of two never interleave

    # -- Untrusted text (channel titles, post text) is shown, never interpreted: no parse_mode, and
    # control and invisible characters as escapes (the executor's visible()); a title on one line.
    def one_line(self, text):
        return self.ex.visible(str(text)).replace("\n", "\\n")

    def clip(self, text, limit):
        """one_line() in at most limit UTF-16 code units: cut between characters, never inside an
        escape, and marked with an ellipsis."""
        shown = [self.one_line(c) for c in str(text)]
        if sum(map(units, shown)) <= limit:
            return "".join(shown)
        kept, size = [], units("…")
        for part in shown:
            if size + units(part) > limit:
                break
            kept.append(part)
            size += units(part)
        return "".join(kept) + "…"

    def say(self, chat_id, text, markup=None):
        return ("send", {"chat_id": chat_id, "text": self.ex.visible(text), "link_preview_options": NO_LINK_PREVIEW,
                         **({"reply_markup": markup} if markup else {})})

    @staticmethod
    def decide(record):
        """The card's buttons: Allow once names the channel and the version it allows."""
        return {"inline_keyboard": [
            [{"text": f"Allow once → [{record['channel']}] {record['version_id']}", "callback_data": "allow"}],
            [{"text": "Reject", "callback_data": "reject"}]]}

    def mark(self, chat_id, message_id, label):
        """The card's buttons give way to its decision; the store, not this edit, is the truth."""
        return ("edit", {"chat_id": chat_id, "message_id": message_id,
                         "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": "done"}]]}})

    def me(self):
        if self.bot_id is None:
            self.bot_id = self.ex.telegram_call("getMe", {})["id"]
        return self.bot_id

    # -- The loop
    def run(self):
        last = None
        while True:
            try:
                self.poll_once(POLL_TIMEOUT)
                last = None
            except self.ex.Rejected as e:  # Telegram refused: e.g. a webhook, or another reader
                problem = str(e)
                if "webhook" in problem:
                    problem += (" (the bot has a webhook, so it cannot be polled; it was left in place: remove "
                                "it yourself with the Bot API's deleteWebhook if this bot is for IntentRivet)")
                elif "other getUpdates" in problem:
                    problem += " (another program reads this bot's updates; only one may)"
                if problem != last:
                    self.ex.log(f"owner bot: {problem}; retrying every 30 s")
                last = problem
                time.sleep(30)
            except Exception as e:  # the type only: nothing that could carry the token
                self.ex.log(f"owner bot: {type(e).__name__}; retrying in 5 s")
                time.sleep(5)

    def poll_once(self, timeout=0):
        with closing(self.ex.connect()) as db:
            offset = int(self.ex.get_setting(db, "update_offset") or 0)
        updates = self.ex.telegram_call("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": ["message", "callback_query", "channel_post"]}, list, timeout=timeout + 10)
        for update in updates:
            if isinstance(update, dict) and type(update.get("update_id")) is int:
                self.handle(update)

    def handle(self, update):
        found = self.inspect_channel_post(update)  # its Bot API reads happen outside the transaction
        with closing(self.ex.connect()) as db:
            with self.ex.transaction(db):
                if update["update_id"] < int(self.ex.get_setting(db, "update_offset") or 0):
                    return  # applied already: a redelivered update changes nothing
                steps = self.apply(db, update, found)
                self.ex.set_setting(db, "update_offset", update["update_id"] + 1)
        for step in steps:
            self.perform(step)

    def perform(self, step):
        kind, arg = step
        try:
            if kind == "card":
                delivered, detail = self.deliver_card(arg)
                if not delivered:
                    self.ex.log(f"owner bot: preview of {arg} not delivered: {detail}")
                return
            method = {"send": "sendMessage", "edit": "editMessageReplyMarkup", "answer": "answerCallbackQuery"}[kind]
            self.ex.telegram_call(method, arg, (dict, bool))
        except Exception as e:
            self.ex.log(f"owner bot: {kind} failed: {e if isinstance(e, self.ex.Rejected) else type(e).__name__}")

    # -- One update, inside the transaction that also stores its offset
    def apply(self, db, update, found):
        owner = self.ex.owner_id(db)
        if "callback_query" in update:
            return self.on_button(db, owner, update["callback_query"])
        if "channel_post" in update:
            return self.on_channel_post(db, owner, found)
        message = update.get("message")
        if not isinstance(message, dict):
            return []
        chat, sender, text = message.get("chat") or {}, message.get("from") or {}, message.get("text")
        # A person in a private chat, by the numeric ids Telegram sets: never by name or message text.
        if not (chat.get("type") == "private" and type(sender.get("id")) is int and not sender.get("is_bot")
                and chat.get("id") == sender["id"]):
            return []
        text = text.strip() if isinstance(text, str) else ""
        if sender["id"] != owner:
            return self.on_stranger(db, owner, sender["id"], text)
        return self.on_owner(db, owner, text)

    def on_stranger(self, db, owner, user, text):
        if owner is None and text.startswith("/start "):
            if self.bind(db, user, text[len("/start "):].strip()):
                return [self.say(user, "You are now the owner of this bot.\n\n" + HELP, MENU)]
            return [self.say(user, "This link is not valid: it has expired or was used already. "
                                   "Ask for a new one from the host where the bot is set up.")]
        return [self.say(user, "This is a private bot.")]

    def bind(self, db, user, code):
        stored = self.ex.get_setting(db, "bind_code")
        if not stored:
            return False
        stored = json.loads(stored)
        if time.time() > stored["expires"]:
            self.ex.set_setting(db, "bind_code", None)
            return False
        if not secrets.compare_digest(hashlib.sha256(code.encode()).hexdigest(), stored["sha256"]):
            return False
        self.ex.set_setting(db, "owner_id", user)
        self.ex.set_setting(db, "bind_code", None)  # used up; binding stays closed while an owner exists
        return True

    def on_owner(self, db, owner, text):
        if text in ("Add channel", "/add_channel"):
            return self.add_channel(db, owner)
        if text in ("Channels", "/channels"):
            return [self.say(owner, self.channel_list(db), MENU)]
        if text in ("Pending requests", "/pending"):
            return self.pending(db, owner)
        if text == "/cancel":
            self.ex.set_setting(db, "add_channel", None)
            return [self.say(owner, "Cancelled; nothing changed.", MENU)]
        session = self.session(db)
        if session and session["step"] == "name" and not text.startswith("/"):
            return self.name_channel(db, owner, session, text)
        return [self.say(owner, HELP, MENU)]

    # -- Add channel: phrase -> channel post -> the bot's rights -> the owner names it
    def session(self, db):
        raw = self.ex.get_setting(db, "add_channel")
        session = json.loads(raw) if raw else None
        return session if session and session["expires"] > time.time() else None

    def add_channel(self, db, owner):
        phrase = "Connect check " + "-".join(secrets.token_hex(2).upper() for _ in range(3))
        self.ex.set_setting(db, "add_channel", json.dumps(
            {"step": "post", "phrase": phrase, "expires": int(time.time()) + ADD_CHANNEL_TTL}))
        return [self.say(owner, f"To connect one of your channels, within {ADD_CHANNEL_TTL // 60} minutes:\n"
                                "1. In the channel's settings, add this bot as an administrator with the right to "
                                "post messages; it needs no other right. Hermes' own bot, or any other account "
                                "Hermes can use, must not be an administrator of the channel: it could post there "
                                "without your permission.\n"
                                "2. Post this exact text in the channel. It is a real post that your subscribers "
                                "can see; delete it once the channel is connected:\n\n"
                                f"{phrase}\n\n"
                                "I will show you the channel I find, and connect it only once you confirm it and "
                                "give it a name. /cancel to stop.")]

    def inspect_channel_post(self, update):
        """Bot API reads for a channel post with the phrase of the Add channel in progress."""
        post = update.get("channel_post")
        chat = post.get("chat") if isinstance(post, dict) else None
        if not (isinstance(chat, dict) and chat.get("type") == "channel" and type(chat.get("id")) is int):
            return None
        with closing(self.ex.connect()) as db:
            session = self.session(db)
        if not (session and session["step"] == "post" and post.get("text") == session["phrase"]):
            return None
        try:
            fresh = self.ex.telegram_call("getChat", {"chat_id": chat["id"]})
            member = self.ex.telegram_call("getChatMember", {"chat_id": chat["id"], "user_id": self.me()})
        except self.ex.Rejected as e:
            return {"phrase": session["phrase"], "problem": f"I could not check that channel ({e})"}
        problem = None
        if not (fresh.get("type") == "channel" and fresh.get("id") == chat["id"]):
            problem = "Telegram does not report it as that channel"
        elif not (member.get("status") == "administrator" and member.get("can_post_messages") is True):
            problem = "the bot is not an administrator allowed to post messages there"
        title = fresh.get("title") if isinstance(fresh.get("title"), str) else ""
        return {"phrase": session["phrase"], "chat_id": chat["id"], "title": title, "problem": problem}

    def on_channel_post(self, db, owner, found):
        session = self.session(db)
        if not (found and owner is not None and session and session["step"] == "post"
                and session["phrase"] == found["phrase"]):
            return []
        if found["problem"]:
            return [self.say(owner, f"I found the check text, but {found['problem']}. Fix that and post the "
                                    "same text again, or /cancel.")]
        taken = db.execute("SELECT key FROM channels WHERE chat_id = ?", (found["chat_id"],)).fetchone()
        if taken:
            self.ex.set_setting(db, "add_channel", None)
            return [self.say(owner, f"That channel is already connected as [{taken[0]}]; nothing changed.", MENU)]
        self.ex.set_setting(db, "add_channel", json.dumps({
            "step": "name", "chat_id": found["chat_id"], "title": found["title"],
            "expires": int(time.time()) + ADD_CHANNEL_TTL}))
        return [self.say(owner, "I found this channel:\n"
                                f"Title: {self.one_line(found['title'])}\n"
                                f"chat_id: {found['chat_id']}\n"
                                "Bot: administrator, may post messages\n\n"
                                "To connect it, send a short name of your choice for it: a-z, 0-9 and -, for "
                                "example news or blog. Hermes will use that name. /cancel to stop.")]

    def name_channel(self, db, owner, session, name):
        if not CHANNEL_KEY.fullmatch(name):
            return [self.say(owner, "A name has 1 to 32 characters: a-z, 0-9 and -, starting with a letter or "
                                    "digit. Send another name, or /cancel.")]
        row = db.execute("SELECT title FROM channels WHERE key = ?", (name,)).fetchone()
        if row:
            return [self.say(owner, f"[{name}] is already the name of {self.one_line(row[0])}; a connected "
                                    "channel is never replaced. Send another name, or /cancel.")]
        if db.execute("SELECT 1 FROM channels WHERE chat_id = ?", (session["chat_id"],)).fetchone():
            self.ex.set_setting(db, "add_channel", None)
            return [self.say(owner, "That channel is connected already; nothing changed.", MENU)]
        db.execute("INSERT INTO channels (key, title, chat_id, added_at) VALUES (?, ?, ?, ?)",
                   (name, session["title"], session["chat_id"], self.ex.now()))
        self.ex.set_setting(db, "add_channel", None)
        return [self.say(owner, f"Connected [{name}]: {self.one_line(session['title'])} (chat_id "
                                f"{session['chat_id']}). Hermes can now prepare posts for [{name}]. You can "
                                "delete the check text from the channel now. To connect another channel, use "
                                "Add channel again.", MENU)]

    def channel_list(self, db):
        channels = self.ex.channel_map(db)
        if not channels:
            return "No channel is connected yet. Use Add channel."
        return "Connected channels:\n" + "\n".join(
            f"[{key}] {self.one_line(cfg['title'])}, chat_id {cfg['chat_id']}" for key, cfg in channels.items())

    def pending(self, db, owner, page=1):
        """Pending requests, oldest first, PAGE_SIZE per message, each with its Open button; Previous
        and Next reach the others. Titles and texts are clipped and only ten Outcome unknown ids are
        listed, so a page stays far below Telegram's limit whatever the store holds."""
        pending = self.ex.pending_requests(db)
        pages = max(1, -(-len(pending) // PAGE_SIZE))
        page = min(max(page, 1), pages)
        shown = pending[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        lines = []
        for version_id in shown:
            record = self.ex.fetch(db, version_id)
            first = record["action"]["text"].split("\n")[0]
            lines.append(f"{version_id} -> [{self.clip(record['channel'], 32)}] "
                         f"{self.clip(record['channel_title'], 64)}: {self.clip(first, 60)}")
        if not pending:
            text = "No pending requests."
        elif pages == 1:
            text = "Pending requests:\n" + "\n".join(lines)
        else:
            start = (page - 1) * PAGE_SIZE + 1
            text = (f"Pending requests {start}-{start + len(lines) - 1} of {len(pending)} (page {page} of "
                    f"{pages}):\n" + "\n".join(lines))
        unknown = [r[0] for r in db.execute("SELECT version_id FROM permissions WHERE outcome = ? ORDER BY rowid",
                                            (self.ex.OUTCOME_UNKNOWN,))]
        if unknown and page == 1:  # the latest ten: the executor's own short ids
            earlier = f" (and {len(unknown) - 10} earlier)" if len(unknown) > 10 else ""
            text += f"\n\nOutcome unknown (check the channel; never sent again): {', '.join(unknown[-10:])}{earlier}"
        buttons = [[{"text": f"Open {v}", "callback_data": f"open:{v}"}] for v in shown]
        turn = [{"text": label, "callback_data": f"pending:{to}"}
                for label, to in (("Previous", page - 1), ("Next", page + 1)) if 1 <= to <= pages]
        if turn:
            buttons.append(turn)
        return [self.say(owner, text, {"inline_keyboard": buttons} if buttons else MENU)]

    # -- Buttons: only the owner's, in the owner's private chat; a decision only on a stored card
    def on_button(self, db, owner, query):
        def answer(text):
            return ("answer", {"callback_query_id": query.get("id"), "text": text[:190]})
        sender, message = query.get("from") or {}, query.get("message") or {}
        chat, data = message.get("chat") or {}, query.get("data")
        if not (owner is not None and sender.get("id") == owner and chat.get("id") == owner
                and chat.get("type") == "private"):
            return [answer("Not allowed.")]
        if isinstance(data, str) and (turn := re.fullmatch(r"pending:([0-9]{1,6})", data)):
            return [answer("Pending requests."), *self.pending(db, owner, int(turn[1]))]
        if isinstance(data, str) and data.startswith("open:"):
            version_id = data[len("open:"):]
            if version_id not in self.ex.pending_requests(db):
                return [answer(f"{version_id} has no decision to make.")]
            return [answer(f"Opening {version_id}."), ("card", version_id)]
        card = db.execute("SELECT version_id FROM cards WHERE chat_id = ? AND message_id = ?",
                          (owner, message.get("message_id"))).fetchone()
        if data not in ("allow", "reject", "done") or not card:
            return [answer("This button is not active.")]
        record = self.ex.fetch(db, card[0])
        version_id, message_id = record["version_id"], message["message_id"]
        if data == "done":
            return [answer(f"{version_id}: {record['status']}.")]
        target = (f"[{record['channel']}] {self.one_line(record['channel_title'])} "
                  f"(chat_id {record['action']['chat_id']})")
        try:
            if data == "allow":
                self.ex.allow(db, record)
            else:
                self.ex.reject(db, version_id)
        except self.ex.Superseded as changed:
            return [answer("Not allowed: a newer version exists."),
                    self.mark(owner, message_id, f"Superseded by {changed.latest}"),
                    self.say(owner, f"Not allowed: {version_id} is no longer the latest version of draft "
                                    f"{record['draft_id']}; {changed.latest} was created after it. Open "
                                    f"{changed.latest} and decide on it.",
                             {"inline_keyboard": [[{"text": f"Open {changed.latest}",
                                                    "callback_data": f"open:{changed.latest}"}]]})]
        except self.ex.Rejected as e:  # decided already: the first decision stands
            return [answer(str(e)), self.mark(owner, message_id, self.ex.fetch(db, version_id)["status"])]
        if data == "allow":
            return [answer("Allowed once."), self.mark(owner, message_id, "Allowed once"),
                    self.say(owner, f"Allowed one publication of {version_id} to {target}. Nothing is sent "
                                    "yet. Hermes may publish it at any time, without asking you again; to have it "
                                    f"published now, ask Hermes to publish {version_id}. This permission does not "
                                    "expire and cannot be withdrawn.")]
        return [answer("Rejected."), self.mark(owner, message_id, "Rejected"),
                self.say(owner, f"Rejected {version_id}: it will not be published. A new decision needs a new "
                                "version.")]

    # -- The preview card, delivered from the executor's HTTP thread or from Pending requests
    def card_parts(self, record):
        """The exact text, shown safely, split at line breaks where possible. The trusted fields come
        first; each later part's first line names the version and the channel; and the last line, after
        the text, where nothing from the text can follow, names the version, the channel (its title
        escaped and clipped) and its chat_id, as the Allow once button does. The post text may imitate
        any line above it: the last line and the button are what the decision is made on."""
        text, version_id, channel = record["action"]["text"], record["version_id"], record["channel"]
        target = (f"{version_id} to [{channel}] {self.clip(record['channel_title'], 64)} "
                  f"(chat_id {record['action']['chat_id']}), {len(text)} characters")

        def head(label):
            return (f"Permission request {version_id}{label}\n"
                    f"Channel: [{record['channel']}] {self.one_line(record['channel_title'])}\n"
                    f"chat_id: {record['action']['chat_id']}\n"
                    f"Version: {version_id} (draft {record['draft_id']}, version {record['version']})\n"
                    f"Text: {text.count(chr(10)) + 1} line(s), {len(text)} characters, sent as plain text\n"
                    "----- BEGIN TEXT -----\n")

        def continued(label):
            return f"Permission request {version_id}{label} to [{channel}], text continued:\n"

        tail = f"\n----- END TEXT -----\n{ALLOW_MEANING}\nAllow once → {target}"
        widest = " (part 99 of 99)"
        budget = PART_LIMIT - max(units(head(widest)), units(continued(widest))) - units(tail)
        shown = [self.ex.visible(c) for c in text]  # per character: a split never cuts an escape
        chunks, start = [], 0
        while start < len(text):
            size, end, newline = 0, start, None
            while end < len(text) and size + units(shown[end]) <= budget:
                size += units(shown[end])
                end += 1
                if text[end - 1] == "\n":
                    newline = end
            if end < len(text) and newline:
                end = newline
            chunks.append("".join(shown[start:end]))
            start = end
        n = len(chunks)
        label = (lambda i: f" (part {i} of {n})") if n > 1 else (lambda i: "")
        return [(head(label(i)) if i == 1 else continued(label(i))) + chunk + (tail if i == n else "")
                for i, chunk in enumerate(chunks, 1)]

    def deliver_card(self, version_id):
        """Send the preview to the owner; the buttons ride on the last part, only once every part is
        there. Every preview goes out here (prepare's HTTP thread, Open in the bot's thread), one at a
        time, so the parts of two previews never interleave. A card counts only once it is stored: if
        a part fails, buttons that may have arrived do nothing, and the owner is told so. Returns
        (delivered, detail). A failure loses nothing: Pending requests opens it again."""
        with closing(self.ex.connect()) as db:
            owner = self.ex.owner_id(db)
            record = self.ex.fetch(db, version_id)
        if owner is None:
            return False, "no owner is bound to the bot yet (executor.py bind on the host)"
        parts = self.card_parts(record)
        with self.card_lock:
            message, problem = {}, None
            for number, part in enumerate(parts, 1):
                payload = {"chat_id": owner, "text": part, "link_preview_options": NO_LINK_PREVIEW,
                           **({"reply_markup": self.decide(record)} if number == len(parts) else {})}
                try:
                    message = self.ex.telegram_call("sendMessage", payload)
                except self.ex.Rejected as e:
                    problem = f"Telegram did not take part {number} of {len(parts)}: {e}"
                    break
            if problem is None and type(message.get("message_id")) is not int:
                problem = "Telegram did not report the preview's message_id"
            if problem:
                self.perform(self.say(owner, f"The preview of {version_id} to [{record['channel']}] did not arrive "
                                             "complete: its buttons, if they arrived, do nothing. Pending requests "
                                             "opens it again."))
                return False, problem
            with closing(self.ex.connect()) as db, self.ex.transaction(db):
                db.execute("INSERT OR REPLACE INTO cards (chat_id, message_id, version_id, sent_at) VALUES (?, ?, ?, ?)",
                           (owner, message["message_id"], version_id, self.ex.now()))
        return True, f"delivered to the owner in Telegram ({len(parts)} message{'s' if len(parts) > 1 else ''})"

    def notify_outcome(self, record, outcome, detail):
        """After a publication attempt: queue the owner's notice and return at once, so the answer to
        Hermes never waits for Telegram. The stored outcome is the truth, whatever becomes of it."""
        try:
            self.notices.put_nowait((record, outcome, detail))
        except queue.Full:
            self.ex.log(f"owner bot: outcome notice of {record['version_id']} dropped: "
                        f"{NOTICES_MAX} notices are waiting already")

    def run_notices(self):
        """The one thread that delivers the queued outcome notices, one at a time."""
        while True:
            notice = self.notices.get()
            try:
                self.tell_outcome(*notice)
            finally:
                self.notices.task_done()

    def tell_outcome(self, record, outcome, detail):
        """Tell the owner how a publication attempt ended. Never resends anything."""
        try:
            with closing(self.ex.connect()) as db:
                owner = self.ex.owner_id(db)
            if owner is None:
                return
            version_id = record["version_id"]
            where = f"[{record['channel']}] {self.one_line(record['channel_title'])}"
            text = {self.ex.PUBLISHED: f"Published {version_id} to {where}: {detail}.",
                    self.ex.OUTCOME_UNKNOWN: f"Outcome unknown: {version_id} to {where}: {detail}. The post may or "
                                             "may not be in the channel; check it. It will not be sent again.",
                    self.ex.FAILED: f"Failed: {version_id} to {where} was not published: {detail}. This "
                                    "permission is used; another publication needs a new version."}[outcome]
            self.perform(self.say(owner, text))
        except Exception as e:
            self.ex.log(f"owner bot: outcome notice failed ({type(e).__name__})")
