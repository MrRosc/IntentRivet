#!/bin/sh
# Give a model to the demo profile, and only to it (an example: OpenRouter; INTENTRIVET_MODEL picks the model): pin it
# in the profile's config.yaml and copy OPENROUTER_API_KEY from .stand/model.env
# into the profile's own .env (over stdin, never as an argument). The key stays
# out of the container environment, so Hermes' default profile, which defaults
# to anthropic/claude-opus-4.6, has no credentials at all.
set -eu
cd "$(dirname "$0")/.."
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-demo}"   # compose.live.yaml: the example Hermes
MODEL="${INTENTRIVET_MODEL:-openai/gpt-4.1-mini}"
PROFILE="${INTENTRIVET_PROFILE:-intentrivet-demo}"
grep -q '^OPENROUTER_API_KEY=.' .stand/model.env || { echo "No OPENROUTER_API_KEY in .stand/model.env" >&2; exit 1; }
grep '^OPENROUTER_API_KEY=' .stand/model.env | docker compose exec -T -u hermes hermes sh -c '
set -eu
P=/opt/data/profiles/$2
umask 077
{ grep -v "^OPENROUTER_API_KEY=" "$P/.env" || true; cat; } > "$P/.env.new"
mv "$P/.env.new" "$P/.env"
hermes -p "$2" config set model.provider openrouter
hermes -p "$2" config set model.default "$1"
# Auxiliary tasks inherit the main model; this pins their fallback too (default: a paid gemini model).
hermes -p "$2" config set auxiliary.openrouter_model "$1"
echo "OPENROUTER_API_KEY stored in $P/.env"' sh "$MODEL" "$PROFILE"
docker compose restart hermes   # the background gateway picks up the new settings
