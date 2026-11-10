#!/usr/bin/env python3
"""Stage 2 end-to-end acceptance: one operator-allowed publication, enforced by the executor.

Run from the repository root against a running stand with the plugin installed
(no model, no real Telegram):

    python3 stand/e2e_stage2.py

The agent acts through Hermes' tool pipeline (stand/agent-call.sh) and, for direct
attempts, through Hermes' own terminal tool; the operator is the executor's CLI, driven
over stdin; the Telegram stand-in counts calls and accepted posts. One report per case;
exit code 0 only if every expectation holds.
"""

import codecs
import json
import os
import re
import subprocess
import threading
import time

COMPOSE = ["docker", "compose"]
TYPE_ALLOW = r'Type "allow" to confirm; anything else cancels: '
SELECT = r"Select a request by number, or press Enter to quit: "
failures = []

# Runs a shell script from stdin through Hermes' terminal tool, as the agent's own shell. The script
# first reads the executor's address and the agent key from the profile's .env, as the agent can.
TERMINAL_PY = r'''
import json, os, shlex, sys, tempfile
from hermes_cli.plugins import discover_plugins
from model_tools import handle_function_call
discover_plugins()
fd, path = tempfile.mkstemp(suffix=".sh")
with os.fdopen(fd, "w") as f:
    f.write(f"HERMES_HOME={shlex.quote(os.environ['HERMES_HOME'])}\n" + sys.stdin.read())
try:
    command = " ".join(["sh", path, *map(shlex.quote, sys.argv[1:])])
    print(handle_function_call("terminal", {"command": command, "timeout": 120}))
finally:
    os.unlink(path)
'''
AGENT_API = r'''
e=$(sed -n 's/^INTENTRIVET_URL=//p' "$HERMES_HOME/.env" | tail -1)
k=$(sed -n 's/^INTENTRIVET_AGENT_KEY=//p' "$HERMES_HOME/.env" | tail -1)
api() { curl -H "Authorization: Bearer $k" "$@"; }
'''
POST_SH = AGENT_API + r'''
api -s -m 30 -o /tmp/reply.$$ -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d "$1" $e/publications
printf ' '; cat /tmp/reply.$$; rm -f /tmp/reply.$$; echo
'''
CONCURRENT_SH = AGENT_API + r'''
i=0
while [ $i -lt $2 ]; do
  api -s -m 30 -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
    -d "$1" $e/publications > /tmp/attempt.$$.$i &
  i=$((i + 1))
done
wait; cat /tmp/attempt.$$.*; rm -f /tmp/attempt.$$.*
'''
APPROVE_SH = AGENT_API + r'''
for path in /versions/$1/allow /versions/$1/approve /permissions /approvals; do
  echo "POST $path -> $(api -s -o /dev/null -w '%{http_code}' -X POST -d "{\"version_id\":\"$1\"}" $e$path)"
done
echo "executor database files reachable: $(find / \( -path /proc -o -path /sys \) -prune -o -name 'intentrivet.sqlite3*' -print 2>/dev/null | wc -l)"
docker version >/dev/null 2>&1 && echo "container engine server: REACHABLE" || echo "container engine server: unreachable"
'''


def compose(*args, input=None):
    return subprocess.run([*COMPOSE, *args], input=input, capture_output=True, text=True)


def line(role, text):
    print(f"  {role:<9}: {text}")


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}       {what}")
    if not ok:
        failures.append(what)


def tool(name, args):
    """The agent calls an IntentRivet tool through Hermes' tool pipeline."""
    out = subprocess.run(["stand/agent-call.sh", name, json.dumps(args, ensure_ascii=False)],
                         capture_output=True, text=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def agent_shell(script, *args):
    """The agent runs a script in its own shell (Hermes' terminal tool, approvals bypassed)."""
    r = compose("exec", "-T", "-u", "hermes", "-w", "/opt/hermes", "-e", "HERMES_HOME=/opt/data/profiles/" + os.environ.get("INTENTRIVET_PROFILE", "intentrivet-demo"),
                "-e", "HERMES_YOLO_MODE=1", "-e", "PYTHONPATH=/opt/hermes", "hermes", "/opt/hermes/.venv/bin/python",
                "-c", TERMINAL_PY, *args, input=script)
    return json.loads(r.stdout.strip().splitlines()[-1])["output"]


def agent_post(body):
    code, _, reply = agent_shell(POST_SH, json.dumps(body)).strip().splitlines()[-1].partition(" ")
    return int(code), json.loads(reply).get("error", reply)


def stand_in(*args):
    return compose("exec", "-T", "telegram-mock", "python", "-I", "/app/telegram_mock.py", *args).stdout


def stats():
    return json.loads(stand_in("stats"))


def seen(s=None):
    s = s or stats()
    line("Stand-in", f"{s['calls']} sendMessage call(s), {s['accepted']} accepted post(s)")
    return s


def record(version_id):
    return json.loads(compose("exec", "-T", "executor", "python", "-I", "/app/executor.py", "show", version_id).stdout)


def wait_for_executor():
    probe = "import urllib.request as u\ntry: u.urlopen('http://127.0.0.1:8080/channels')\nexcept Exception as e: print(e)"
    for _ in range(120):  # any HTTP answer (401 without the key) means it serves again
        if "HTTP Error" in compose("exec", "-T", "executor", "python", "-I", "-c", probe).stdout:
            return
        time.sleep(0.5)
    raise SystemExit("the executor did not come back")


class Operator:
    """The executor's operator CLI, driven over stdin as a person would type."""

    def __init__(self):
        self.p = subprocess.Popen([*COMPOSE, "exec", "-T", "executor", "python", "-I", "-u", "/app/executor.py",
                                   "requests"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.out, self.mark, self.lock = "", 0, threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        decoder = codecs.getincrementaldecoder("utf-8")()
        while chunk := os.read(self.p.stdout.fileno(), 4096):
            with self.lock:
                self.out += decoder.decode(chunk)

    def expect(self, pattern, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                match = re.search(pattern, self.out[self.mark:])
                if match:
                    shown = self.out[self.mark:self.mark + match.end()]
                    self.mark += match.end()
                    return match, shown
            time.sleep(0.1)
        raise SystemExit(f"operator CLI: no {pattern!r} after:\n{self.out[self.mark:]}")

    def type(self, text):
        self.p.stdin.write(text.encode() + b"\n")
        self.p.stdin.flush()

    def open(self, version_id):
        _, listing = self.expect(SELECT)
        number = re.search(rf"\[(\d+)\] {re.escape(version_id)} ", listing)
        if not number:
            raise SystemExit(f"{version_id} is not in Permission requests:\n{listing}")
        self.type(number[1])
        return self.expect(TYPE_ALLOW)[1]

    def quit(self):
        listing = self.expect(SELECT)[1]
        self.type("")
        self.p.wait(30)
        return listing


def operator_allows(version_id):
    op = Operator()
    op.open(version_id)
    op.type("allow")
    _, said = op.expect(rf"Allowed one publication of {re.escape(version_id)} [^\n]*\n")
    op.quit()
    line("Operator", f'Permission requests -> {version_id} -> typed "allow"')
    line("Executor", said.strip().splitlines()[-1])


def post(text, draft_id=None):
    args = {"channel": "test", "text": text, **({"draft_id": draft_id} if draft_id else {})}
    result = tool("intentrivet_prepare_post", args)
    if not result.get("ok"):
        raise SystemExit(f"prepare failed: {result}")
    return result


def case_1():
    print("\n== Case 1: approval is operator-only and bound to the version the operator saw")
    stand_in("reset")
    a1 = post("Stage 2 demo: first line.\nSecond line, draft.")
    line("Agent", f"prepared {a1['version_id']}")
    approve = agent_shell(APPROVE_SH, a1["version_id"]).strip().splitlines()
    for attempt in approve:
        line("Agent", f"from its own shell: {attempt}")
    check(all(a.endswith("-> 404") for a in approve[:4]) and approve[4].endswith(": 0")
          and approve[5].endswith(": unreachable"), "the agent finds no approval route, no database, no engine")
    check(record(a1["version_id"])["publication"] is None, "no permission exists after the agent's attempts")

    op = Operator()
    shown = op.open(a1["version_id"])
    line("Operator", f"opened {a1['version_id']} in Permission requests")
    check(f"Version:  {a1['version_id']}" in shown and "Second line, draft." in shown
          and "IntentRivet Test Channel (local stand)  [test]" in shown and "chat_id:  -1000000000001" in shown,
          "the operator sees the exact version, the full text and the target channel")
    a2 = post("Stage 2 demo: first line.\nSecond line, final.", a1["draft_id"])
    line("Agent", f"meanwhile created {a2['version_id']} of the same draft")
    op.type("allow")
    _, changed = op.expect(r"Open \S+\? \[y/N\]: ")
    line("Operator", f'typed "allow" for {a1["version_id"]}')
    for said in changed.strip().splitlines():
        line("Executor", said)
    check(f"Not allowed: {a1['version_id']} is no longer the latest" in changed and f"Open {a2['version_id']}?" in changed,
          "the CLI shows that the version changed, allows nothing and offers the new one")
    check(record(a1["version_id"])["publication"] is None and record(a2["version_id"])["publication"] is None,
          "the stale confirmation created no permission")
    op.type("y")
    shown = op.expect(TYPE_ALLOW)[1]
    check(f"Version:  {a2['version_id']}" in shown and "Second line, final." in shown, "the operator now sees the new version")
    op.type("allow")
    _, said = op.expect(rf"Allowed one publication of {re.escape(a2['version_id'])} [^\n]*\n")
    op.quit()
    line("Operator", f'opened {a2["version_id"]} (offered), typed "allow"')
    line("Executor", said.strip().splitlines()[-1])
    check(record(a2["version_id"])["status"] == "Allowed: one publication"
          and record(a1["version_id"])["status"] == "Awaiting approval", "only the version that was shown is allowed")
    check(seen()["calls"] == 0, "the stand-in saw no call")
    return a1, a2


def case_2(a1, a2):
    print("\n== Case 2: the permission cannot be redirected; the allowed send still works")
    stand_in("reset")
    attempts = [({"version_id": a2["version_id"], "channel": "main"}, 403),
                ({"version_id": a1["version_id"], "channel": "test"}, 403),
                ({"version_id": a2["version_id"], "channel": "test", "chat_id": -1000000000002}, 400),
                ({"version_id": a2["version_id"], "channel": "test", "text": "Different text"}, 400)]
    for body, expected in attempts:
        code, error = agent_post(body)
        line("Agent", f"from its own shell: POST /publications {json.dumps(body)}")
        line("Executor", f"{code}: {error}")
        check(code == expected, f"refused with {expected}")
    result = tool("intentrivet_publish_post", {"version_id": a2["version_id"], "channel": "main"})
    line("Agent", f"intentrivet_publish_post {a2['version_id']} to main")
    line("Executor", result["error"])
    check(not result["ok"] and "(403)" in result["error"], "the tool call to another channel is refused")
    check(seen()["calls"] == 0, "zero stand-in calls after the refusals")
    check(record(a2["version_id"])["status"] == "Allowed: one publication", "the permission is still unused")

    result = tool("intentrivet_publish_post", {"version_id": a2["version_id"], "channel": "test"})
    line("Agent", f"intentrivet_publish_post {a2['version_id']} to test")
    line("Executor", f"{result.get('outcome')}, {result.get('detail')}" if result["ok"] else result["error"])
    s = seen()
    check(result["ok"] and result["outcome"] == "Published" and (s["calls"], s["accepted"]) == (1, 1),
          "the allowed publication went out once")
    check(s["posts"][-1]["text"] == record(a2["version_id"])["action"]["text"] and s["posts"][-1]["chat_id"] == -1000000000001,
          "the stand-in received exactly the stored text, for the allowed chat_id")
    result = tool("intentrivet_publish_post", {"version_id": a2["version_id"], "channel": "test"})
    line("Agent", "try again: the same call")
    line("Executor", result["error"])
    check(not result["ok"] and "(409)" in result["error"] and seen()["calls"] == 1, "a repeat sends nothing")


def case_3():
    print("\n== Case 3: simultaneous attempts, then a retry after an executor restart")
    stand_in("reset")
    b = post("Stage 2 demo: concurrency.\nOne send only.")
    line("Agent", f"prepared {b['version_id']}")
    operator_allows(b["version_id"])
    body = json.dumps({"version_id": b["version_id"], "channel": "test"})
    codes = sorted(agent_shell(CONCURRENT_SH, body, "8").split())
    line("Agent", f"from its own shell: 8 simultaneous POST /publications for {b['version_id']}")
    line("Executor", f"HTTP {', '.join(codes)}")
    s = seen()
    check(codes == ["200"] + ["409"] * 7 and (s["calls"], s["accepted"]) == (1, 1), "exactly one send")
    compose("restart", "executor")
    wait_for_executor()
    line("Operator", "restarted the executor")
    result = tool("intentrivet_publish_post", {"version_id": b["version_id"], "channel": "test"})
    line("Agent", "try again after the restart")
    line("Executor", result["error"])
    check(not result["ok"] and "(409)" in result["error"] and seen()["calls"] == 1, "still exactly one call")


def case_4():
    print("\n== Case 4: Telegram accepts the post, the connection drops before the reply")
    stand_in("reset")
    stand_in("mode", "drop")
    c = post("Stage 2 demo: lost reply.\nMust not be sent twice.")
    line("Agent", f"prepared {c['version_id']}")
    operator_allows(c["version_id"])
    result = tool("intentrivet_publish_post", {"version_id": c["version_id"], "channel": "test"})
    line("Agent", f"intentrivet_publish_post {c['version_id']} to test")
    line("Executor", result["error"])
    s = seen()
    check("Outcome unknown" in result["error"] and (s["calls"], s["accepted"]) == (1, 1),
          "the stand-in accepted the post; the executor reports Outcome unknown")
    op = Operator()
    listing = op.quit()
    shown = [l for l in listing.splitlines() if l.startswith("Outcome unknown")]
    line("Operator", f"Permission requests: {shown[0] if shown else '(no Outcome unknown line)'}")
    check(bool(shown) and c["version_id"] in shown[0] and record(c["version_id"])["status"] == "Outcome unknown",
          "the operator sees Outcome unknown")
    for when in ("now", "after an executor restart"):
        if when != "now":
            compose("restart", "executor")
            wait_for_executor()
        result = tool("intentrivet_publish_post", {"version_id": c["version_id"], "channel": "test"})
        line("Agent", f"try again {when}")
        line("Executor", result["error"])
        check(not result["ok"] and "(409)" in result["error"] and seen()["calls"] == 1, f"a repeat {when} sends nothing")
    check(record(c["version_id"])["status"] == "Outcome unknown", "the operator still sees Outcome unknown")
    stand_in("mode", "ok")


def case_5():
    print("\n== Case 5: the executor stops while the send is in flight")
    stand_in("reset")
    stand_in("mode", "hold")
    d = post("Stage 2 demo: crash during the send.\nNo second send after restart.")
    line("Agent", f"prepared {d['version_id']}")
    operator_allows(d["version_id"])
    attempt = subprocess.Popen(["stand/agent-call.sh", "intentrivet_publish_post",
                                json.dumps({"version_id": d["version_id"], "channel": "test"})],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for _ in range(100):
        if stats()["calls"]:
            break
        time.sleep(0.1)
    compose("kill", "executor")
    compose("start", "executor")
    wait_for_executor()
    first = json.loads(attempt.communicate(timeout=60)[0].strip().splitlines()[-1])
    line("Agent", f"intentrivet_publish_post {d['version_id']} to test")
    line("Operator", "killed and restarted the executor while the stand-in held the request")
    line("Executor", first.get("error", first))
    publication = record(d["version_id"])["publication"]
    check(publication["outcome"] == "Outcome unknown" and publication["detail"] == "the executor stopped during the send",
          "after the restart the interrupted send is Outcome unknown")
    result = tool("intentrivet_publish_post", {"version_id": d["version_id"], "channel": "test"})
    line("Agent", "try again after the restart")
    line("Executor", result["error"])
    s = seen()
    check(not result["ok"] and "(409)" in result["error"] and (s["calls"], s["accepted"]) == (1, 1),
          "the stand-in still has exactly one call")
    stand_in("mode", "ok")


def case_6():
    print("\n== Case 6: the demo flow on two channels: Main refused before sendMessage, Test published once")
    stand_in("reset")
    e = post("IntentRivet demo announcement.\nAllowed for the Test Channel only.")
    line("Agent", f"prepared {e['version_id']} for test")
    operator_allows(e["version_id"])
    result = tool("intentrivet_publish_post", {"version_id": e["version_id"], "channel": "main"})
    line("Agent", f"intentrivet_publish_post {e['version_id']} to main")
    line("Executor", result["error"])
    check(not result["ok"] and "(403)" in result["error"] and "'test', not 'main'" in result["error"],
          "the tool call for Main is refused, naming the allowed and the requested channel")
    test = subprocess.run(["stand/boundary-test.sh", e["version_id"], "main"], capture_output=True, text=True)
    for said in test.stdout.strip().splitlines():
        line("Boundary", said)
    check(test.returncode == 0 and "HTTP 403" in test.stdout and "Allowed:   [test]" in test.stdout
          and "Requested: [main] IntentRivet Main Channel (local stand)" in test.stdout,
          "boundary test: the direct request from the agent's terminal is refused, the send count unchanged")
    guard = subprocess.run(["stand/boundary-test.sh", e["version_id"], "test"], capture_output=True, text=True)
    check(guard.returncode == 2 and "Not run" in guard.stderr, "the boundary test never requests the allowed channel")
    check(seen()["calls"] == 0 and record(e["version_id"])["status"] == "Allowed: one publication",
          "no stand-in call, the permission still unused")
    result = tool("intentrivet_publish_post", {"version_id": e["version_id"], "channel": "test"})
    line("Agent", f"intentrivet_publish_post {e['version_id']} to test")
    line("Executor", f"{result.get('outcome')}, {result.get('detail')}" if result["ok"] else result["error"])
    s = seen()
    check(result["ok"] and result["outcome"] == "Published" and (s["calls"], s["accepted"]) == (1, 1)
          and s["posts"][-1]["chat_id"] == -1000000000001
          and s["posts"][-1]["text"] == record(e["version_id"])["action"]["text"],
          "the allowed publication went out once, the stored text to the Test chat_id")
    result = tool("intentrivet_publish_post", {"version_id": e["version_id"], "channel": "test"})
    line("Agent", "try again: the same call")
    line("Executor", result["error"])
    check(not result["ok"] and "(409)" in result["error"] and seen()["calls"] == 1, "a repeat sends nothing")


if __name__ == "__main__":
    # This script types "allow" into the operator CLI and sends test posts: never on the live stand.
    if "telegram-mock" not in compose("config", "--services").stdout.split():
        raise SystemExit("e2e_stage2.py runs only on the stand with the Telegram stand-in (compose.yaml).")
    a1, a2 = case_1()
    case_2(a1, a2)
    case_3()
    case_4()
    case_5()
    case_6()
    if failures:
        print(f"\nRESULT: {len(failures)} expectation(s) failed")
        raise SystemExit(1)
    print("\nRESULT: every expectation holds")
