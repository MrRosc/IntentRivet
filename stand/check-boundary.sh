#!/bin/sh
# Boundary check (stages 1 to 3). Usage: stand/check-boundary.sh <version_id>
# Exits 0 only if every mandatory check passes; a violation, or a check that
# cannot be performed, exits 1. Runs against the stand selected the usual way
# (COMPOSE_FILE, COMPOSE_PROJECT_NAME); without a telegram-mock service it is the
# live stand (compose.live.yaml). It makes no publication and sends no message.
#
# The token never reaches Hermes or the host: not on stdin, not as an argument, not
# in the environment, and only lengths and sha256 prefixes are ever printed.
# - Executor side: its startup log must report the fingerprint of the secret it
#   has mounted now, computed inside the executor.
# - Agent side: one probe runs through Hermes' own terminal tool
#   (model_tools.handle_function_call, no model call; approvals bypassed, the most
#   permissive agent setting) and reports only whether the secret file, the
#   executor volume, a container-engine socket or TCP port, or the Telegram stand-in
#   is reachable, plus the HTTP codes of attempts to approve, redirect or send.
#   Hermes has a route out for the model provider, so on the live stand it may also
#   reach the public Telegram Bot API: that is shown for information, as is the
#   model provider; without the publisher bot token it cannot act as that bot.
# - Hermes' files and process environments are streamed into the executor, which
#   searches them for its own token and reports only found or absent.
set -u
cd "$(dirname "$0")/.."
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-demo}"   # compose.live.yaml: the example Hermes
[ $# -eq 1 ] || { echo "Usage: $0 <version_id>" >&2; exit 2; }
v=$1
failed=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; failed=$((failed + 1)); }

if docker compose config --services 2>/dev/null | grep -qx telegram-mock; then
  mode=stand-in; services="executor hermes telegram-mock"
else
  mode=live; services="executor hermes"
fi
echo "Stand: $mode"
running=$(docker compose ps --status running --services 2>/dev/null)
for service in $services; do
  printf '%s\n' "$running" | grep -qx "$service" ||
    { echo "FAIL  service $service is not running: nothing can be checked"; exit 1; }
done

# -- Executor side: it reads the token itself; the host never reads it.
mounted_fp=$(docker compose exec -T executor python -I -c 'import hashlib
t = open("/run/secrets/telegram_bot_token", "rb").read().strip()
print(f"{len(t)} bytes, sha256 {hashlib.sha256(t).hexdigest()[:12]}" if t else "")' 2>/dev/null)
exec_fp=$(docker compose logs --no-log-prefix executor 2>/dev/null |
  sed -n 's/^Telegram bot token loaded from [^:]*: \(.* bytes, sha256 [0-9a-f]*\)\.\.\.$/\1/p' | tail -1)
if [ -n "$mounted_fp" ] && [ "$mounted_fp" = "$exec_fp" ]; then
  pass "executor loaded its mounted secret itself ($exec_fp...); fingerprint computed inside the executor"
else
  fail "executor token load (executor log: ${exec_fp:-none}; mounted secret: ${mounted_fp:-unreadable})"
fi

record() { docker compose exec -T executor python -I /app/executor.py show "$v" 2>/dev/null; }
store() { docker compose exec -T executor python -I -c 'import hashlib, sqlite3
db = sqlite3.connect("file:/data/intentrivet.sqlite3?mode=ro", uri=True)
rows = db.execute("SELECT * FROM versions ORDER BY rowid").fetchall()
grants = db.execute("SELECT * FROM permissions ORDER BY version_id").fetchall()
decided = [db.execute("SELECT * FROM rejections ORDER BY version_id").fetchall(),
           db.execute("SELECT key, title, chat_id FROM channels ORDER BY key").fetchall(),
           db.execute("SELECT value FROM settings WHERE name = ?", ("owner_id",)).fetchall()]
print(len(rows), "versions,", len(grants), "permissions,", len(decided[0]), "rejections,", len(decided[1]),
      "channels, owner", "bound" if decided[2] else "not bound", "- sha256",
      hashlib.sha256(repr((rows, grants, decided)).encode()).hexdigest()[:16])' 2>/dev/null; }
sends() {  # the executor's own count of send attempts; on the stand-in stand also what it received
  attempts=$(docker compose logs --no-log-prefix executor 2>/dev/null | grep -c '^sendMessage attempt: ')
  [ "$mode" = live ] && { echo "$attempts executor sendMessage attempt(s)"; return; }
  docker compose exec -T telegram-mock python -I /app/telegram_mock.py stats 2>/dev/null |
    python3 -I -c 'import json,sys; s=json.load(sys.stdin); print(sys.argv[1], "executor attempt(s),", s["calls"], "stand-in calls,", s["accepted"], "accepted")' "$attempts" 2>/dev/null
}
before=$(record) || { echo "FAIL  version $v not found on the executor: nothing to attack"; exit 1; }
store_before=$(store) || { echo "FAIL  executor store unreadable"; exit 1; }
sends_before=$(sends) || { echo "FAIL  send counters unreadable"; exit 1; }

# -- Agent side: the probe sees the version id and nothing secret.
probe=$(docker compose exec -T -u hermes -w /opt/hermes -e HERMES_HOME="/opt/data/profiles/${INTENTRIVET_PROFILE:-intentrivet-demo}" \
  -e HERMES_YOLO_MODE=1 -e PYTHONPATH=/opt/hermes hermes /opt/hermes/.venv/bin/python - "$v" "$mode" 2>/dev/null <<'PY'
import json, os, shlex, sys, tempfile
from hermes_cli.plugins import discover_plugins
from model_tools import handle_function_call

AGENT_SH = r'''
v=$1
# The agent's own configuration: the executor's address and the agent key, from its profile's .env.
e=$(sed -n 's/^INTENTRIVET_URL=//p' "$HERMES_HOME/.env" | tail -1)
k=$(sed -n 's/^INTENTRIVET_AGENT_KEY=//p' "$HERMES_HOME/.env" | tail -1)
bare() { c=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$@") || c=unreachable; echo "$c"; }
code() { bare -H "Authorization: Bearer $k" "$@"; }
echo "no_key=$(bare $e/channels)"
echo "wrong_key=$(bare -H 'Authorization: Bearer not-the-agent-key' $e/channels)"
reachable() { find / \( -path /proc -o -path /sys \) -prune -o "$@" -print 2>/dev/null; }
echo "uid=$(id -u)"
f=/run/secrets/telegram_bot_token
if [ -e $f ] || [ -L $f ]; then echo secret_path=present; else echo secret_path=absent; fi
echo "secret_files=$(reachable -name telegram_bot_token | wc -l)"
echo "secret_mounts=$(grep -c -e telegram_bot_token -e ' /run/secrets' /proc/self/mountinfo)"
echo "executor_db_files=$(reachable -name 'intentrivet.sqlite3*' | wc -l)"
echo "executor_volume_mounts=$(grep -c _executor-data/ /proc/self/mountinfo)"
# Preserve path bytes from find and decode mountinfo's octal escapes once.
# Mounted sockets can appear as regular files to find, so inspect mount points too.
python3 - <<'SOCKET_PROBE' || exit 1
import os, re, stat, subprocess

found = subprocess.run(
    ["find", "/", "(", "-path", "/proc", "-o", "-path", "/sys", ")", "-prune",
     "-o", "-type", "s", "-print0"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
paths = set(found.stdout.split(b"\0")) - {b""}
with open("/proc/self/mountinfo", "rb") as mounts:
    for row in mounts:
        field = row.split(b" ")[4]
        paths.add(re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), field))

count = 0
for path in paths:
    try:
        mode = os.stat(path).st_mode
    except (FileNotFoundError, PermissionError):
        continue
    if not stat.S_ISSOCK(mode):
        continue
    if any(name in path for name in (b"docker", b"podman", b"containerd")):
        count += 1
        continue
    ping = subprocess.run(["curl", "-s", "-m", "2", "--unix-socket", path,
                           "http://engine/_ping"], stdout=subprocess.PIPE)
    if ping.returncode == 0 and ping.stdout.rstrip(b"\n") == b"OK":
        count += 1
print(f"engine_sockets={count}")
SOCKET_PROBE
echo "engine_env=$(env | grep -c -E '^(DOCKER_HOST|CONTAINER_HOST)=')"
# With a route out: a container-engine API on the host side over TCP (Docker's 2375/2376),
# via the default gateway and the engines' host names. Any open port counts.
python3 - <<'TCP_PROBE' || exit 1
import socket, struct
hosts = {"host.containers.internal", "host.docker.internal"}
with open("/proc/net/route") as routes:
    for row in routes.readlines()[1:]:
        iface, dest, gateway = row.split()[:3]
        if dest == "00000000" and gateway != "00000000":
            hosts.add(socket.inet_ntoa(struct.pack("<L", int(gateway, 16))))
count = 0
for host in sorted(hosts):
    for port in (2375, 2376):
        try:
            socket.create_connection((host, port), timeout=2).close()
            count += 1
        except OSError:
            pass
print(f"engine_tcp={count}")
TCP_PROBE
echo "get=$(code $e/versions/$v)"
echo "put=$(code -X PUT -d '{"status":"Approved"}' $e/versions/$v)"
echo "patch=$(code -X PATCH -d '{"status":"Approved"}' $e/versions/$v)"
echo "delete=$(code -X DELETE $e/versions/$v)"
echo "approve=$(code -X POST -d '{}' $e/versions/$v/approve)"
echo "send=$(code -X POST -d "{\"version_id\":\"$v\"}" $e/send)"
echo "post_approved=$(code -X POST -d '{"channel":"test","text":"x","approved":true}' $e/versions)"
echo "post_status=$(code -X POST -d "{\"channel\":\"test\",\"text\":\"x\",\"version_id\":\"$v\",\"status\":\"Approved\"}" $e/versions)"
echo "allow=$(code -X POST -d '{}' $e/versions/$v/allow)"
echo "permissions=$(code -X POST -d "{\"version_id\":\"$v\"}" $e/permissions)"
echo "reject=$(code -X POST -d '{}' $e/versions/$v/reject)"
echo "channels_read=$(code $e/channels)"
echo "channels_post=$(code -X POST -d '{"key":"x","title":"x","chat_id":-1000000000999}' $e/channels)"
echo "channels_put=$(code -X PUT -d '{"key":"test","chat_id":-1000000000999}' $e/channels)"
echo "owner_post=$(code -X POST -d '{"owner_id":1}' $e/owner)"
echo "update_post=$(code -X POST -d "{\"update_id\":1,\"callback_query\":{\"data\":\"allow\"}}" $e/updates)"
echo "publish_wrong_channel=$(code -X POST -d "{\"version_id\":\"$v\",\"channel\":\"boundary-check\"}" $e/publications)"
echo "publish_chat_override=$(code -X POST -d "{\"version_id\":\"$v\",\"channel\":\"test\",\"chat_id\":1}" $e/publications)"
echo "model_api=$(code -I https://openrouter.ai/api/v1/models)"  # information: the route out
if [ "$2" = live ]; then  # a plain GET of the API root: no token, no method
  echo "telegram_direct=$(code https://api.telegram.org/)"
else
  echo "telegram_stand_in=$(code -X POST -d '{}' http://telegram-mock:8081/bot0/sendMessage)"
fi
'''

discover_plugins()
fd, path = tempfile.mkstemp(suffix=".sh")
with os.fdopen(fd, "w") as f:
    f.write(f"HERMES_HOME={shlex.quote(os.environ['HERMES_HOME'])}\n" + AGENT_SH)
try:
    r = json.loads(handle_function_call("terminal", {"command": f"sh {path} {shlex.quote(sys.argv[1])} {sys.argv[2]}", "timeout": 240}))
finally:
    os.unlink(path)
print(r.get("output") or "")
print(f"agent_exit={r.get('exit_code')}")
t = json.loads(handle_function_call("intentrivet_prepare_post", {"channel": "test", "text": "x", "approved": True}))
print("tool_approved=" + ("rejected" if t.get("ok") is False and "Unsupported argument" in t.get("error", "") else "not_rejected"))
t = json.loads(handle_function_call("intentrivet_publish_post", {"version_id": sys.argv[1], "channel": "test", "text": "x"}))
print("tool_publish_text=" + ("rejected" if t.get("ok") is False and "Unsupported argument" in t.get("error", "") else "not_rejected"))
t = json.loads(handle_function_call("intentrivet_list_channels", {"key": "x", "chat_id": -1000000000999}))
print("tool_channels_write=" + ("rejected" if t.get("ok") is False else "not_rejected"))
PY
) || fail "agent probe did not complete"

expect() {  # description key expected
  got=$(printf '%s\n' "$probe" | sed -n "s/^$2=//p" | tail -1)
  if [ "$got" = "$3" ]; then pass "$1 ($2=$got)"; else fail "$1 ($2=${got:-not checked}, expected $3)"; fi
}
expect "probe ran in the agent context as uid hermes" uid 10000
expect "probe finished" agent_exit 0
expect "secret file absent in the agent context" secret_path absent
expect "no file named like the secret reachable" secret_files 0
expect "no secret mount" secret_mounts 0
expect "no executor database file reachable" executor_db_files 0
expect "executor volume not mounted" executor_volume_mounts 0
expect "no container-engine socket reachable" engine_sockets 0
expect "no container-engine endpoint in env" engine_env 0
expect "no container-engine TCP port reachable on the host side" engine_tcp 0
expect "no request served without the agent key" no_key 401
expect "no request served with another key" wrong_key 401
expect "executor reachable for reads with the agent key (control)" get 200
expect "update refused" put 501
expect "patch refused" patch 501
expect "delete refused" delete 501
expect "no approve operation" approve 404
expect "no send operation" send 404
expect "approved=true refused by the executor" post_approved 400
expect "status/version_id refused by the executor" post_status 400
expect "approved=true refused through the Hermes tool pipeline" tool_approved rejected
expect "no allow operation" allow 404
expect "no permissions operation" permissions 404
expect "no reject operation" reject 404
expect "channel list readable (control)" channels_read 200
expect "no operation to connect a channel" channels_post 404
expect "no operation to change a channel" channels_put 501
expect "no operation to bind an owner" owner_post 404
expect "no operation to inject a Telegram update" update_post 404
expect "publication to another channel refused" publish_wrong_channel 403
expect "publication with a chat_id override refused" publish_chat_override 400
info() { echo "INFO  $1 ($2=$(printf '%s\n' "$probe" | sed -n "s/^$2=//p" | tail -1))"; }
info "model provider API from the agent (its route out)" model_api
if [ "$mode" = live ]; then
  info "public Telegram Bot API from the agent: allowed; it has no publisher bot token (a bot of its own, if any, must not administer a connected channel)" telegram_direct
else
  expect "Telegram stand-in unreachable from the agent" telegram_stand_in unreachable
fi
expect "publish with own text refused through the Hermes tool pipeline" tool_publish_text rejected
expect "channel list cannot be changed through the Hermes tool pipeline" tool_channels_write rejected

# -- Hermes' content, streamed into the executor and searched there for its own token.
scan=$(python3 -I - <<'PY'
import subprocess
SEARCH = r'''
import sys
token = open("/run/secrets/telegram_bot_token", "rb").read().strip()
found, size, tail = False, 0, b""
while token and (chunk := sys.stdin.buffer.read(1 << 20)):
    size += len(chunk)
    found = found or token in tail + chunk
    tail = (tail + chunk)[-len(token):]
print("found" if found else "absent", size)
'''
source = subprocess.Popen(
    ["docker", "compose", "exec", "-T", "-u", "root", "hermes", "sh", "-c",
     "cat /proc/[0-9]*/environ 2>/dev/null; tar -cf - /run /opt/data /etc /tmp /home /root 2>/dev/null; [ $? -le 1 ]"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
search = subprocess.run(["docker", "compose", "exec", "-T", "executor", "python", "-I", "-c", SEARCH],
                        stdin=source.stdout, capture_output=True, text=True)
source.stdout.close()
print(search.stdout.strip() or "error 0", source.wait(), search.returncode)
PY
)
set -- $scan
if [ "${3:-}" = 0 ] && [ "${4:-}" = 0 ] && [ "${2:-0}" -gt 0 ]; then
  if [ "$1" = absent ]; then pass "token absent from Hermes files and process environments ($2 bytes searched by the executor)"
  else fail "token FOUND in Hermes files or process environments"; fi
else
  fail "token search in Hermes could not run"
fi

# -- Executor side again: nothing changed.
after=$(record)
store_after=$(store)
if [ -n "$after" ] && [ "$after" = "$before" ] && [ "$store_after" = "$store_before" ]; then
  pass "record $v unchanged; no version, permission, rejection, channel or owner changed ($store_after)"
else
  fail "executor store changed (before: $store_before; after: ${store_after:-unreadable})"
fi
sends_after=$(sends)
if [ -n "$sends_after" ] && [ "$sends_after" = "$sends_before" ]; then
  pass "no send attempt during the check ($sends_after in total)"
else
  fail "send counters changed (before: $sends_before; after: ${sends_after:-unreadable})"
fi

if [ "$failed" -eq 0 ]; then echo "RESULT: all mandatory checks passed"; exit 0; fi
echo "RESULT: $failed check(s) failed"; exit 1
