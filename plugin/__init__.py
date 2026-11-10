"""IntentRivet plugin for Hermes Agent.

The plugin stores nothing and cannot approve. It forwards a post to the trusted
executor, which records an immutable snapshot (the exact Telegram call) and sends its
preview to the owner in Telegram; it can list the connected channels and ask the
executor to publish a version. The executor's address and the agent key come from the
Hermes profile's .env (INTENTRIVET_URL, INTENTRIVET_AGENT_KEY); `hermes intentrivet check`
tests them. Only the owner allows a publication (Allow once in the publisher bot); the
executor decides and sends the stored snapshot. Every preview is rendered from the
executor's record, never from the model's arguments.
"""

import http.client
import json
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

TOOL_NAME = "intentrivet_prepare_post"
TOOL_ARGS = {"channel", "text", "draft_id"}
PUBLISH_TOOL = "intentrivet_publish_post"
PUBLISH_ARGS = {"version_id", "channel"}
CHANNELS_TOOL = "intentrivet_list_channels"
USAGE = ("Usage: /intentrivet <version_id>, e.g. /intentrivet post-1-v2. "
         "Each prepared post's result gives the exact command.")
# The executor answers a publication only once Telegram has (its own limits: 10 s per step).
PUBLISH_TIMEOUT = 60

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Submit a Telegram post for the owner's approval. The trusted executor stores an "
        "immutable snapshot (publish action, exact text, chat_id), sends its exact preview to the "
        "owner in Telegram and returns its version id with status 'Awaiting approval' and whether "
        "the preview was delivered. The text is sent as plain text, exactly as given, with no "
        "Markdown or HTML formatting. To change the text or the channel of a post, call again with "
        "its draft_id: this creates a new version that needs its own approval; earlier versions are "
        "never modified. This tool cannot approve or send a post: the owner decides in Telegram "
        "(Allow once or Reject); publish only after they allowed it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Key of a connected channel, e.g. 'test' (intentrivet_list_channels lists "
                               "them). Needed; if it is missing or unknown, the error lists the keys.",
            },
            "text": {
                "type": "string",
                "description": "Full post text, verbatim. Line breaks are kept.",
            },
            "draft_id": {
                "type": "string",
                "description": "Only when revising: the draft_id returned for the post being changed.",
            },
        },
        # channel is checked by the executor, not here: a missing channel then gets the
        # executor's list of configured keys instead of a bare schema error from Hermes.
        "required": ["text"],
        "additionalProperties": False,
    },
}

PUBLISH_SCHEMA = {
    "name": PUBLISH_TOOL,
    "description": (
        "Publish one stored version to its Telegram channel. The trusted executor sends it only if "
        "the owner allowed exactly this version (Allow once), only to the channel it was prepared for, "
        "sends the stored text (never text from this call), and allows one send per permission: "
        "calling again never sends again. If the result is 'Outcome unknown', do not retry; tell the "
        "owner to check the channel."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "version_id": {"type": "string", "description": "Exact version id, e.g. post-1-v2."},
            "channel": {"type": "string", "description": "Channel key the version was prepared for, e.g. 'test'."},
        },
        "required": ["version_id", "channel"],
        "additionalProperties": False,
    },
}

CHANNELS_SCHEMA = {
    "name": CHANNELS_TOOL,
    "description": ("List the Telegram channels the owner connected: each one's key (use it as 'channel'), "
                    "title and chat_id. Read-only: only the owner connects channels, in the publisher bot."),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the executor never redirects; the key goes nowhere else
        return None


# The executor is on a loopback address, an SSH tunnel or a private network: never route it, or the
# agent key, through a proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _settings():
    """The executor's address and the agent key: plugin.yaml's requires_env, read on every call with
    Hermes' agent.secret_scope.get_secret, which honours the active profile (catalog rule 11). A tool
    runs in its turn's profile scope; /intentrivet in a gateway runs in the scope of the profile whose
    bot received it, in a CLI session in that profile's process; `hermes -p PROFILE intentrivet check`
    is that profile's own process, whose .env Hermes loaded at start, which get_secret reads."""
    from agent.secret_scope import get_secret
    url = (get_secret("INTENTRIVET_URL") or "").strip().rstrip("/")
    key = (get_secret("INTENTRIVET_AGENT_KEY") or "").strip()
    if not url:
        raise RuntimeError("INTENTRIVET_URL is not set in this Hermes profile's .env: set it to the executor's "
                           "address as this Hermes reaches it, e.g. http://127.0.0.1:8787.")
    if not url.startswith(("http://", "https://")):
        raise RuntimeError(f"INTENTRIVET_URL must start with http:// or https:// (it is {url!r}).")
    if not key:
        raise RuntimeError("INTENTRIVET_AGENT_KEY is not set in this Hermes profile's .env: on the executor "
                           "host, executor.py agent-key issues one.")
    return url, key


class _NoAnswer(RuntimeError):
    """The request was sent, but no complete answer came back: what the executor did is not known here."""


def _call(path, body=None, timeout=10):
    """One request to the executor. A request that changes something (it has a body: POST /versions,
    POST /publications) may have been acted on whenever the executor's own complete answer is missing,
    however the failure looks here (urllib also reports an error after the body went out as URLError):
    then it raises _NoAnswer. A read gets plain diagnostics."""
    base, key = _settings()
    url = base + path
    changes = body is not None
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                              "Authorization": f"Bearer {key}"})
    try:
        with _opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:  # the executor's own answer is complete JSON with an "error" text
            message = json.load(e)["error"]
            if not isinstance(message, str):
                raise TypeError("error is not a text")
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as problem:
            if changes:
                raise _NoAnswer(f"No complete answer from the executor at {url} (HTTP {e.code}, "
                                f"{type(problem).__name__})") from None
            message = f"HTTP {e.code}, without a complete error message"
        raise RuntimeError(f"Executor ({e.code}): {message}") from None
    except urllib.error.URLError as e:
        if changes:
            raise _NoAnswer(f"No confirmed answer from the executor at {url} ({e.reason})") from None
        raise RuntimeError(f"Executor unreachable at {url}: {e.reason}") from None
    except (OSError, http.client.HTTPException, ValueError) as e:  # sent; the answer timed out, broke or was cut
        raise _NoAnswer(f"No complete answer from the executor at {url} ({type(e).__name__}: {e})") from None


def _publication_lines(publication):
    if not publication:
        return []
    used = f"used {publication['used_at']}" if publication["used_at"] else "not used yet"
    detail = f"; {publication['detail']}" if publication["detail"] else ""
    return [f"Allowed:  one publication, {publication['allowed_at']}, {used}{detail}"]


def visible(text):
    """Text for a terminal or chat. Newlines stay line breaks; every other control, format, line- or
    paragraph-separator character (ESC, CR, backspace, C1, bidi overrides, zero-width marks, U+2028)
    is shown as its JSON escape, e.g. \\u001b or \\r; spaces of any width stay. The joiners U+200C
    and U+200D, which emoji and some scripts need, are kept. The same rule as the executor's CLI."""
    return "".join(c if c in "\n\u200c\u200d" or unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp")
                   else json.dumps(c)[1:-1] for c in text)


def render_preview(record):
    action = record["action"]
    text = action["text"]
    lines = text.count("\n") + 1  # as displayed: only newlines break lines
    return visible("\n".join([
        "IntentRivet: Telegram post (stored snapshot from the trusted executor)",
        f"Version:  {record['version_id']}  (draft {record['draft_id']}, version {record['version']})",
        f"Status:   {record['status']}",
        *_publication_lines(record.get("publication")),
        f"Channel:  {record['channel_title']}  [{record['channel']}]",
        f"chat_id:  {action['chat_id']}",
        "Format:   plain text (no parse_mode)",
        f"Text:     {lines} line(s), {len(text)} characters",
        "----- BEGIN TEXT -----",
        text,
        "----- END TEXT -----",
    ]))


def register(ctx):
    def prepare_post(args, **kwargs):
        extra = sorted(set(args) - TOOL_ARGS)
        if extra:
            return json.dumps({"ok": False, "error": (
                f"Unsupported argument(s): {', '.join(extra)}. This tool only submits a post "
                "for approval; it cannot approve or send.")})
        try:  # the executor delivers the preview before it answers: a long text takes several messages
            record = _call("/versions", args, timeout=90)
        except _NoAnswer as e:
            return json.dumps({"ok": False, "error": (
                f"{e}. The post may have been stored and its preview sent to the owner: ask the owner whether "
                "a preview arrived (Pending requests in the publisher bot lists it) before preparing it again.")})
        except RuntimeError as e:
            return json.dumps({"ok": False, "error": str(e)})
        preview = record.get("owner_preview") or {}
        return json.dumps({
            "ok": True,
            "version_id": record["version_id"],
            "draft_id": record["draft_id"],
            "status": record["status"],
            "owner_preview": ("Delivered to the owner in Telegram; awaiting their decision (Allow once or "
                              "Reject). Do not publish before they allow it." if preview.get("delivered") else
                              f"NOT delivered: {preview.get('detail', 'unknown')}. The version is stored; the "
                              "owner can open it from Pending requests in the bot."),
            "operator_command": f"/intentrivet {record['version_id']}",
            "preview": render_preview(record),
            # Hermes may show the model only this result, not the full schema.
            "to_revise": f"Call {TOOL_NAME} again with draft_id {record['draft_id']!r}, "
                         f"channel {record['channel']!r} and the full new text.",
        }, ensure_ascii=False)

    def publish_post(args, **kwargs):
        extra = sorted(set(args) - PUBLISH_ARGS)
        if extra:
            return json.dumps({"ok": False, "error": (
                f"Unsupported argument(s): {', '.join(extra)}. The executor sends the stored text "
                "to the stored channel; only version_id and channel are accepted.")})
        try:
            result = _call("/publications", args, timeout=PUBLISH_TIMEOUT)
        except _NoAnswer as e:
            version_id = args.get("version_id")
            return json.dumps({"ok": False, "outcome": "not known to this call", "error": (
                f"{e}. The publication of {version_id} may be in progress or done. Do not publish it again "
                "and do not prepare a new version until its outcome is known. The executor never sends a "
                "permission twice; it stores the outcome, the owner gets it in the publisher bot, and "
                f"/intentrivet {version_id} shows the stored status without sending anything.")},
                ensure_ascii=False)
        except RuntimeError as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        return json.dumps({"ok": True, **result,
                           "operator_command": f"/intentrivet {result['version_id']}"}, ensure_ascii=False)

    def list_channels(args, **kwargs):
        if args:
            return json.dumps({"ok": False, "error": "This tool takes no arguments and changes nothing; only "
                                                     "the owner connects channels, in the publisher bot."})
        try:
            channels = _call("/channels")["channels"]
        except RuntimeError as e:
            return json.dumps({"ok": False, "error": str(e)})
        return json.dumps({"ok": True, "channels": [
            {"key": c["key"], "title": visible(c["title"]), "chat_id": c["chat_id"]} for c in channels]},
            ensure_ascii=False)

    def show(raw_args):
        args = raw_args.split()
        if not args:
            return USAGE
        version_id = args[0]
        try:
            return render_preview(_call(f"/versions/{urllib.parse.quote(version_id, safe='')}"))
        except RuntimeError as e:
            return str(e)

    def check(args):
        """hermes intentrivet check: the executor's address and the agent key, tested with a read."""
        try:
            channels = _call("/channels")["channels"]
        except RuntimeError as e:
            print(f"IntentRivet: not connected. {e}")
            return 1
        print(f"IntentRivet: connected to the executor at {_settings()[0]}; it accepted the agent key.")
        print("Channels: " + (", ".join(f"[{c['key']}] {visible(c['title'])}" for c in channels) or
                              "none yet; the owner connects them with Add channel in the publisher bot."))
        return 0

    ctx.register_cli_command("intentrivet", help="IntentRivet: check the connection to the executor",
                             setup_fn=lambda parser: parser.add_argument("action", choices=["check"]),
                             handler_fn=check)
    ctx.register_tool(name=TOOL_NAME, toolset="intentrivet", schema=SCHEMA,
                      handler=prepare_post, emoji="📝")
    ctx.register_tool(name=PUBLISH_TOOL, toolset="intentrivet", schema=PUBLISH_SCHEMA,
                      handler=publish_post, emoji="📤")
    ctx.register_tool(name=CHANNELS_TOOL, toolset="intentrivet", schema=CHANNELS_SCHEMA,
                      handler=list_channels, emoji="📋")
    ctx.register_command("intentrivet", show, args_hint="<version_id>",
                         description="Show a stored IntentRivet post version")
