#!/bin/sh
# Optional: chat with Hermes in Telegram through Hermes' own gateway, with a SECOND bot. The
# publisher bot's token is never reused: two programs reading one bot's updates break both.
# The token is typed at a hidden prompt; it goes over stdin into the Hermes profile's .env only,
# with TELEGRAM_ALLOWED_USERS set to the publisher bot's owner, so the Hermes bot answers nobody
# else. It is refused if it is the publisher bot's token (compared by SHA-256 in the executor) or
# another Hermes profile here already uses it. Then the Hermes container restarts its gateways.
set -eu
cd "$(dirname "$0")/.."
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-demo}"   # compose.live.yaml: the example Hermes
export COMPOSE_FILE="${COMPOSE_FILE:-compose.live.yaml}"
P="${INTENTRIVET_PROFILE:-intentrivet-demo}"
[ -t 0 ] || { echo "Run this in a terminal: the token is typed at a hidden prompt." >&2; exit 1; }
owner=$(docker compose exec -T executor python -I -c 'import sqlite3
db = sqlite3.connect("file:/data/intentrivet.sqlite3?mode=ro", uri=True)
row = db.execute("SELECT value FROM settings WHERE name = ?", ("owner_id",)).fetchone()
print(row[0] if row else "")')
[ -n "$owner" ] || { echo "Bind the publisher bot's owner first (stand/setup.sh)." >&2; exit 1; }

trap 'stty echo; echo' EXIT
trap 'exit 130' INT TERM HUP
stty -echo
printf "Token of the bot for chatting with Hermes, NOT the publisher bot's (input hidden): "
IFS= read -r token || token=
stty echo
trap - EXIT INT TERM HUP
echo
bad() { echo "$1 Nothing was saved." >&2; exit 1; }
case ${token%%:*} in ''|*[!0-9]*) bad "That is not a bot token." ;; esac
case ${token#*:} in ''|*[!A-Za-z0-9_-]*) bad "That is not a bot token." ;; esac
digest=$(printf '%s' "$token" | sha256sum | cut -d' ' -f1)
same=$(docker compose exec -T executor python -I -c 'import hashlib, sys
mine = open("/run/secrets/telegram_bot_token", "rb").read().strip()
print("same" if hashlib.sha256(mine).hexdigest() == sys.argv[1] else "different")' "$digest")
[ "$same" = different ] || bad "That is the publisher bot's token: create a second bot for Hermes."

printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ALLOWED_USERS=%s\n' "$token" "$owner" |
  docker compose exec -T -u hermes hermes sh -c '
set -eu
P=/opt/data/profiles/$1
[ -d "$P" ] || { echo "No Hermes profile $1: run stand/setup.sh first. Nothing was saved." >&2; exit 1; }
IFS= read -r token_line; IFS= read -r users_line
for other in /opt/data/.env /opt/data/profiles/*/.env; do   # builtins only: the token is no argument
  [ "$other" != "$P/.env" ] && [ -f "$other" ] || continue
  while IFS= read -r line; do
    [ "$line" != "$token_line" ] || { echo "$other already uses this bot. Nothing was saved." >&2; exit 1; }
  done < "$other"
done
umask 077
{ grep -v -e "^TELEGRAM_BOT_TOKEN=" -e "^TELEGRAM_ALLOWED_USERS=" "$P/.env" 2>/dev/null || true
  printf "%s\n%s\n" "$token_line" "$users_line"; } > "$P/.env.new"
mv "$P/.env.new" "$P/.env"
echo "Hermes profile $1: its Telegram bot is set; it answers only ${users_line#*=}."' sh "$P"
unset token
docker compose restart hermes   # the profile gateway starts with its Telegram bot
