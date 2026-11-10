"""Test-only stand-in for the Telegram Bot API. Never the real Telegram.

It counts every sendMessage call to a channel it receives and every post it accepts, and
keeps every message to a private chat apart, in its own volume, so the numbers survive
executor restarts. It also queues the updates a test feeds it for the executor's getUpdates.
Control it with docker compose exec:

    python telegram_mock.py serve                # :8081
    python telegram_mock.py stats                # {"calls": N, "accepted": M, "posts": [...],   (channels only)
                                                 #  "private": K, "private_log": [...], "queued": U, "mode": ...}
    python telegram_mock.py mode ok|drop|hold    # channel posts. drop: accept the post, then cut the
                                                 # connection before replying; hold: accept it, never reply
    python telegram_mock.py mode no-result|null-result
                                                 # accept the post, reply HTTP 200 {"ok": true}
                                                 # or {"ok": true, "result": null}
    python telegram_mock.py mode altered         # accept the post, report another text back
    python telegram_mock.py private ok|fail      # fail: refuse messages to private chats (403)
    python telegram_mock.py private delay SECONDS
                                                 # take messages to private chats, answer SECONDS late
    python telegram_mock.py webhook URL|none     # a webhook makes getUpdates fail with 409, as Telegram does
    python telegram_mock.py message USER TEXT    # queue a private message from user id USER
    python telegram_mock.py press USER CHAT MESSAGE DATA
                                                 # queue a button press by USER on message MESSAGE in chat CHAT
    python telegram_mock.py post CHAT TITLE TEXT # queue a post in channel CHAT (its title for getChat too)
    python telegram_mock.py redeliver UPDATE_ID  # hand out a delivered update once more, whatever the offset
    python telegram_mock.py reset                # forget everything, back to mode ok

A post is accepted when the bot token and the payload are valid. getMe, getChat and
getChatMember answer for a synthetic bot that may post in any channel; they are not counted.
Request lines are not logged: the token is part of the path.
"""

import hmac
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = Path(os.environ.get("TELEGRAM_MOCK_STATE", "/state"))
TOKEN_PATH = "/run/secrets/telegram_bot_token"
PORT = 8081
MODES = ("ok", "drop", "hold", "no-result", "null-result", "altered")
BOT = {"id": 1000001, "is_bot": True, "first_name": "IntentRivet stand-in", "username": "intentrivet_stand_bot"}
LOCK = threading.Lock()  # one writer for the state files
FILES = ("calls.jsonl", "mode", "private.jsonl", "private_mode", "updates.jsonl", "redeliver.jsonl",
         "delivered.jsonl", "titles.json", "webhook", "next_update")


def read_lines(name):
    try:  # split on "\n" only: a text may hold U+2028 or NEL, which JSON leaves raw
        text = (STATE / name).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.split("\n") if line]
    except FileNotFoundError:
        return []


def write_lines(name, rows):
    (STATE / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def append(name, row):
    with open(STATE / name, "a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")


def setting(name, default):
    try:
        return (STATE / name).read_text(encoding="utf-8").strip() or default
    except FileNotFoundError:
        return default


def titles():
    try:
        return json.loads((STATE / "titles.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def calls():
    return read_lines("calls.jsonl")


def current_mode():
    return setting("mode", "ok")


def queue(update):
    """Add an update with the next update_id; also kept for redeliver."""
    with LOCK:
        update_id = int(setting("next_update", "1"))
        (STATE / "next_update").write_text(str(update_id + 1))
        update = {"update_id": update_id, **update}
        append("updates.jsonl", update)
        append("delivered.jsonl", update)
    return update_id


def take_updates(offset):
    """Telegram's semantics: updates below the offset are confirmed and gone."""
    with LOCK:
        pending = [u for u in read_lines("updates.jsonl") if u["update_id"] >= offset]
        write_lines("updates.jsonl", pending)
        again = read_lines("redeliver.jsonl")
        write_lines("redeliver.jsonl", [])
    return again + pending[:100]


def reads(method, p):
    """Synthetic answers for the executor's checks and the owner's Add channel."""
    if method == "getMe":
        return BOT
    if method == "getChat":
        title = titles().get(str(p.get("chat_id")), f"Stand-in channel {p.get('chat_id')}")
        return {"id": p.get("chat_id"), "type": "channel", "title": title}
    if method == "getChatMember":
        return {"status": "administrator", "can_post_messages": True, "user": BOT}
    return {"url": setting("webhook", ""), "pending_update_count": 0}  # getWebhookInfo


class Handler(BaseHTTPRequestHandler):
    token = ""

    def reply(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        match = re.fullmatch(r"/bot([^/]*)/(sendMessage|getMe|getChat|getChatMember|getWebhookInfo|getUpdates|"
                             r"editMessageReplyMarkup|answerCallbackQuery)", self.path)
        if not match:
            return self.reply(404, {"ok": False, "error_code": 404, "description": "Not Found"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError:
            payload = None
        p = payload if isinstance(payload, dict) else {}
        token_ok = hmac.compare_digest(match[1].encode(), self.token.encode())
        method = match[2]
        if not token_ok and method != "sendMessage":
            return self.reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        if method in ("getMe", "getChat", "getChatMember", "getWebhookInfo"):
            return self.reply(200, {"ok": True, "result": reads(method, p)})
        if method == "getUpdates":
            if setting("webhook", ""):
                return self.reply(409, {"ok": False, "error_code": 409, "description":
                                        "Conflict: can't use getUpdates method while webhook is active; "
                                        "use deleteWebhook to delete the webhook first"})
            end = time.time() + min(int(p.get("timeout") or 0), 30)
            while not (updates := take_updates(int(p.get("offset") or 0))) and time.time() < end:
                time.sleep(0.1)
            return self.reply(200, {"ok": True, "result": updates})
        if method in ("editMessageReplyMarkup", "answerCallbackQuery"):
            with LOCK:
                append("private.jsonl", {"method": method, **p})
            return self.reply(200, {"ok": True, "result": True})
        if isinstance(p.get("chat_id"), int) and p["chat_id"] > 0 and token_ok:
            return self.private_message(p)
        self.channel_post(payload, token_ok)

    def private_message(self, p):
        """A message to a person (the owner, or someone the bot answers): never a publication."""
        with LOCK:
            sent = [m for m in read_lines("private.jsonl") if m.get("method") == "sendMessage"]
            mode = setting("private_mode", "ok")
            ok = mode != "fail" and isinstance(p.get("text"), str)
            message_id = 500 + len(sent) + 1
            append("private.jsonl", {"method": "sendMessage", "delivered": ok, "message_id": message_id if ok else None,
                                     "chat_id": p["chat_id"], "text": p.get("text"),
                                     "reply_markup": p.get("reply_markup")})
        if not ok:
            return self.reply(403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"})
        if mode.startswith("delay "):
            time.sleep(int(mode.split()[1]))
        try:
            self.reply(200, {"ok": True, "result": {"message_id": message_id, "text": p["text"],
                                                    "chat": {"id": p["chat_id"], "type": "private"}}})
        except OSError:  # the caller stopped waiting
            pass

    def channel_post(self, payload, token_ok):
        valid = (isinstance(payload, dict) and isinstance(payload.get("chat_id"), int)
                 and isinstance(payload.get("text"), str))
        mode = current_mode()
        with LOCK:
            log = calls()
            record = {"call": len(log) + 1, "accepted": token_ok and valid, "mode": mode,
                      "chat_id": payload.get("chat_id") if valid else None,
                      "text": payload.get("text") if valid else None}
            append("calls.jsonl", record)
            message_id = sum(c["accepted"] for c in log) + record["accepted"]
        if not token_ok:
            return self.reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        if not valid:
            return self.reply(400, {"ok": False, "error_code": 400, "description": "Bad Request"})
        if mode == "hold":
            time.sleep(60)
        if mode in ("drop", "hold"):  # the post is accepted; the reply never leaves
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        message = {"message_id": message_id, "chat": {"id": record["chat_id"], "type": "channel"},
                   "text": record["text"]}
        self.reply(200, {"ok": {"ok": True, "result": message},
                         "no-result": {"ok": True},  # the post is accepted; the reply has no message
                         "null-result": {"ok": True, "result": None},
                         "altered": {"ok": True, "result": {**message, "text": "Another text"}}}[mode])

    def log_message(self, *args):
        pass


def person(user_id):
    return {"id": user_id, "is_bot": False, "first_name": f"User {user_id}"}


def main(command="serve", *args):
    if command == "serve":
        Handler.token = Path(TOKEN_PATH).read_text().strip()
        if not Handler.token:
            sys.exit("No bot token; refusing to start.")
        print(f"Telegram stand-in listening on :{PORT} (mode {current_mode()})", flush=True)
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    elif command == "stats":
        log, private = calls(), read_lines("private.jsonl")
        print(json.dumps({"calls": len(log), "accepted": sum(c["accepted"] for c in log), "mode": current_mode(),
                          "posts": log, "private": sum(m.get("method") == "sendMessage" for m in private),
                          "private_log": private,  # queued: updates the executor has not confirmed yet
                          "queued": len(read_lines("updates.jsonl")) + len(read_lines("redeliver.jsonl"))},
                         ensure_ascii=False))
    elif command == "mode" and args and args[0] in MODES:
        (STATE / "mode").write_text(args[0])
    elif command == "private" and len(args) == 1 and args[0] in ("ok", "fail"):
        (STATE / "private_mode").write_text(args[0])
    elif command == "private" and len(args) == 2 and args[0] == "delay" and re.fullmatch(r"[0-9]{1,3}", args[1]):
        (STATE / "private_mode").write_text(f"delay {int(args[1])}")
    elif command == "webhook" and len(args) == 1:
        (STATE / "webhook").write_text("" if args[0] == "none" else args[0])
    elif command == "message" and len(args) == 2:
        user = int(args[0])
        print(queue({"message": {"message_id": int(time.time()), "date": int(time.time()), "from": person(user),
                                 "chat": {"id": user, "type": "private"}, "text": args[1]}}))
    elif command == "press" and len(args) == 4:
        user, chat, message_id = map(int, args[:3])
        print(queue({"callback_query": {"id": f"cb{time.time_ns()}", "from": person(user), "data": args[3],
                                        "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}}}}))
    elif command == "post" and len(args) == 3:
        chat = int(args[0])
        with LOCK:
            known = titles()
            known[str(chat)] = args[1]
            (STATE / "titles.json").write_text(json.dumps(known, ensure_ascii=False))
        print(queue({"channel_post": {"message_id": int(time.time()), "date": int(time.time()), "text": args[2],
                                      "chat": {"id": chat, "type": "channel", "title": args[1]}}}))
    elif command == "redeliver" and len(args) == 1:
        with LOCK:
            again = [u for u in read_lines("delivered.jsonl") if u["update_id"] == int(args[0])]
            for update in again:
                append("redeliver.jsonl", update)
        print(len(again))
    elif command == "reset":
        for name in FILES:
            (STATE / name).unlink(missing_ok=True)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(*sys.argv[1:])
