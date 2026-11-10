#!/usr/bin/env python3
"""End-to-end acceptance of the owner's path in Telegram, on a fresh stand with the stand-in.

Run from the repository root against a running stand-in stand started without legacy channels
(INTENTRIVET_LEGACY_CHANNELS=/dev/null) and with the plugin installed; no model, no real Telegram:

    python3 stand/e2e_owner.py

The owner and a stranger are Telegram users fed to the stand-in as updates; the executor's own
polling thread reads them. The agent acts through Hermes' tool pipeline and Hermes' terminal
tool. The stand-in counts channel posts apart from private messages; it also holds the owner's
outcome notice 11 s, longer than the plugin waits, and loses a preview, for Pending requests in
pages. Exit code 0 only if every expectation holds.
"""

import importlib.util
import re
import subprocess
import time
from pathlib import Path

from e2e_stage2 import AGENT_API, agent_shell, check, compose, failures, line, record, stand_in, stats, tool, wait_for_executor

OWNER, STRANGER = 7000001, 7000002
TEST_CHAT, MAIN_CHAT = -1000000007001, -1000000007002
def decide(version_id, channel):
    """The card's buttons: Allow once names the channel and the version it allows."""
    return {"inline_keyboard": [[{"text": f"Allow once → [{channel}] {version_id}", "callback_data": "allow"}],
                                [{"text": "Reject", "callback_data": "reject"}]]}


def decides(message):
    rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
    return [b.get("callback_data") for row in rows for b in row] == ["allow", "reject"]
# The agent prepares $1 posts for main from its own shell, straight through the executor's API.
MANY_SH = AGENT_API + r'''
i=1
while [ $i -le $1 ]; do
  api -s -m 60 -o /dev/null -w '%{http_code} ' -X POST -H 'Content-Type: application/json' \
    -d "{\"channel\": \"main\", \"text\": \"Queued request $i\"}" $e/versions
  i=$((i + 1))
done
'''
_spec = importlib.util.spec_from_file_location("intentrivet_plugin",
                                               Path(__file__).resolve().parent.parent / "plugin" / "__init__.py")
_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin)
visible = _plugin.visible  # the same safe display as the executor's


def settle(timeout=30):
    """Wait until the executor has confirmed every queued update; its replies go out before that."""
    end = time.time() + timeout
    while stats()["queued"] and time.time() < end:
        time.sleep(0.2)
    if stats()["queued"]:
        raise SystemExit("the executor did not read its updates")


def feed(*args):
    update_id = int(stand_in(*map(str, args)).strip())
    settle()
    return update_id


def private(to=None):
    return [m for m in stats()["private_log"] if m["method"] == "sendMessage" and to in (None, m["chat_id"])]


def said(to=OWNER):
    return private(to)[-1]["text"]


def told(start, timeout=20):
    """The owner's last message, once it starts with start (outcome notices go out on their own)."""
    end = time.time() + timeout
    while not said().startswith(start) and time.time() < end:
        time.sleep(0.2)
    return said()


def card():
    return [m for m in private(OWNER) if decides(m)][-1]["message_id"]


def answers():
    return [m["text"] for m in stats()["private_log"] if m["method"] == "answerCallbackQuery"]


def executor_py(*args, env=()):
    extra = [x for k in env for x in ("-e", k)]
    return compose("exec", "-T", *extra, "executor", "python", "-I", "/app/executor.py", *args).stdout


def owner():
    return compose("exec", "-T", "executor", "python", "-I", "-c",
                   "import sqlite3; db = sqlite3.connect('file:/data/intentrivet.sqlite3?mode=ro', uri=True); "
                   "r = db.execute(\"SELECT value FROM settings WHERE name = 'owner_id'\").fetchone(); "
                   "print(r[0] if r else '')").stdout.strip()


def permissions():
    return compose("exec", "-T", "executor", "python", "-I", "-c",
                   "import sqlite3; db = sqlite3.connect('file:/data/intentrivet.sqlite3?mode=ro', uri=True); "
                   "print(db.execute('SELECT COUNT(*) FROM permissions').fetchone()[0])").stdout.strip()


def link_code(printed):
    match = re.search(r"\?start=([A-Za-z0-9_-]+)", printed)
    return match[1] if match else ""


def binding():
    print("\n== Owner binding: one-time code from the host only")
    stand_in("reset")
    check(owner() == "", "a fresh store has no owner")
    feed("message", STRANGER, "/start some-guess")
    line("Stranger", "/start some-guess")
    line("Bot", said(STRANGER))
    check("not valid" in said(STRANGER) and owner() == "", "a guessed code binds nobody")
    expired = link_code(executor_py("bind", env=["INTENTRIVET_BIND_TTL=1"]))
    time.sleep(2)
    feed("message", OWNER, f"/start {expired}")
    line("Owner", "/start <expired code>")
    check("expired or was used already" in said(OWNER) and owner() == "", "an expired code binds nobody")
    printed = executor_py("bind")
    code = link_code(printed)
    line("Operator", "executor.py bind -> a link https://t.me/intentrivet_stand_bot?start=<code> (code not shown here)")
    check(bool(code) and code not in compose("logs", "--no-log-prefix", "executor").stdout,
          "the link is printed to the operator only, never into the executor's log")
    feed("message", OWNER, f"/start {code}")
    line("Owner", "/start <code>")
    line("Bot", said(OWNER).splitlines()[0])
    check(owner() == str(OWNER), "the owner is bound by their numeric Telegram user id")
    feed("message", STRANGER, f"/start {code}")
    line("Stranger", "/start <the same code>")
    line("Bot", said(STRANGER))
    check(owner() == str(OWNER) and said(STRANGER) == "This is a private bot.", "the used code binds nobody else")
    printed = executor_py("bind")
    line("Operator", printed.strip().splitlines()[-1])
    check("already has an owner" in printed and not link_code(printed), "no new link while an owner exists")


def connect(key, chat_id, title):
    feed("message", OWNER, "Add channel")
    phrase = re.search(r"Connect check \S+", said())[0]
    line("Owner", "Add channel")
    line("Bot", f"... post exactly: {phrase}")
    feed("post", chat_id, title, phrase)
    line("Channel", f"{title} ({chat_id}): {phrase}")
    found = said()
    line("Bot", " | ".join(found.splitlines()[:3]))
    listed = tool("intentrivet_list_channels", {})["channels"]
    check(f"Title: {title}\nchat_id: {chat_id}" in found and key not in [c["key"] for c in listed],
          f"the bot shows the channel it found and connects nothing before the owner confirms")
    feed("message", OWNER, key)
    line("Owner", key)
    line("Bot", said())
    check(said().startswith(f"Connected [{key}]: {title} (chat_id {chat_id})"), f"[{key}] connected after the owner named it")


def channels():
    print("\n== Two channels, connected from the owner's private chat")
    connect("test", TEST_CHAT, "Demo Test Channel")
    connect("main", MAIN_CHAT, "Demo Main Channel")
    feed("message", OWNER, "Add channel")
    phrase = re.search(r"Connect check \S+", said())[0]
    feed("post", TEST_CHAT, "Demo Test Channel", phrase)
    line("Owner", "Add channel, the phrase posted in the Test channel again")
    line("Bot", said())
    check("already connected as [test]" in said(), "a connected channel is not connected twice")
    listed = tool("intentrivet_list_channels", {})
    line("Agent", f"intentrivet_list_channels -> {[(c['key'], c['chat_id']) for c in listed['channels']]}")
    check([(c["key"], c["chat_id"]) for c in listed["channels"]] == [("test", TEST_CHAT), ("main", MAIN_CHAT)],
          "the agent reads the connected channels")


def flow():
    print("\n== Prepare through Hermes' tool pipeline; the full preview; Allow once; Main refused; one send")
    text = "\n".join(f"Demo line {i}: IntentRivet lets Hermes publish only what the owner allowed." for i in range(55))
    text = text[:4000] + "\n\u001b[31m (an escape) and 🚀"
    before = len(private(OWNER))
    result = tool("intentrivet_prepare_post", {"channel": "test", "text": text})
    v = result["version_id"]
    line("Agent", f"intentrivet_prepare_post for test -> {v}, {result['status']}")
    line("Executor", result["owner_preview"])
    parts = private(OWNER)[before:]
    body = parts[0]["text"].split("----- BEGIN TEXT -----\n", 1)[1] + "".join(
        m["text"].split("text continued:\n", 1)[1] for m in parts[1:])
    line("Bot", f"{len(parts)} preview messages; buttons on the last one only")
    check(result["owner_preview"].startswith("Delivered") and len(parts) > 1
          and all(m.get("reply_markup") is None for m in parts[:-1]) and parts[-1]["reply_markup"] == decide(v, "test")
          and parts[-1]["text"].rsplit("\n", 1)[-1].startswith(f"Allow once → {v} to [test] Demo Test Channel (chat_id {TEST_CHAT})")
          and body.split("\n----- END TEXT -----\n", 1)[0] == visible(text)
          and f"Channel: [test] Demo Test Channel\nchat_id: {TEST_CHAT}\n" in parts[0]["text"],
          "the owner gets the complete text, the channel, its chat_id and the version, in marked parts")
    feed("press", OWNER, OWNER, card(), "allow")
    line("Owner", "Allow once")
    line("Bot", said())
    s = stats()
    check(record(v)["status"] == "Allowed: one publication" and s["calls"] == 0, "Allow once publishes nothing")
    result = tool("intentrivet_publish_post", {"version_id": v, "channel": "main"})
    line("Agent", f"intentrivet_publish_post {v} to main")
    line("Executor", result["error"])
    test = subprocess.run(["stand/boundary-test.sh", v, "main"], capture_output=True, text=True)
    for out in test.stdout.strip().splitlines():
        line("Boundary", out)
    check("(403)" in result["error"] and test.returncode == 0 and stats()["calls"] == 0
          and record(v)["publication"]["used_at"] is None,
          "Main refused through the tool and from the agent's terminal, before sendMessage; the permission unused")
    stand_in("private", "delay", "11")  # the owner's notice takes 11 s; the plugin waits 10 s for its answer
    started = time.time()
    result = tool("intentrivet_publish_post", {"version_id": v, "channel": "test"})
    line("Agent", f"intentrivet_publish_post {v} to test, while Telegram holds the owner's notice 11 s")
    line("Executor", f"{result.get('outcome')}, {result.get('detail')} (tool result after {time.time() - started:.1f} s)")
    s = stats()
    check(result.get("outcome") == "Published" and (s["calls"], s["accepted"]) == (1, 1)
          and s["posts"][-1]["chat_id"] == TEST_CHAT and s["posts"][-1]["text"] == text,
          "published once: the stored text, to the Test chat_id; the answer did not wait for the owner's notice")
    line("Bot", told(f"Published {v}"))
    check(said().startswith(f"Published {v} to [test]"), "the owner's notice has the result and the post link")
    for when in ("now, while the notice is held", "after an executor restart"):
        if when.startswith("after"):
            end = time.time() + 30
            while "owner bot: send failed" not in (logs := compose("logs", "--no-log-prefix", "executor").stdout) \
                    and time.time() < end:
                time.sleep(0.5)
            line("Executor", ([x for x in logs.splitlines() if "owner bot: send failed" in x] or ["(no such line)"])[-1])
            check("owner bot: send failed: sendMessage: Telegram not reachable" in logs
                  and record(v)["status"] == "Published",
                  "the executor stopped waiting for the late notice; the stored outcome stays Published")
            stand_in("private", "ok")
            compose("restart", "executor")
            wait_for_executor()
        result = tool("intentrivet_publish_post", {"version_id": v, "channel": "test"})
        line("Agent", f"the same call again {when}")
        line("Executor", result["error"])
        check("(409)" in result["error"] and stats()["calls"] == 1, f"a repeat {when} sends nothing")


def decisions():
    print("\n== Reject, a stale card, a stranger's and a double press, a redelivered update")
    d1 = tool("intentrivet_prepare_post", {"channel": "test", "text": "Decision demo, first version."})
    card1 = card()
    feed("press", STRANGER, OWNER, card1, "allow")
    line("Stranger", f"Allow once on {d1['version_id']}")
    check(answers()[-1] == "Not allowed." and record(d1["version_id"])["publication"] is None, "a stranger's press does nothing")
    d2 = tool("intentrivet_prepare_post", {"channel": "test", "text": "Decision demo, second version.",
                                           "draft_id": d1["draft_id"]})
    card2 = card()
    feed("press", OWNER, OWNER, card1, "allow")
    line("Owner", f"Allow once on the stale card of {d1['version_id']}")
    line("Bot", said())
    check(f"{d1['version_id']} is no longer the latest version" in said()
          and record(d1["version_id"])["publication"] is None, "a stale card allows nothing and points to the new version")
    feed("press", OWNER, OWNER, card2, "reject")
    feed("press", OWNER, OWNER, card2, "allow")
    line("Owner", f"Reject, then Allow once on {d2['version_id']}")
    result = tool("intentrivet_publish_post", {"version_id": d2["version_id"], "channel": "test"})
    check(record(d2["version_id"])["status"] == "Rejected" and "was rejected" in result["error"],
          "Reject is final: Allow once afterwards changes nothing, the publication is refused")
    d3 = tool("intentrivet_prepare_post", {"channel": "test", "text": "Decision demo, a double press."})
    before = permissions()
    pressed = feed("press", OWNER, OWNER, card(), "allow")
    feed("press", OWNER, OWNER, card(), "allow")
    sent = len(stats()["private_log"])
    stand_in("redeliver", str(pressed))
    settle()
    line("Owner", f"Allow once twice on {d3['version_id']}; Telegram delivers the first press again")
    check(int(permissions()) == int(before) + 1 and len(stats()["private_log"]) == sent
          and record(d3["version_id"])["status"] == "Allowed: one publication",
          "one permission; the redelivered update changes nothing")
    check(stats()["calls"] == 1, "still exactly one channel post")


def lost_reply():
    print("\n== Outcome unknown stays unknown and is never resent")
    stand_in("mode", "drop")
    v = tool("intentrivet_prepare_post", {"channel": "test", "text": "Lost reply demo."})["version_id"]
    feed("press", OWNER, OWNER, card(), "allow")
    result = tool("intentrivet_publish_post", {"version_id": v, "channel": "test"})
    line("Executor", result["error"])
    line("Bot", told(f"Outcome unknown: {v}"))
    again = tool("intentrivet_publish_post", {"version_id": v, "channel": "test"})
    check("Outcome unknown" in result["error"] and said().startswith(f"Outcome unknown: {v}")
          and "(409)" in again["error"] and stats()["calls"] == 2, "Outcome unknown: told to the owner, one call only")
    stand_in("mode", "ok")
    attempts = compose("logs", "--no-log-prefix", "executor").stdout.count("sendMessage attempt: ")
    line("Executor", f"{attempts} sendMessage attempt line(s) for channels; the stand-in got "
                     f"{stats()['calls']} channel post(s) and {stats()['private']} private message(s)")
    check(attempts == 2, "the executor's attempt lines count channel posts only, not messages to the owner")


def pending_pages():
    print("\n== Pending requests in pages; a late request whose preview was not delivered, opened and decided")
    codes = agent_shell(MANY_SH, "24").split()
    line("Agent", f"24 posts for main from its own shell: HTTP {' '.join(sorted(set(codes)))}")
    stand_in("private", "fail")
    late = tool("intentrivet_prepare_post", {"channel": "test", "text": "A late request; its preview is lost."})
    stand_in("private", "ok")
    line("Agent", f"intentrivet_prepare_post for test -> {late['version_id']}")
    line("Executor", late["owner_preview"])
    feed("message", OWNER, "Pending requests")
    pages, listed = [], []
    while True:
        shown = private(OWNER)[-1]
        keyboard = shown["reply_markup"]["inline_keyboard"]
        pages.append(shown["text"])
        line("Bot", f"{shown['text'].splitlines()[0]} [{' | '.join(b['text'] for row in keyboard for b in row)}]")
        listed += [b["callback_data"][len("open:"):] for row in keyboard for b in row
                   if b["callback_data"].startswith("open:")]
        turn = [b["callback_data"] for row in keyboard for b in row if b["text"] == "Next"]
        if not turn:
            break
        feed("press", OWNER, OWNER, shown["message_id"], turn[0])
        line("Owner", "Next")
    check(codes == ["201"] * 24 and late["owner_preview"].startswith("NOT delivered") and len(pages) == 3
          and len(set(listed)) == len(listed) == 25 and listed[-1] == late["version_id"]
          and max(len(p.encode("utf-16-le")) // 2 for p in pages) <= 4096,
          "25 pending requests in 3 messages within Telegram's limit, each one with its Open button")
    feed("press", OWNER, OWNER, shown["message_id"], f"open:{late['version_id']}")
    line("Owner", f"Open {late['version_id']}, on the last page")
    feed("press", OWNER, OWNER, card(), "allow")
    line("Owner", "Allow once")
    line("Bot", said())
    check(record(late["version_id"])["status"] == "Allowed: one publication"
          and record(listed[0])["status"] == "Awaiting approval" and stats()["calls"] == 2,
          "the late request opened and allowed in Telegram, the 24 earlier ones untouched; nothing sent")


if __name__ == "__main__":
    if "telegram-mock" not in compose("config", "--services").stdout.split():
        raise SystemExit("e2e_owner.py runs only on the stand with the Telegram stand-in (compose.yaml).")
    binding()
    channels()
    flow()
    decisions()
    lost_reply()
    pending_pages()
    if failures:
        print(f"\nRESULT: {len(failures)} expectation(s) failed")
        raise SystemExit(1)
    print("\nRESULT: every expectation holds")
