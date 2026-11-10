#!/bin/sh
# Test only: stand/e2e_existing.py on a disposable setup in this user's rootless Podman, then the same
# Hermes through an SSH tunnel, as from another host. No real Telegram, no model.
#  - The executor of compose.live.yaml in its own project, image tag and volume, against the Telegram
#    stand-in, without internet; its API on a free 127.0.0.1 port.
#  - An existing Hermes deployed apart from it: the official Hermes image in its own container and
#    volume on the host's network (as Hermes' own docker-compose runs it), with a profile that already
#    has its model, memories, persona, credentials (fake) and another plugin.
#  - The plugin comes from this repository's committed HEAD. After the acceptance:
#    stand/check-isolation.sh as Hermes' user; the API is closed on the host's other addresses; an
#    unprivileged sshd on 127.0.0.1 with a key restricted to the one port forward (README), the profile
#    switched to the tunnel's address in its .env, the tunnel's limits, and
#    stand/check-isolation.sh other-host through that tunnel (ssh given the test's own config).
# Everything is removed at the end unless KEEP=1. Logs in .stand-logs/ (git-ignored).
set -eu
cd "$(dirname "$0")/.."
stand/check-engine.sh >/dev/null
export DOCKER_BUILDKIT=0
run=ir6-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
free_port() { python3 -I -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
port=$(free_port) sport=$(free_port) tport=$(free_port) oport=$(free_port)
tmp=$(mktemp -d) log=.stand-logs/stage6-$(date +%Y%m%d-%H%M%S)-$run
mkdir -p "$log"
image=${HERMES_IMAGE:-$(sed -n 's/^ *image: \(nousresearch\/hermes-agent:.*\)$/\1/p' compose.live.yaml)}  # another Hermes: HERMES_IMAGE
H="docker exec -i -u hermes $run-hermes"
cleanup() {
  set +e   # every step runs, whatever failed before
  [ -n "${sshd:-}" ] && kill "$sshd" 2>/dev/null
  [ -n "${tunnel:-}" ] && kill "$tunnel" 2>/dev/null
  if [ "${KEEP:-0}" != 1 ]; then
    docker rm -f "$run-hermes" >/dev/null 2>&1; docker volume rm "$run-hermes" >/dev/null 2>&1
    docker compose down -v >/dev/null 2>&1; docker rmi "intentrivet-executor:$run" >/dev/null 2>&1
  fi
  rm -rf "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP
printf '123456789:stand-in-token-of-%s\n' "$run" > "$tmp/token"
chmod 644 "$tmp/token"
cat > "$tmp/override.yaml" <<EOF
services:
  executor:
    image: intentrivet-executor:$run
    environment:
      TELEGRAM_API_BASE: http://telegram-mock:8081
    volumes:
      - /dev/null:/etc/intentrivet/channels.json:ro
  telegram-mock:
    image: python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
    command: ["python", "-I", "/app/telegram_mock.py", "serve"]
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    networks: [telegram]
    volumes:
      - $PWD/stand/telegram_mock.py:/app/telegram_mock.py:ro
      - telegram-mock-data:/state
    secrets: [telegram_bot_token]
networks:
  telegram:
    internal: true
volumes:
  telegram-mock-data: {}
secrets:
  telegram_bot_token:
    file: $tmp/token
EOF
export COMPOSE_FILE="compose.live.yaml:$tmp/override.yaml" COMPOSE_PROJECT_NAME=$run INTENTRIVET_PORT=$port
echo "Run $run: executor API 127.0.0.1:$port; logs $log"
docker compose up -d --build executor telegram-mock > "$log/up.log" 2>&1

docker run -d --name "$run-hermes" --network host --security-opt no-new-privileges \
  -v "$run-hermes:/opt/data" "$image" sleep infinity >/dev/null
i=0; until $H hermes --version >/dev/null 2>&1; do i=$((i + 1)); [ $i -lt 60 ] || exit 1; sleep 1; done
profile=work-$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')
$H sh -s "$profile" > "$log/existing-profile.log" 2>&1 <<'EOF'
set -eu
hermes profile create "$1" --no-skills --no-alias
P=$(dirname "$(hermes -p "$1" config path)")
hermes -p "$1" config set model.provider openrouter
hermes -p "$1" config set model.default qwen/qwen3-coder
hermes -p "$1" config set agent.max_turns 40
printf '%s\n' '- The owner prefers short answers.' '- Weekly report goes out on Fridays.' > "$P/memories/MEMORY.md"
printf '%s\n' "You are the owner's assistant. Be brief." > "$P/SOUL.md"
(umask 077; printf '%s\n' OPENROUTER_API_KEY=sk-or-v1-fixture-not-a-key TELEGRAM_ALLOWED_USERS=5550001 \
  TELEGRAM_HOME_CHANNEL=5550001 >> "$P/.env")
mkdir -p /tmp/notes-demo && cd /tmp/notes-demo
printf '%s\n' 'name: notes-demo' 'version: 1.0.0' 'description: Another plugin of the existing profile.' \
  'provides_tools:' '  - notes_demo_echo' > plugin.yaml
printf '%s\n' 'import json' '' '' 'def register(ctx):' \
  '    ctx.register_tool(name="notes_demo_echo", toolset="notes-demo", handler=lambda args, **kw: json.dumps({}),' \
  '                      schema={"name": "notes_demo_echo", "description": "Echo.", "parameters": {"type": "object"}})' \
  > __init__.py
git init -q . && git add . && git -c user.name=fixture -c user.email=fixture@example.invalid commit -qm fixture
hermes -p "$1" plugins install file:///tmp/notes-demo </dev/null
hermes -p "$1" plugins enable notes-demo
EOF
git clone -q . "$tmp/src"
echo "Plugin from commit $(git -C "$tmp/src" rev-parse --short HEAD); existing Hermes profile $profile in container $run-hermes"
docker cp "$tmp/src" "$run-hermes:/tmp/intentrivet" && docker exec "$run-hermes" chown -R hermes:hermes /tmp/intentrivet

status=0
HERMES="$H" HERMES_TTY="docker exec -it -u hermes $run-hermes" HERMES_PROFILE=$profile \
PLUGIN_SOURCE="file:///tmp/intentrivet#plugin" INTENTRIVET_URL="http://127.0.0.1:$port" \
EXECUTOR="docker compose exec -T executor" STAND_IN="docker compose exec -T telegram-mock python -I /app/telegram_mock.py" \
  python3 -I stand/e2e_existing.py 2>&1 | tee "$log/e2e_existing.log"
grep -q '^RESULT: all checks passed' "$log/e2e_existing.log" || status=1

echo; echo "== stand/check-isolation.sh as Hermes' user, in its container"
$H sh /tmp/intentrivet/stand/check-isolation.sh same-host "$(id -un)" 2>&1 | tee "$log/check-isolation.log" || status=1

echo; echo "== The executor's API on this host's other addresses"
for address in $(hostname -I); do
  case $address in *:*) url="http://[$address]:$port/channels" ;; *) url="http://$address:$port/channels" ;; esac
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$url") || code=closed
  if [ "$code" = 000 ] || [ "$code" = closed ]; then echo "  PASS  closed on $address"
  else echo "  FAIL  answers on $address (HTTP $code)"; status=1; fi
done

echo; echo "== Through an SSH tunnel, as from another host (unprivileged sshd on 127.0.0.1:$sport)"
ssh-keygen -q -t ed25519 -N '' -f "$tmp/host_key" && ssh-keygen -q -t ed25519 -N '' -f "$tmp/tunnel_key"
printf 'restrict,port-forwarding,permitopen="127.0.0.1:%s",command="/bin/false" %s\n' "$port" "$(cat "$tmp/tunnel_key.pub")" \
  > "$tmp/authorized_keys"
printf '%s\n' "ListenAddress 127.0.0.1:$sport" "HostKey $tmp/host_key" "AuthorizedKeysFile $tmp/authorized_keys" \
  "PidFile $tmp/sshd.pid" "UsePAM no" "StrictModes no" "PasswordAuthentication no" "KbdInteractiveAuthentication no" \
  "Match User $(id -un) Address 127.0.0.1" "  AllowTcpForwarding local" "  PermitOpen 127.0.0.1:$port" "  PermitListen none" \
  "  AllowStreamLocalForwarding no" "  AllowAgentForwarding no" "  X11Forwarding no" "  PermitTTY no" "  PermitTunnel no" \
  "  ForceCommand /bin/false" > "$tmp/sshd_config"      # README's Match block, for the Hermes host's address
/usr/sbin/sshd -D -e -o LogLevel=VERBOSE -f "$tmp/sshd_config" 2>"$log/sshd.log" &
sshd=$!
sleep 1
ssh_opts="-i $tmp/tunnel_key -p $sport -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=$tmp/known_hosts"
said=$(ssh $ssh_opts "$(id -un)@127.0.0.1" echo shell-allowed 2>/dev/null) && code=0 || code=$?
case $code:$said in 1:) echo "  PASS  the tunnel's key logs in but runs no command" ;;
  *) echo "  FAIL  the tunnel's key: exit $code, said '$said' (expected a login that runs nothing)"; status=1 ;; esac
ssh $ssh_opts -N -L "127.0.0.1:$tport:127.0.0.1:$port" -L "127.0.0.1:$oport:127.0.0.1:$sport" \
  "$(id -un)@127.0.0.1" 2>"$log/tunnel.log" &
tunnel=$!
sleep 2
other=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$oport/") || other=refused
sleep 1
grep -q "administratively prohibited" "$log/tunnel.log" && echo "  PASS  the key forwards to no other port ($other)" ||
  { echo "  FAIL  forward to another port not refused"; status=1; }
timeout 10 ssh $ssh_opts -N -o ExitOnForwardFailure=yes -R "127.0.0.1:$((oport + 1)):127.0.0.1:$port" "$(id -un)@127.0.0.1" \
  2>"$log/reverse.log" && code=0 || code=$?
grep -q "remote port forwarding failed" "$log/reverse.log" && [ "$code" = 255 ] &&
  echo "  PASS  the key gets no reverse forward (ssh -R)" || { echo "  FAIL  ssh -R not refused (exit $code)"; status=1; }
# A socket path that does not exist: sshd logs a policy refusal before it would look; no engine is touched.
timeout 10 ssh $ssh_opts -N -L "$tmp/l.sock:$tmp/engine.sock" "$(id -un)@127.0.0.1" 2>/dev/null & fwd=$!
sleep 2
python3 -I -c "import socket, sys; u = socket.socket(socket.AF_UNIX); u.settimeout(3); u.connect(sys.argv[1]); u.recv(1)" \
  "$tmp/l.sock" 2>/dev/null
kill $fwd 2>/dev/null; sleep 0.5
grep -q "refused streamlocal port forward" "$log/sshd.log" &&
  echo "  PASS  the key gets no Unix-socket forward (refused by sshd's policy)" ||
  { echo "  FAIL  Unix-socket forwarding not refused by sshd's policy"; status=1; }
# The documented way on every Hermes: the line in the profile's .env (v0.20.1's config set writes config.yaml).
$H sh -c 'f=$(dirname "$(hermes -p "$1" config path)")/.env; sed -i "s|^INTENTRIVET_URL=.*|INTENTRIVET_URL=$2|" "$f"' \
  sh "$profile" "http://127.0.0.1:$tport" 2>/dev/null
checked=$($H hermes -p "$profile" intentrivet check 2>&1) || true
echo "$checked" | sed 's/^/    /'
case $checked in *"at http://127.0.0.1:$tport; it accepted the agent key"*) echo "  PASS  hermes intentrivet check through the tunnel" ;;
  *) echo "  FAIL  hermes intentrivet check through the tunnel"; status=1 ;; esac
printf '%s\n' "Host intentrivet-test" "  HostName 127.0.0.1" "  Port $sport" "  User $(id -un)" "  IdentityFile $tmp/tunnel_key" \
  "  IdentitiesOnly yes" "  StrictHostKeyChecking no" "  UserKnownHostsFile $tmp/known_hosts" > "$tmp/ssh_config"
mkdir "$tmp/bin" && printf '#!/bin/sh\nexec /usr/bin/ssh -F %s "$@"\n' "$tmp/ssh_config" > "$tmp/bin/ssh" && chmod +x "$tmp/bin/ssh"
echo "  stand/check-isolation.sh other-host intentrivet-test http://127.0.0.1:$tport:"
PATH="$tmp/bin:$PATH" sh stand/check-isolation.sh other-host intentrivet-test "http://127.0.0.1:$tport" > "$log/check-isolation-tunnel.log" 2>&1 &&
  ok=yes || ok=no
grep -E "^(PASS|FAIL|UNKNOWN)  the |^RESULT|^logins" "$log/check-isolation-tunnel.log" | sed 's/^/    /'
[ "$ok" = yes ] && grep -q "PASS  the SSH login to intentrivet-test is confirmed and runs no command" "$log/check-isolation-tunnel.log" &&
  echo "  PASS  check-isolation verifies the tunnel and its forward-only login" ||
  { echo "  FAIL  check-isolation through the tunnel"; status=1; }

echo
if [ $status -eq 0 ]; then echo "RESULT: existing Hermes accepted (logs $log)"; else echo "RESULT: FAILED (logs $log)"; fi
exit $status
