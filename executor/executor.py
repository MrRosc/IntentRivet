"""IntentRivet trusted executor: the owner allows each publication, once, in Telegram.

Runs in its own container, apart from Hermes. Hermes reaches it only over HTTP, with the
agent key (every request carries it), and can do four things: create a new immutable
version of a post, read a version by its id, list the connected channels, and ask to
publish a version. The key gives nothing else. Only the owner can allow a publication:
with Allow once on the version's preview in the publisher
bot's private chat (owner_bot.py), or on the host in this container's CLI; both call
allow() below. One permission is one send of exactly one stored version to its stored
chat_id. The executor marks the permission used, atomically and durably, before it
contacts Telegram, so a permission yields at most one sendMessage call to a channel,
whatever is retried, raced or restarted.

    python executor.py serve        # HTTP API on :8080 and the owner's Telegram interface
    python executor.py agent-key    # operator: a new agent key for Hermes; the previous one stops working
    python executor.py requests     # operator: Permission requests (interactive)
    python executor.py show ID      # read a record straight from the DB
    python executor.py check        # operator: read-only Telegram check, sends nothing
    python executor.py bind         # operator: a one-time link that makes its user the owner
    python executor.py reset-owner  # operator: remove the owner, keep everything else
"""

import hashlib
import http.client
import json
import os
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import unicodedata
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DB_PATH = os.environ.get("INTENTRIVET_DB", "/data/intentrivet.sqlite3")
CHANNELS_PATH = "/etc/intentrivet/channels.json"
TOKEN_PATH = "/run/secrets/telegram_bot_token"
TELEGRAM_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
PORT = 8080

AWAITING_APPROVAL = "Awaiting approval"
ALLOWED = "Allowed: one publication"
PUBLISHING = "Publishing"
PUBLISHED = "Published"
OUTCOME_UNKNOWN = "Outcome unknown"
FAILED = "Failed"
REJECTED = "Rejected"

CREATE_FIELDS = {"channel", "text", "draft_id"}
PUBLISH_FIELDS = {"version_id", "channel"}
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_TIMEOUT = 10
MAX_BODY = 64 * 1024
REQUEST_TIMEOUT = 10  # seconds per socket read/write; a stalled client is dropped
MAX_HANDLERS = 16     # HTTP connections served at a time (each may hold ~6.6 MB of headers); more get 503
BOT_TOKEN = ""        # loaded by serve(); never printed
BIND_TTL = int(os.environ.get("INTENTRIVET_BIND_TTL", "600"))  # seconds a one-time owner link works
BOT = None            # the owner's Telegram interface (owner_bot.OwnerBot), started by serve()

# versions is the stage 1 table, unchanged: a stage 1 store upgrades in place.
SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version_id    TEXT PRIMARY KEY,
    draft_id      TEXT NOT NULL,
    version       INTEGER NOT NULL,
    channel       TEXT NOT NULL,
    channel_title TEXT NOT NULL,
    action        TEXT NOT NULL,  -- the exact Telegram Bot API call, as JSON
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (draft_id, version)
);
-- At most one permission per version, ever: one publication.
CREATE TABLE IF NOT EXISTS permissions (
    version_id  TEXT PRIMARY KEY REFERENCES versions (version_id),
    chat_id     INTEGER NOT NULL,  -- the target the operator saw
    allowed_at  TEXT NOT NULL,
    used_at     TEXT,              -- set and committed before Telegram is contacted
    outcome     TEXT,              -- Publishing, Published, Outcome unknown or Failed
    detail      TEXT,
    finished_at TEXT
);
-- The connected channels: the only source of them (a stage 3-4 channels.json is imported once).
CREATE TABLE IF NOT EXISTS channels (
    key      TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    chat_id  INTEGER NOT NULL UNIQUE,
    added_at TEXT NOT NULL
);
-- A rejected version can never be allowed; a new decision needs a new version.
CREATE TABLE IF NOT EXISTS rejections (
    version_id  TEXT PRIMARY KEY REFERENCES versions (version_id),
    rejected_at TEXT NOT NULL
);
-- Preview cards in the owner's chat: the message that carries a version's decision buttons.
CREATE TABLE IF NOT EXISTS cards (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    version_id TEXT NOT NULL REFERENCES versions (version_id),
    sent_at    TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
-- owner_id, update_offset, bind_code (SHA-256 and expiry), add_channel (dialog), channels_imported,
-- agent_key (SHA-256 of the key Hermes sends).
CREATE TABLE IF NOT EXISTS settings (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Rejected(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Superseded(Exception):
    """The version shown to the operator is no longer the draft's latest."""

    def __init__(self, latest):
        super().__init__(latest)
        self.latest = latest


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message):
    print(message, flush=True)


def connect(read_only=False):
    if read_only:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, isolation_level=None)
    db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    db.executescript(SCHEMA)
    return db


@contextmanager
def transaction(db):
    """A write transaction that holds SQLite's write lock from its first statement. Inside another
    one (the owner's bot applies an update and its offset together), a savepoint of it."""
    if db.in_transaction:
        db.execute("SAVEPOINT inner")
        try:
            yield
        except BaseException:
            db.execute("ROLLBACK TO inner")
            db.execute("RELEASE inner")
            raise
        db.execute("RELEASE inner")
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def get_setting(db, name):
    row = db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def set_setting(db, name, value):
    """Store a setting; None removes it."""
    if value is None:
        db.execute("DELETE FROM settings WHERE name = ?", (name,))
    else:
        db.execute("INSERT INTO settings (name, value) VALUES (?, ?) "
                   "ON CONFLICT (name) DO UPDATE SET value = excluded.value", (name, str(value)))


def owner_id(db):
    value = get_setting(db, "owner_id")
    return int(value) if value else None


def channel_map(db):
    """The connected channels, {key: {"title", "chat_id"}}, in the order they were connected."""
    return {key: {"title": title, "chat_id": chat_id}
            for key, title, chat_id in db.execute("SELECT key, title, chat_id FROM channels ORDER BY rowid")}


def import_legacy_channels(db, path=CHANNELS_PATH):
    """Stages 3-4 kept the channels in a JSON file. It is imported once, into an empty channel table;
    from then on the store is the only source of channels. Returns a line for the log, or None."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    legacy = json.loads(text)["channels"] if text.strip() else {}
    for key, cfg in legacy.items():
        # An integer chat_id is what Telegram reports back, so the reply can be checked against it.
        if not (isinstance(cfg, dict) and isinstance(cfg.get("title"), str) and type(cfg.get("chat_id")) is int):
            raise SystemExit(f"{path}: channel {key!r} needs a string title and an integer chat_id.")
    if not legacy:
        return None
    with transaction(db):
        if get_setting(db, "channels_imported") or db.execute("SELECT 1 FROM channels").fetchone():
            current = channel_map(db)
            if any(current.get(k) != {"title": c["title"], "chat_id": c["chat_id"]} for k, c in legacy.items()):
                return f"{path} differs from the store and is ignored: the store is the only source of channels"
            return None
        try:
            for key, cfg in legacy.items():
                db.execute("INSERT INTO channels (key, title, chat_id, added_at) VALUES (?, ?, ?, ?)",
                           (key, cfg["title"], cfg["chat_id"], now()))
        except sqlite3.IntegrityError:
            raise SystemExit(f"{path}: two channels share a chat_id; nothing was imported.") from None
        set_setting(db, "channels_imported", f"{path}, {now()}")
    return f"Imported {len(legacy)} channel(s) from {path} into the store: {', '.join(legacy)}"


def fetch(db, version_id):
    row = db.execute("""
        SELECT v.version_id, v.draft_id, v.version, v.channel, v.channel_title, v.action,
               v.status, v.created_at, p.chat_id, p.allowed_at, p.used_at, p.outcome,
               p.detail, p.finished_at, r.rejected_at
        FROM versions v LEFT JOIN permissions p ON p.version_id = v.version_id
                        LEFT JOIN rejections r ON r.version_id = v.version_id
        WHERE v.version_id = ?""", (version_id,)).fetchone()
    if row is None:
        raise Rejected(404, f"No such version: {version_id}")
    (version_id, draft_id, version, channel, channel_title, action, status, created_at,
     chat_id, allowed_at, used_at, outcome, detail, finished_at, rejected_at) = row
    publication = None if allowed_at is None else {
        "chat_id": chat_id, "allowed_at": allowed_at, "used_at": used_at,
        "outcome": outcome, "detail": detail, "finished_at": finished_at}
    return {
        "version_id": version_id,
        "draft_id": draft_id,
        "version": version,
        "channel": channel,
        "channel_title": channel_title,
        "action": json.loads(action),
        "status": (REJECTED if rejected_at else status) if publication is None else (outcome or ALLOWED),
        "created_at": created_at,
        "publication": publication,
    }


def create_version(db, body):
    """Insert a new version. Never updates an existing row."""
    if not isinstance(body, dict):
        raise Rejected(400, "Request body must be a JSON object.")
    # Independent authority check: whatever the plugin did, only content
    # fields are accepted here. Approval state cannot be supplied by a client.
    unknown = sorted(set(body) - CREATE_FIELDS)
    if unknown:
        raise Rejected(400, f"Unknown field(s): {', '.join(unknown)}. "
                            "Only channel, text and draft_id are accepted; approval cannot be set here.")
    channel, text, draft_id = body.get("channel"), body.get("text"), body.get("draft_id")
    channels = channel_map(db)
    if not isinstance(channel, str) or channel not in channels:
        problem = "channel is required" if channel is None else f"Unknown channel: {channel!r}"
        if not channels:
            raise Rejected(400, f"{problem}. No channel is connected yet: the owner connects one with "
                                "Add channel in the publisher bot's private chat.")
        raise Rejected(400, f"{problem}. Connected channels: "
                            + ", ".join(f"{k} ({v['title']})" for k, v in channels.items())
                            + ". Call again with one of these keys.")
    if not isinstance(text, str) or not text.strip():
        raise Rejected(400, "text must be a non-empty string.")
    if len(text) > TELEGRAM_TEXT_LIMIT:
        raise Rejected(400, f"text is {len(text)} characters; Telegram allows {TELEGRAM_TEXT_LIMIT}.")
    # Type first: false, 0, [], {} and "" are errors, never "no draft_id" (only absent or null is).
    if draft_id is not None and not (isinstance(draft_id, str) and draft_id):
        raise Rejected(400, "draft_id must be a non-empty string; omit it for a new post.")

    with transaction(db):
        if draft_id is None:
            (count,) = db.execute("SELECT COUNT(DISTINCT draft_id) FROM versions").fetchone()
            draft_id, version = f"post-{count + 1}", 1
        else:
            (latest,) = db.execute("SELECT MAX(version) FROM versions WHERE draft_id = ?",
                                   (draft_id,)).fetchone()
            if latest is None:
                raise Rejected(404, f"No such draft: {draft_id!r}")
            version = latest + 1
        version_id = f"{draft_id}-v{version}"
        cfg = channels[channel]
        # No parse_mode: Telegram sends the text as plain text.
        action = {"method": "sendMessage", "chat_id": cfg["chat_id"], "text": text}
        db.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            version_id, draft_id, version, channel, cfg["title"],
            json.dumps(action, ensure_ascii=False), AWAITING_APPROVAL, now(),
        ))
    return fetch(db, version_id)


def pending_requests(db):
    """Latest version of each draft with no decision yet (no permission, not rejected), oldest first."""
    return [row[0] for row in db.execute("""
        SELECT v.version_id FROM versions v
        WHERE v.version = (SELECT MAX(w.version) FROM versions w WHERE w.draft_id = v.draft_id)
          AND NOT EXISTS (SELECT 1 FROM permissions p WHERE p.version_id = v.version_id)
          AND NOT EXISTS (SELECT 1 FROM rejections r WHERE r.version_id = v.version_id)
        ORDER BY v.rowid""")]


def allow(db, shown):
    """Owner only (the bot's Allow once, the operator CLI): allow one publication of exactly the
    version that was shown, to its stored chat_id."""
    with transaction(db):
        (latest,) = db.execute("SELECT version_id FROM versions WHERE draft_id = ? "
                               "ORDER BY version DESC LIMIT 1", (shown["draft_id"],)).fetchone()
        if latest != shown["version_id"]:
            raise Superseded(latest)
        if db.execute("SELECT 1 FROM permissions WHERE version_id = ?", (latest,)).fetchone():
            raise Rejected(409, f"{latest} already has its publication permission.")
        if db.execute("SELECT 1 FROM rejections WHERE version_id = ?", (latest,)).fetchone():
            raise Rejected(409, f"{latest} was rejected; a new decision needs a new version.")
        db.execute("INSERT INTO permissions (version_id, chat_id, allowed_at) VALUES (?, ?, ?)",
                   (latest, shown["action"]["chat_id"], now()))


def reject(db, version_id):
    """Owner only: refuse this version for good; the first decision stands."""
    with transaction(db):
        fetch(db, version_id)
        if db.execute("SELECT 1 FROM permissions WHERE version_id = ?", (version_id,)).fetchone():
            raise Rejected(409, f"{version_id} is already allowed; the decision stands.")
        if db.execute("SELECT 1 FROM rejections WHERE version_id = ?", (version_id,)).fetchone():
            raise Rejected(409, f"{version_id} was already rejected.")
        db.execute("INSERT INTO rejections (version_id, rejected_at) VALUES (?, ?)", (version_id, now()))


def claim(db, version_id, channel):
    """Use the permission for exactly this version and channel, or refuse without using it."""
    with transaction(db):
        record = fetch(db, version_id)
        permission = record["publication"]
        if permission is None:
            why = "was rejected by the owner" if record["status"] == REJECTED else "is not allowed"
            raise Rejected(403, f"A publication of {version_id} {why}. Only the owner allows one: Allow once "
                                "on its preview in the publisher bot. Nothing was sent.")
        if channel != record["channel"] or permission["chat_id"] != record["action"]["chat_id"]:
            raise Rejected(403, f"The publication allowed for {version_id} goes to channel "
                                f"{record['channel']!r}, not {channel!r}. Nothing was sent.")
        if permission["used_at"]:
            outcome = permission["outcome"]
            done = outcome + (f" ({permission['detail']})" if permission["detail"] else "")
            # What this request did (nothing), and the next step for this outcome: never a new version
            # before the outcome of this one is known.
            nxt = {PUBLISHING: "Its send is in progress: the outcome is stored when Telegram answers, and the "
                               "owner is told. Wait for it; do not prepare a new version of this post meanwhile.",
                   OUTCOME_UNKNOWN: "It is never sent again. The owner must check the channel first; only if the "
                                    "post is not there does another publication need a new version and a new "
                                    "permission.",
                   PUBLISHED: "The post is in the channel."}.get(
                outcome, "Nothing was published; another publication needs a new version and a new permission "
                         "from the owner.")
            raise Rejected(409, f"The one publication allowed for {version_id} was already used: {done}. "
                                f"This request sent nothing. {nxt}")
        db.execute("UPDATE permissions SET used_at = ?, outcome = ? WHERE version_id = ? AND used_at IS NULL",
                   (now(), PUBLISHING, version_id))
    return record


def telegram_connection(timeout=TELEGRAM_TIMEOUT):
    """A connection to the Bot API and the method path prefix. The prefix holds the token: never print it."""
    base = urlsplit(TELEGRAM_API)
    if base.scheme == "https":  # the certificate chain and the host name are verified
        connection = http.client.HTTPSConnection(base.hostname, base.port, timeout=timeout,
                                                 context=ssl.create_default_context())
    else:  # plain HTTP: the local stand-in only
        connection = http.client.HTTPConnection(base.hostname, base.port, timeout=timeout)
    return connection, f"{base.path.rstrip('/')}/bot{BOT_TOKEN}/"


def telegram_post(connection, prefix, method, payload):
    connection.request("POST", prefix + method, json.dumps(payload, ensure_ascii=False).encode(),
                       {"Content-Type": "application/json"})
    response = connection.getresponse()
    status, data = response.status, response.read()
    try:
        return status, json.loads(data)
    except (ValueError, RecursionError):
        return status, None


def telegram_call(method, payload, result_type=dict, timeout=TELEGRAM_TIMEOUT):
    """One Bot API call that is not a publication: checks, the owner's chat, getUpdates. Returns its
    result; raises Rejected on any failure, never with the request path (it holds the token)."""
    connection, prefix = telegram_connection(timeout)
    try:
        status, reply = telegram_post(connection, prefix, method, payload)
    except (OSError, http.client.HTTPException) as e:
        raise Rejected(502, f"{method}: Telegram not reachable ({type(e).__name__}: {e})") from None
    finally:
        connection.close()
    if isinstance(reply, dict) and reply.get("ok") is True and isinstance(reply.get("result"), result_type):
        return reply["result"]
    description = reply.get("description") if isinstance(reply, dict) else None
    raise Rejected(status, f"{method}: {description or f'HTTP {status}'}")


def post_link(chat, message_id):
    """t.me link to a channel post: public by username, else the /c/ form that works for members."""
    username = chat.get("username")
    if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
        return f"https://t.me/{username}/{message_id}"
    if str(chat["id"]).startswith("-100"):
        return f"https://t.me/c/{str(chat['id'])[4:]}/{message_id}"
    return None


def send_telegram(action):
    """At most one sendMessage call (none if the connection fails); never retried. Returns (outcome, detail)."""
    connection, prefix = telegram_connection()
    try:
        connection.connect()
    except OSError as e:
        return FAILED, f"Telegram not reachable, nothing was sent ({type(e).__name__})"
    try:  # from here on the request may have reached Telegram
        status, reply = telegram_post(connection, prefix, "sendMessage",
                                      {"chat_id": action["chat_id"], "text": action["text"]})
    except (OSError, http.client.HTTPException) as e:
        return OUTCOME_UNKNOWN, f"no response after the request was sent ({type(e).__name__})"
    finally:
        connection.close()
    if status == 200 and isinstance(reply, dict) and reply.get("ok") is True:
        message = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        message_id, chat = message.get("message_id"), message.get("chat")
        if not (type(message_id) is int and message_id > 0):
            return OUTCOME_UNKNOWN, "Telegram replied ok but without a valid message_id (HTTP 200)"
        # Published means: this chat, this exact text, as Telegram itself reports the new message.
        if not (isinstance(chat, dict) and chat.get("id") == action["chat_id"]
                and message.get("text") == action["text"]):
            return OUTCOME_UNKNOWN, (f"Telegram reported message_id {message_id}, but its chat or text "
                                     "differs from the stored snapshot")
        link = post_link(chat, message_id)
        return PUBLISHED, f"message_id {message_id}" + (f", {link}" if link else "")
    if 400 <= status < 500 and isinstance(reply, dict) and reply.get("ok") is False:
        return FAILED, f"Telegram refused: {reply.get('description', f'HTTP {status}')}"
    return OUTCOME_UNKNOWN, f"unexpected Telegram response (HTTP {status})"


def publish(db, body):
    if not isinstance(body, dict):
        raise Rejected(400, "Request body must be a JSON object.")
    unknown = sorted(set(body) - PUBLISH_FIELDS)
    if unknown:
        raise Rejected(400, f"Unknown field(s): {', '.join(unknown)}. Only version_id and channel are "
                            "accepted; the text and chat_id always come from the allowed snapshot.")
    version_id, channel = body.get("version_id"), body.get("channel")
    if not (isinstance(version_id, str) and version_id and isinstance(channel, str) and channel):
        raise Rejected(400, "version_id and channel must be non-empty strings.")
    record = claim(db, version_id, channel)  # committed: the permission is used from here on
    # Diagnostics: one line per attempt and its result; never the token or the post text.
    log(f"sendMessage attempt: {version_id} -> chat_id {record['action']['chat_id']}")
    try:
        outcome, detail = send_telegram(record["action"])
    except Exception as e:  # never leave a used permission Publishing, with the client cut off
        outcome, detail = OUTCOME_UNKNOWN, f"internal error during the send ({type(e).__name__})"
    with transaction(db):
        db.execute("UPDATE permissions SET outcome = ?, detail = ?, finished_at = ? WHERE version_id = ?",
                   (outcome, detail, now(), version_id))
    log(f"sendMessage result: {version_id}: {outcome}; {detail}")
    if BOT:  # queued for the owner: this answer never waits for that notice, which may fail or be late
        BOT.notify_outcome(record, outcome, detail)
    if outcome == PUBLISHED:
        return 200, {"version_id": version_id, "channel": channel, "chat_id": record["action"]["chat_id"],
                     "outcome": outcome, "detail": detail}
    if outcome == OUTCOME_UNKNOWN:
        raise Rejected(502, f"Outcome unknown: {detail}. The post may or may not be in the channel; "
                            "it will not be sent again. The owner must check the channel.")
    raise Rejected(502, f"Failed: {detail}. Nothing was published; this permission is used.")


def prepare_version(db, body):
    """Create a version, then deliver its preview to the owner. The reply says what really happened."""
    record = create_version(db, body)
    delivered, detail = (BOT.deliver_card(record["version_id"]) if BOT
                         else (False, "the owner's Telegram interface is not running"))
    return 201, {**record, "owner_preview": {"delivered": delivered, "detail": detail}}


def recover_interrupted(db):
    """A send in flight when the executor stopped may or may not have reached Telegram."""
    with transaction(db):
        return db.execute("UPDATE permissions SET outcome = ?, detail = ?, finished_at = ? WHERE outcome = ?",
                          (OUTCOME_UNKNOWN, "the executor stopped during the send", now(),
                           PUBLISHING)).rowcount


class Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT  # applied to the socket by StreamRequestHandler.setup()

    def reply(self, status, payload, headers=()):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for header in headers:
            self.send_header(*header)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        """Every request carries the agent key, 'Authorization: Bearer <key>', checked before anything
        else is read. It allows the four operations below and nothing more: no HTTP request can act
        for the owner, whatever key it carries."""
        with closing(connect(read_only=True)) as db:
            stored = get_setting(db, "agent_key")
        given = self.headers.get_all("Authorization") or []
        scheme, _, key = given[0].strip().partition(" ") if len(given) == 1 else ("", "", "")
        if not stored:
            problem = "No agent key is set on the executor yet: on the executor host run executor.py agent-key."
        elif scheme.lower() != "bearer" or not key.strip():
            problem = "Agent key missing: send it as 'Authorization: Bearer <key>' (INTENTRIVET_AGENT_KEY in Hermes)."
        elif not secrets.compare_digest(hashlib.sha256(key.strip().encode()).hexdigest(), stored):
            problem = "Agent key not accepted: it is wrong, or it was replaced with executor.py agent-key."
        else:
            return True
        self.reply(401, {"error": problem}, [("WWW-Authenticate", "Bearer")])
        return False

    def handle_request(self, work):
        try:
            with closing(connect()) as db:
                status, payload = work(db)
        except Rejected as e:
            status, payload = e.status, {"error": str(e)}
        self.reply(status, payload)

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == "/channels":  # read-only: the agent cannot connect, rename or remove a channel
            return self.handle_request(lambda db: (200, {"channels": [
                {"key": key, **cfg} for key, cfg in channel_map(db).items()]}))
        prefix = "/versions/"
        if not self.path.startswith(prefix) or "/" in self.path[len(prefix):]:
            return self.reply(404, {"error": f"Unknown operation: GET {self.path}"})
        self.handle_request(lambda db: (200, fetch(db, self.path[len(prefix):])))

    def do_POST(self):
        if not self.authorized():
            return
        operations = {
            "/versions": prepare_version,
            "/publications": publish,
        }
        if self.path not in operations:
            return self.reply(404, {"error": f"Unknown operation: POST {self.path}"})
        # Validate before reading anything: exactly one plain non-negative integer.
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0].strip()):
            return self.reply(400, {"error": "Content-Length must be a single non-negative integer."})
        digits = lengths[0].strip().lstrip("0") or "0"
        limit = str(MAX_BODY)
        if (len(digits), digits) > (len(limit), limit):
            return self.reply(413, {"error": "Request body too large."})
        length = int(digits)
        data = self.rfile.read(length)
        if len(data) != length:
            return self.reply(400, {"error": "Incomplete request body."})
        try:
            body = json.loads(data or b"null")
        except (ValueError, RecursionError):
            return self.reply(400, {"error": "Request body is not valid JSON."})
        try:  # a lone surrogate such as \ud800 is valid JSON, but cannot be stored or sent
            json.dumps(body, ensure_ascii=False).encode()
        except UnicodeEncodeError:
            return self.reply(400, {"error": "Request body contains an unpaired surrogate (such as \\ud800); "
                                             "strings must be valid Unicode text."})
        operation = operations[self.path]
        self.handle_request(lambda db: operation(db, body))


class BoundedServer(ThreadingHTTPServer):
    """One thread per connection, at most MAX_HANDLERS at a time: a connection beyond them gets 503 at
    once and is closed, before anything is read. Slow clients can still hold every slot (the API is
    then busy for Hermes too), but no longer threads and memory without bound."""
    BUSY = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
            b"Connection: close\r\n\r\n"
            b'{"error": "The executor is busy: too many connections at once. Nothing was done; try again."}')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(MAX_HANDLERS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(self.BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def load_token():
    """Read the bot token; report only a fingerprint."""
    try:
        token = Path(TOKEN_PATH).read_bytes().strip()
    except OSError:
        token = b""
    if not token:
        sys.exit(f"Telegram bot token not found at {TOKEN_PATH}; refusing to start.")
    if not re.fullmatch(rb"[A-Za-z0-9:_-]+", token):  # it goes into the request path
        sys.exit(f"The token at {TOKEN_PATH} has characters a bot token cannot have; refusing to start.")
    print(f"Telegram bot token loaded from {TOKEN_PATH}: {len(token)} bytes, "
          f"sha256 {hashlib.sha256(token).hexdigest()[:12]}...", flush=True)
    return token.decode()


def serve():
    global BOT_TOKEN, BOT
    BOT_TOKEN = load_token()
    with closing(connect()) as db:
        if note := import_legacy_channels(db):
            log(note)
        if interrupted := recover_interrupted(db):
            log(f"{interrupted} send(s) interrupted by a restart are now {OUTCOME_UNKNOWN!r}")
        channels, owner = channel_map(db), owner_id(db)
    sys.path.insert(0, str(Path(__file__).resolve().parent))  # owner_bot.py ships next to this file
    import owner_bot
    BOT = owner_bot.OwnerBot(sys.modules[__name__])
    threading.Thread(target=BOT.run, name="owner-bot", daemon=True).start()  # the only getUpdates reader
    threading.Thread(target=BOT.run_notices, name="owner-notices", daemon=True).start()  # outcome notices
    log(f"Listening on :{PORT}; channels: {', '.join(channels) or 'none yet'}; owner: "
        f"{'bound' if owner else 'not bound yet (executor.py bind)'}; Telegram API: {TELEGRAM_API}")
    BoundedServer(("0.0.0.0", PORT), Handler).serve_forever()


def visible(text):
    """Text for a terminal. Newlines stay line breaks; every other control, format, line- or
    paragraph-separator character (ESC, CR, backspace, C1, bidi overrides, zero-width marks, U+2028)
    is shown as its JSON escape, e.g. \\u001b or \\r, so none reaches the terminal raw. Spaces of
    any width stay as they are. The joiners U+200C and U+200D, which emoji and some scripts need,
    are kept. The stored text is never changed."""
    return "".join(c if c in "\n\u200c\u200d" or unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp")
                   else json.dumps(c)[1:-1] for c in text)


def render(record):
    action, text = record["action"], record["action"]["text"]
    lines = text.count("\n") + 1  # as displayed: only newlines break lines
    return visible("\n".join([
        f"Version:  {record['version_id']}  (draft {record['draft_id']}, version {record['version']})",
        f"Status:   {record['status']}",
        f"Channel:  {record['channel_title']}  [{record['channel']}]",
        f"chat_id:  {action['chat_id']}",
        "Format:   plain text (no parse_mode)",
        f"Text:     {lines} line(s), {len(text)} characters",
        "----- BEGIN TEXT -----",
        text,
        "----- END TEXT -----",
    ]))


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        return ""


def review(db, version_id):
    """Show one version and let the operator decide. Returns a newer version to open, or None."""
    shown = fetch(db, version_id)
    target = f"{shown['channel_title']} [{shown['channel']}]"
    print(f"\n{render(shown)}")
    if ask(f'Allow one publication of {version_id} to {target}?\n'
           'Type "allow" to confirm; anything else cancels: ') != "allow":
        print("Cancelled; nothing was allowed.")
        return None
    try:
        allow(db, shown)
    except Superseded as changed:
        print(f"Not allowed: {version_id} is no longer the latest version of draft {shown['draft_id']}; "
              f"{changed.latest} was created after it was shown.")
        return changed.latest if ask(f"Open {changed.latest}? [y/N]: ").lower() == "y" else None
    except Rejected as e:
        print(f"Not allowed: {e}")
        return None
    print(f"Allowed one publication of {version_id} to {target} (chat_id {shown['action']['chat_id']}).")
    return None


def requests():
    """Operator CLI: Permission requests."""
    # Against the real Telegram, a decision is typed by a person: refuse piped input. A guard
    # against scripted allows, not a security boundary (the host operator is trusted anyway).
    if urlsplit(TELEGRAM_API).scheme == "https" and not sys.stdin.isatty():
        sys.exit("Permission requests reach the real Telegram here: run them in an interactive terminal "
                 "(docker compose exec, without -T) and type the decision yourself.")
    with closing(connect()) as db:
        while True:
            pending = pending_requests(db)
            print("\nPermission requests")
            if not pending:
                print("  (none)")
            for number, version_id in enumerate(pending, 1):
                record = fetch(db, version_id)
                first_line = visible(record["action"]["text"].split("\n")[0][:60])
                print(f"  [{number}] {version_id} -> {record['channel_title']} [{record['channel']}]: {first_line}")
            unknown = [r[0] for r in db.execute("SELECT version_id FROM permissions WHERE outcome = ?",
                                                (OUTCOME_UNKNOWN,))]
            if unknown:
                print(f"{OUTCOME_UNKNOWN} (check the channel; these are never sent again): {', '.join(unknown)}")
            choice = ask("Select a request by number, or press Enter to quit: ")
            if not choice:
                return
            if not (choice.isdigit() and 1 <= int(choice) <= len(pending)):
                print("No such request.")
                continue
            version_id = pending[int(choice) - 1]
            while version_id:
                version_id = review(db, version_id)


def show(version_id):
    try:
        with closing(connect(read_only=True)) as db:
            # Still valid JSON: visible() only turns raw characters inside strings into JSON escapes.
            print(visible(json.dumps(fetch(db, version_id), ensure_ascii=False, indent=2)))
    except (Rejected, sqlite3.OperationalError) as e:
        sys.exit(str(e))


def check():
    """Operator: read-only Telegram check: getMe, getWebhookInfo, and getChat and getChatMember for
    each connected channel. Sends no message and reads no update."""
    global BOT_TOKEN
    BOT_TOKEN = load_token()
    try:
        with closing(connect(read_only=True)) as db:
            channels, owner = channel_map(db), owner_id(db)
    except sqlite3.OperationalError:  # no store yet: the executor has not run
        channels, owner = {}, None
    tls = "TLS, certificate and host name verified" if urlsplit(TELEGRAM_API).scheme == "https" else "plain HTTP"
    print(f"Telegram check: getMe, getWebhookInfo, getChat, getChatMember only; no message is sent.\n"
          f"Telegram API: {TELEGRAM_API} ({tls})")
    problems = []
    try:
        me = telegram_call("getMe", {})
        print(visible(f"Bot:      {me.get('first_name')} (@{me.get('username')}, id {me.get('id')})"))
        hook = telegram_call("getWebhookInfo", {})
        if hook.get("url"):
            problems.append(f"the bot has a webhook (host {urlsplit(str(hook['url'])).hostname}), so the executor "
                            "cannot poll it; it was left in place: remove it yourself (Bot API deleteWebhook) "
                            "if this bot is for IntentRivet only")
        else:
            print("Webhook:  none (the executor polls the bot's updates)")
        print(f"Owner:    {f'Telegram user id {owner}' if owner else 'not bound yet (executor.py bind)'}")
        if not channels:
            print("Channels: none yet (Add channel in the bot's private chat)")
        for key, cfg in channels.items():
            chat = telegram_call("getChat", {"chat_id": cfg["chat_id"]})
            member = telegram_call("getChatMember", {"chat_id": cfg["chat_id"], "user_id": me.get("id")})
            access = f"public @{chat['username']}" if chat.get("username") else "private"
            print(visible(f"Channel:  [{key}] {chat.get('title')}\n"
                          f"          chat_id {chat.get('id')}, type {chat.get('type')}, {access}\n"
                          f"          bot: {member.get('status')}, can_post_messages: {member.get('can_post_messages')}"))
            if chat.get("type") != "channel" or chat.get("id") != cfg["chat_id"]:
                problems.append(f"[{key}] chat_id {cfg['chat_id']} is not a channel Telegram reports by that id")
            if member.get("status") != "administrator" or member.get("can_post_messages") is not True:
                problems.append(f"[{key}] the bot is not an administrator allowed to post messages")
    except Rejected as e:
        problems.append(str(e))
    print(visible("RESULT: ready" if not problems else "RESULT: not ready: " + "; ".join(problems)))
    sys.exit(0 if not problems else 1)


def bind():
    """Operator, on the host: a one-time link to the bot's private chat. The account that opens it
    within BIND_TTL seconds and presses Start becomes the owner. It works once and only while no
    owner is bound; a new link replaces an unused one. The code is printed here only: the store
    keeps its SHA-256, and nothing logs it."""
    global BOT_TOKEN
    BOT_TOKEN = load_token()
    with closing(connect()) as db:
        if (owner := owner_id(db)) is not None:
            print(f"The bot already has an owner (Telegram user id {owner}); no link is issued. "
                  "To hand it to another account: executor.py reset-owner, then bind again.")
            return
        try:
            username = telegram_call("getMe", {}).get("username")
        except Rejected as e:
            sys.exit(f"Could not reach the bot: {e}")
        code = secrets.token_urlsafe(24)
        with transaction(db):
            set_setting(db, "bind_code", json.dumps({"sha256": hashlib.sha256(code.encode()).hexdigest(),
                                                     "expires": int(time.time()) + BIND_TTL}))
    print(f"Open this link in Telegram, signed in as the account that will own the bot, and press Start\n"
          f"within {BIND_TTL // 60} minutes. It works once; keep it to yourself until then.\n\n"
          f"  https://t.me/{username}?start={code}\n")


def agent_key():
    """Operator, on the executor host: a new agent key for Hermes, printed here only; the store keeps its
    SHA-256. The previous key stops working at once, so replacing one asks for a confirmation."""
    with closing(connect()) as db:
        if get_setting(db, "agent_key"):
            if not sys.stdin.isatty():
                sys.exit("An agent key is set already. To replace it, run this in an interactive terminal "
                         "(docker compose exec, without -T) and type the confirmation.")
            if ask('Replace the agent key? Hermes cannot reach the executor until it has the new one. '
                   'Type "replace" to confirm: ') != "replace":
                print("Cancelled; the agent key is unchanged.")
                return
        key = secrets.token_urlsafe(32)
        with transaction(db):
            set_setting(db, "agent_key", hashlib.sha256(key.encode()).hexdigest())
    print("New agent key. Give it to Hermes as INTENTRIVET_AGENT_KEY; it is shown only now, and a previous\n"
          f"key no longer works.\n\n  {key}\n")


def reset_owner():
    """Operator, on the host: remove the owner, an unused link and an Add channel in progress, so that
    executor.py bind can bind a new owner. Versions, permissions, outcomes and channels stay."""
    if not sys.stdin.isatty():
        sys.exit("Run this in an interactive terminal (docker compose exec, without -T) and type the confirmation.")
    with closing(connect()) as db:
        owner = owner_id(db)
        if owner is None:
            print("No owner is bound; nothing to reset.")
            return
        if ask(f'Remove owner {owner}? Versions, permissions and channels stay. Type "reset" to confirm: ') != "reset":
            print("Cancelled; nothing changed.")
            return
        with transaction(db):
            for name in ("owner_id", "bind_code", "add_channel"):
                set_setting(db, name, None)
    print("Owner removed; their buttons no longer work. Next: executor.py bind")


if __name__ == "__main__":
    command, *rest = sys.argv[1:] or ["serve"]
    if command == "serve" and not rest:
        serve()
    elif command == "requests" and not rest:
        requests()
    elif command == "show" and len(rest) == 1:
        show(rest[0])
    elif command == "check" and not rest:
        check()
    elif command == "bind" and not rest:
        bind()
    elif command == "agent-key" and not rest:
        agent_key()
    elif command == "reset-owner" and not rest:
        reset_owner()
    else:
        sys.exit(__doc__)
