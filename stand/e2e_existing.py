#!/usr/bin/env python3
"""Acceptance of IntentRivet with an existing Hermes, deployed apart from the executor.

Nothing here assumes how either side is deployed; commands and addresses come from the environment:

  HERMES          command prefix that runs a command as Hermes' user where Hermes runs
                  (e.g. "docker exec -i -u hermes CONTAINER"; empty: this shell)
  HERMES_TTY      the same with a terminal, for the install's prompts (default: HERMES)
  HERMES_PROFILE  the existing profile to install into (empty: the default profile)
  PLUGIN_SOURCE   what `hermes plugins install` takes there (e.g. file:///path/IntentRivet#plugin)
  INTENTRIVET_URL the executor's API as Hermes reaches it
  EXECUTOR        command prefix that runs a command where the executor runs, a fresh one: no agent
                  key and no owner yet (e.g. "docker compose -p NAME exec -T executor")
  STAND_IN        command prefix of the Telegram stand-in's control
                  (e.g. "docker compose -p NAME exec -T telegram-mock python -I /app/telegram_mock.py")

It installs the plugin the standard way (hermes plugins install, answering its prompts), checks that
the profile's earlier settings are kept, binds an owner and connects two channels with random names
and ids through the publisher bot, then acts as the agent through Hermes' tool pipeline and terminal
tool (no model): preview, refusals, the owner's Allow once, one publication, the repeat refused, and
what the agent can reach. No real Telegram, no model. Exit code 0 only if every check passes.
"""

import json
import os
import pty
import random
import re
import select
import shlex
import subprocess
import sys
import time

ENV = {name: os.environ.get(name, "") for name in
       ("HERMES", "HERMES_TTY", "HERMES_PROFILE", "PLUGIN_SOURCE", "INTENTRIVET_URL", "EXECUTOR", "STAND_IN")}
missing = [name for name in ("PLUGIN_SOURCE", "INTENTRIVET_URL", "EXECUTOR", "STAND_IN") if not ENV[name]]
if missing:
    sys.exit(f"Set {', '.join(missing)}; see the docstring of {sys.argv[0]}.")
HERMES, HERMES_TTY = shlex.split(ENV["HERMES"]), shlex.split(ENV["HERMES_TTY"] or ENV["HERMES"])
PROFILE = ["-p", ENV["HERMES_PROFILE"]] if ENV["HERMES_PROFILE"] not in ("", "default") else []
OWNER, STRANGER = random.randint(10**8, 10**9), random.randint(10**8, 10**9)
CHANNELS = [(f"c{random.randrange(16**4):04x}", -100 * 10**10 - random.randint(10**9, 9 * 10**9),
             f"Channel {n} of this run") for n in (1, 2)]
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
failures = []


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        failures.append(what)


def run(prefix, *args, input=None):
    return subprocess.run([*prefix, *args], input=input, capture_output=True, text=True)


def hermes(*args):
    return run(HERMES, "hermes", *PROFILE, *args)


def executor(*args, input=None):
    return run(shlex.split(ENV["EXECUTOR"]), *args, input=input)


def stand_in(*args):
    return run(shlex.split(ENV["STAND_IN"]), *map(str, args)).stdout


def stats():
    return json.loads(stand_in("stats"))


def feed(*args):
    """Queue an update for the publisher bot and wait until the executor has read it."""
    stand_in(*args)
    end = time.time() + 30
    while stats()["queued"] and time.time() < end:
        time.sleep(0.2)


def said(to=None):
    sent = [m for m in stats()["private_log"] if m["method"] == "sendMessage" and m["chat_id"] == (to or OWNER)]
    return sent[-1]["text"] if sent else ""


def posts():
    return [p for p in stats()["posts"] if p["accepted"]]


# -- Hermes' own installation: where it is and its Python, from Hermes itself
install_dir = re.search(r"Install directory: (\S+)", run(HERMES, "hermes", "--version").stdout)
if not install_dir:
    sys.exit("Hermes did not report its install directory (hermes --version).")
ROOT = install_dir[1]
PYTHON = next((p for p in (f"{ROOT}/.venv/bin/python", f"{ROOT}/venv/bin/python")
               if run(HERMES, "test", "-x", p).returncode == 0), None) or sys.exit(f"No Python under {ROOT}.")
HOME = os.path.dirname(hermes("config", "path").stdout.strip().splitlines()[-1])
ENV_FILE = f"{HOME}/.env"
AGENT = ["env", f"HERMES_HOME={HOME}", f"PYTHONPATH={ROOT}", "HERMES_YOLO_MODE=1", PYTHON, "-c"]
TOOL_PY = r'''
import json, sys
from hermes_cli.env_loader import load_hermes_dotenv
load_hermes_dotenv()  # as Hermes does at start: the profile's .env, where get_secret finds the settings
from hermes_cli.plugins import discover_plugins
from model_tools import handle_function_call
discover_plugins()
print(handle_function_call(sys.argv[1], json.loads(sys.argv[2])))
'''
TERMINAL_PY = r'''
import json, os, sys, tempfile
from hermes_cli.plugins import discover_plugins
from model_tools import handle_function_call
discover_plugins()
fd, path = tempfile.mkstemp(suffix=".sh")  # a short command: Hermes refuses oversized inline payloads
with os.fdopen(fd, "w") as f:
    f.write(sys.argv[1])
try:
    result = json.loads(handle_function_call("terminal", {"command": f"sh {path}", "timeout": 240}))
finally:
    os.unlink(path)
print(result.get("output") or f"(no output; the terminal tool answered {result})")
'''


def tool(name, args):
    """The agent calls a tool through Hermes' tool pipeline in the profile, as the model would."""
    out = run(HERMES, *AGENT, TOOL_PY, name, json.dumps(args, ensure_ascii=False)).stdout
    return json.loads(out.strip().splitlines()[-1])


def agent_shell(script):
    """The agent runs a shell script with its own terminal tool, approvals bypassed."""
    return run(HERMES, *AGENT, TERMINAL_PY, script).stdout


def snapshot():
    """The profile's settings that must survive: config.yaml (parsed), .env lines, memories, persona
    and every other plugin's files. Values of .env lines are compared, never printed."""
    code = r'''
import hashlib, json, os, sys
sys.path.insert(0, sys.argv[2])
try:  # Hermes' own YAML reader (newer versions), else the PyYAML that older ones use
    from hermes_yaml import safe_load
except ImportError:
    from yaml import safe_load
home = sys.argv[1]
def digest(path):
    if os.path.isfile(path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    return {name: digest(os.path.join(path, name)) for name in sorted(os.listdir(path))} if os.path.isdir(path) else None
plugins = os.path.join(home, "plugins")
print(json.dumps({
    "config": safe_load(open(os.path.join(home, "config.yaml"))),
    "env": [hashlib.sha256(line.encode()).hexdigest() + " " + line.split("=")[0]
            for line in open(os.path.join(home, ".env")).read().splitlines()],
    "memories": digest(os.path.join(home, "memories")), "soul": digest(os.path.join(home, "SOUL.md")),
    "plugins": {name: digest(os.path.join(plugins, name)) for name in sorted(os.listdir(plugins))
                if name != "intentrivet" and not name.startswith(".")} if os.path.isdir(plugins) else {}}))
'''
    return json.loads(run(HERMES, PYTHON, "-c", code, HOME, ROOT).stdout)


def kept(before, after):
    """True if after is before plus IntentRivet only: enabled in config.yaml, with what Hermes itself
    writes when it enables a plugin (an empty plugins.disabled list and entries.intentrivet with
    allow_tool_override: false, when they were not there), and its two .env lines."""
    config, old = json.loads(json.dumps(after["config"])), before["config"]
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    old_plugins = old.get("plugins") if isinstance(old.get("plugins"), dict) else {}
    if "intentrivet" in (plugins.get("enabled") or []):
        plugins["enabled"].remove("intentrivet")
    entries = plugins.get("entries")
    if (isinstance(entries, dict) and entries.get("intentrivet") == {"allow_tool_override": False}
            and "intentrivet" not in (old_plugins.get("entries") or {})):
        del entries["intentrivet"]
    for name in ("enabled", "disabled", "entries"):
        if plugins.get(name) in ([], {}) and name not in old_plugins:
            del plugins[name]
    if config.get("plugins") == {} and "plugins" not in old:
        del config["plugins"]
    added = after["env"][len(before["env"]):]
    return (config == before["config"] and after["env"][:len(before["env"])] == before["env"]
            and sorted(line.split()[1] for line in added) == ["INTENTRIVET_AGENT_KEY", "INTENTRIVET_URL"]
            and all(after[k] == before[k] for k in ("memories", "soul", "plugins")))


def install(key):
    """hermes plugins install in a terminal, answering its prompts as a person would."""
    answers = [(rb"INTENTRIVET_URL:\s*$", ENV["INTENTRIVET_URL"].encode() + b"\r"),
               (rb"INTENTRIVET_AGENT_KEY:\s*$", key.encode() + b"\r"),
               (rb"Enable 'intentrivet' now\? \[y/N\]:\s*$", b"y\r")]
    master, slave = pty.openpty()
    process = subprocess.Popen([*HERMES_TTY, "hermes", *PROFILE, "plugins", "install", ENV["PLUGIN_SOURCE"]],
                               stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    raw, end = b"", time.time() + 180
    while time.time() < end:
        if select.select([master], [], [], 0.3)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            raw += chunk
            if b"\x1b[6n" in chunk:  # prompt_toolkit asks the terminal where the cursor is
                os.write(master, b"\x1b[1;1R")
        elif process.poll() is not None:
            break
        for pair in answers:
            if re.search(pair[0], ANSI.sub(b"", raw)):
                os.write(master, pair[1])
                answers.remove(pair)
                raw += b"\n"
                break
    if process.poll() is None:
        process.kill()
    os.close(master)
    return process.wait(), ANSI.sub(b"", raw).decode(errors="replace").replace("\r", ""), len(answers)


print(f"Hermes: {ROOT}, profile home {HOME}; executor API for Hermes: {ENV['INTENTRIVET_URL']}")
print("\n== The existing profile before IntentRivet")
before = snapshot()
check("intentrivet" not in json.dumps(before["config"]) and before["config"].get("model"),
      f"profile with its own settings (model {before['config'].get('model', {}).get('default')}, "
      f"{len(before['env'])} .env lines, plugins {sorted(before['plugins'])}), IntentRivet not installed")
stand_in("reset")
printed = executor("python", "-I", "/app/executor.py", "agent-key", input="").stdout
key = (re.search(r"^  ([A-Za-z0-9_-]{20,})$", printed, re.M) or [None, ""])[1]
check(bool(key), "the executor host issues an agent key (operator: executor.py agent-key)")

print("\n== Installed the standard way: hermes plugins install, answering its prompts")
code, shown, unanswered = install(key)
for line in shown.splitlines():
    if re.search(r"requires|Saved to|Enable|Installed|enabled|Error", line):
        print("   ", line.strip().replace(key, "<agent key>"))
check(code == 0 and unanswered == 0, "install asked for the address, the key and enabling, and finished")
check(key not in shown, "the agent key was not echoed at the prompt")
checked = hermes("intentrivet", "check")
print("   ", checked.stdout.strip().replace("\n", "\n    "))
check(checked.returncode == 0 and "it accepted the agent key" in checked.stdout,
      "hermes intentrivet check: connected, key accepted")
after = snapshot()
check(kept(before, after), "earlier settings kept: config.yaml, .env lines, memories, persona, other plugins")

print("\n== The owner binds the publisher bot and connects two channels of their own")
code_line = executor("python", "-I", "/app/executor.py", "bind").stdout
bind_code = (re.search(r"\?start=([A-Za-z0-9_-]+)", code_line) or [None, ""])[1]
feed("message", STRANGER, "/start not-the-code")
feed("message", OWNER, f"/start {bind_code}")
check(said().startswith("You are now the owner") and "not valid" in said(STRANGER),
      f"owner bound with the one-time link (random user id); a guessed code binds nobody")
for key_name, chat_id, title in CHANNELS:
    feed("message", OWNER, "Add channel")
    phrase = (re.search(r"Connect check \S+", said()) or [""])[0]
    feed("post", chat_id, title, phrase)
    feed("message", OWNER, key_name)
    check(said().startswith(f"Connected [{key_name}]: {title} (chat_id {chat_id})"), f"[{key_name}] connected")
listed = tool("intentrivet_list_channels", {})
check([(c["key"], c["chat_id"]) for c in listed.get("channels", [])] == [(k, c) for k, c, _ in CHANNELS],
      "the agent lists exactly these channels through Hermes' tool pipeline")

print("\n== Preview, the owner's permission, one publication")
(first, first_id, _), (second, _, _) = CHANNELS
text = f"Release notes {random.randrange(10**6)}\nSecond line: \u0442\u0435\u0441\u0442 🚀"
prepared = tool("intentrivet_prepare_post", {"channel": first, "text": text})
version = prepared.get("version_id", "")
print(f"    prepared {version} for [{first}]: {prepared.get('owner_preview', prepared.get('error'))}")
preview = [m for m in stats()["private_log"] if m["method"] == "sendMessage" and m["chat_id"] == OWNER][-2:]
check(prepared.get("ok") and prepared.get("status") == "Awaiting approval", "the version is stored, Awaiting approval")
check(any(text in m["text"] for m in preview) and any(m.get("reply_markup") for m in preview),
      "the owner got the exact text and the Allow once / Reject buttons")
refused = tool("intentrivet_publish_post", {"version_id": version, "channel": first})
check(not refused.get("ok") and "(403)" in refused.get("error", "") and not posts(), "refused without permission")
card = [m for m in stats()["private_log"] if m.get("reply_markup")][-1]["message_id"]
feed("press", OWNER, OWNER, card, "allow")
moved = tool("intentrivet_publish_post", {"version_id": version, "channel": second})
check(not moved.get("ok") and "(403)" in moved.get("error", "") and not posts(),
      f"allowed for [{first}], refused for [{second}]: no channel substitution")
published = tool("intentrivet_publish_post", {"version_id": version, "channel": first})
check(published.get("ok") and published.get("outcome") == "Published", f"published once: {published.get('detail')}")
check([(p["chat_id"], p["text"]) for p in posts()] == [(first_id, text)], "exactly one post, the stored text")
again = tool("intentrivet_publish_post", {"version_id": version, "channel": first})
check(not again.get("ok") and "(409)" in again.get("error", "") and len(posts()) == 1, "the repeat is refused (409)")

print("\n== What the agent itself can reach (its terminal, approvals bypassed)")
probe = agent_shell(f'''
e=$(sed -n 's/^INTENTRIVET_URL=//p' {shlex.quote(ENV_FILE)} | tail -1)
k=$(sed -n 's/^INTENTRIVET_AGENT_KEY=//p' {shlex.quote(ENV_FILE)} | tail -1)
c() {{ curl -s -o /dev/null -m 5 -w '%{{http_code}}' "$@" || echo unreachable; }}
echo "no_key=$(c $e/channels) wrong_key=$(c -H 'Authorization: Bearer x' $e/channels)"
for p in /versions/{version}/allow /permissions /channels /owner /bind /updates; do
  printf 'owner_op=%s ' "$(c -X POST -H "Authorization: Bearer $k" -d '{{}}' $e$p)"; done; echo
echo "change=$(c -X PUT -H "Authorization: Bearer $k" -d '{{}}' $e/versions/{version})"
echo "executor_files=$(find / \\( -path /proc -o -path /sys \\) -prune -o \\( -name 'intentrivet.sqlite3*' -o -name telegram_bot_token \\) -print 2>/dev/null | wc -l)"
n=0; for s in $(find / \\( -path /proc -o -path /sys \\) -prune -o -type s -print 2>/dev/null); do
  case $s in *docker*|*podman*|*containerd*) [ -w "$s" ] && n=$((n+1));; esac; done
echo "engine_sockets=$n engine_env=$(env | grep -c -E '^(DOCKER_HOST|CONTAINER_HOST)=')"
''')
print("   ", probe.strip().replace("\n", "\n    "))
check("no_key=401 wrong_key=401" in probe, "nothing is served without the agent key, or with another key")
check(probe.count("owner_op=404") == 6 and "change=501" in probe,
      "with the agent key: no operation to allow, bind, connect a channel or change a version")
check("executor_files=0" in probe and "engine_sockets=0 engine_env=0" in probe,
      "no executor file and no container-engine socket reachable from the agent")
search = r'''
import sys
token = open("/run/secrets/telegram_bot_token", "rb").read().strip()
found, size, tail = False, 0, b""
while token and (chunk := sys.stdin.buffer.read(1 << 20)):
    size += len(chunk)
    found = found or token in tail + chunk
    tail = (tail + chunk)[-len(token):]
print("found" if found else "absent", size)
'''
hermes_root = os.path.dirname(run(HERMES, "hermes", "config", "path").stdout.strip().splitlines()[-1])
source = subprocess.Popen([*HERMES, "sh", "-c", f"tar -cf - {shlex.quote(hermes_root)} 2>/dev/null; "
                           "cat /proc/[0-9]*/environ 2>/dev/null; true"], stdout=subprocess.PIPE)
found = subprocess.run([*shlex.split(ENV["EXECUTOR"]), "python", "-I", "-c", search], stdin=source.stdout,
                       capture_output=True, text=True).stdout.split()
source.wait()
check(found[:1] == ["absent"] and int(found[1]) > 0,
      f"the bot token is in none of Hermes' files or process environments ({found[1:2]} bytes searched)")
check(kept(before, snapshot()), "the profile's earlier settings are still unchanged at the end")

print(f"\nRESULT: {'all checks passed' if not failures else f'{len(failures)} check(s) failed'}")
sys.exit(1 if failures else 0)
