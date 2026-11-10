#!/bin/sh
# IntentRivet executor setup on the executor host, in a terminal, as the account that runs it. Safe to
# run again: each step keeps what is there. Hermes is not involved: it is installed and connected
# separately (README, Install, step 4), after this.
#  0. The container engine check (stand/check-engine.sh): only the verified rootless Podman goes on;
#     anything else stops here, with nothing changed.
#  1. The publisher bot's token, typed at a hidden prompt (stand/live-setup.sh), unless one is stored.
#  2. Build and start the executor alone (compose.live.yaml; its store is a named volume, kept across
#     restarts). Its API is published on 127.0.0.1:${INTENTRIVET_PORT:-8787} of this host only.
#  3. A read-only check of the bot by the executor: a webhook is reported, never removed.
#  4. While no owner is bound: a one-time link to the bot's private chat; whoever opens it within
#     10 minutes and presses Start becomes the owner. It is printed here only.
set -eu
cd "$(dirname "$0")/.."
export COMPOSE_FILE="${COMPOSE_FILE:-compose.live.yaml}"
export DOCKER_BUILDKIT=0   # Podman builds the image itself: no extra BuildKit builder container
cat <<'EOF'
IntentRivet setup on the executor host: your publisher bot and the executor, apart from Hermes.
- Your existing Hermes keeps its model, memory and its own Telegram bot, the one you chat with.
  It gets an agent key: it can prepare posts and ask to publish them, never allow them.
- Your publisher bot is a second, new bot from @BotFather, for IntentRivet only. Only the executor
  holds its token: it sends you each preview with Allow once / Reject and posts to your channels.
- Keep the executor where Hermes cannot reach its files, its container engine or root on this host
  (README: Where to put the executor), and give Hermes no other way to post in your channels: its
  own bot, or any account whose credentials Hermes can read, must not administer them.

EOF
stand/check-engine.sh
[ -f .stand-live/telegram_bot_token ] || stand/live-setup.sh
docker compose up -d --build executor
docker compose exec -T executor python -I /app/executor.py check ||
  { echo "Fix the problem above, then run stand/setup.sh again." >&2; exit 1; }
docker compose exec -T executor python -I /app/executor.py bind
cat <<'EOF'

Next steps:
1. Owner: if a link is shown above, open it in Telegram within 10 minutes, signed in as the account
   that will own the bot, and press Start.
2. Your channels, in the publisher bot's private chat: Add channel. Make the bot an administrator of
   the channel with the right to post messages, then post the check text the bot gives you in the
   channel: it is a real post that subscribers can see; delete it once the bot confirms. Name the
   channel with a short key of your own; Hermes will use it. Hermes' own bot must not be an
   administrator there. For another channel, now or later: Add channel again.
3. The agent key for Hermes, shown once:
   docker compose exec executor python -I /app/executor.py agent-key
4. Hermes: install the plugin into your existing profile and check the connection (README:
   Install, step 4; if Hermes runs on another host, first: Hermes on another host: SSH tunnel).
5. Start after a reboot: README, Executor autostart after a reboot (an administrator, once).
EOF
