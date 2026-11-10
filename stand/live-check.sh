#!/bin/sh
# Read-only check by the executor: the bot, its webhook, the owner and each connected channel
# (getMe, getWebhookInfo, getChat, getChatMember). Sends no message and reads no update; the
# token never leaves the executor. Exit 0 only if everything is ready.
set -u
cd "$(dirname "$0")/.."
export COMPOSE_FILE="${COMPOSE_FILE:-compose.live.yaml}"
stand/check-engine.sh || exit 1   # never another engine, e.g. a default Docker Engine
exec docker compose run --rm --no-deps -T executor check
