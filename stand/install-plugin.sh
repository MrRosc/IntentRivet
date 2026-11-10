#!/bin/sh
# The bundled example Hermes only (the stand-in stand of compose.yaml, or compose.live.yaml with
# --profile demo). An existing Hermes installs the plugin the standard way instead (README, Install, step 4).
# Installs IntentRivet from this repository's committed HEAD into the Hermes profile
# $INTENTRIVET_PROFILE (default intentrivet-demo; created empty if missing), enables it explicitly
# and connects it to the executor on the stand's internal network: INTENTRIVET_URL and the agent key
# in the profile's .env. A key already there is kept; otherwise the executor issues one, which it does
# without a confirmation only while it has none. The key goes from the executor to the profile over a
# pipe, never as an argument. Re-running reinstalls into the same profile. Only this one-off container
# sees .git (read-only); the long-running Hermes container never does.
set -eu
cd "$(dirname "$0")/.."
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-demo}"   # compose.live.yaml: the example Hermes
P="${INTENTRIVET_PROFILE:-intentrivet-demo}"
docker compose run --rm -T --no-deps -e S6_VERBOSITY=0 \
  -v "$PWD/.git:/src/intentrivet.git:ro" hermes sh -c '
set -eu
# Hermes runs its internal git with an isolated config, and git drops injected
# config for a local upload-pack, so cloning straight from a mount owned by
# another uid fails with "dubious ownership". Make one plain local clone with an
# ephemeral trust entry, then install from it the standard way.
printf "[safe]\n\tdirectory = /src/intentrivet.git\n" > /tmp/trust.gitconfig
GIT_CONFIG_GLOBAL=/tmp/trust.gitconfig git clone -q /src/intentrivet.git /tmp/intentrivet
echo "Installing commit $(git -C /tmp/intentrivet rev-parse HEAD)"
[ -d "/opt/data/profiles/$1" ] || hermes profile create "$1" --no-skills --no-alias
hermes -p "$1" plugins install --force "file:///tmp/intentrivet#plugin" </dev/null
hermes -p "$1" plugins enable intentrivet' sh "$P"

# The profile's .env gets INTENTRIVET_URL and, on stdin, the key line (none: keep the profile's key).
ENV_SH='set -eu
f=/opt/data/profiles/$1/.env
IFS= read -r key_line || key_line=
[ -n "$key_line" ] || key_line=$(grep "^INTENTRIVET_AGENT_KEY=." "$f" 2>/dev/null | tail -1)
[ -n "$key_line" ] || { echo "No agent key for profile $1: the executor issued none (see above)." >&2; exit 1; }
umask 077
{ grep -v -e "^INTENTRIVET_URL=" -e "^INTENTRIVET_AGENT_KEY=" "$f" 2>/dev/null || true
  printf "INTENTRIVET_URL=http://executor:8080\n%s\n" "$key_line"; } > "$f.new"
mv "$f.new" "$f"
echo "Hermes profile $1: connected to http://executor:8080 with an agent key."'
if docker compose exec -T -u hermes hermes grep -q "^INTENTRIVET_AGENT_KEY=." "/opt/data/profiles/$P/.env" 2>/dev/null; then
  docker compose exec -T -u hermes hermes sh -c "$ENV_SH" sh "$P" </dev/null
else
  docker compose exec -T executor python -I /app/executor.py agent-key </dev/null |
    sed -n 's/^  \([A-Za-z0-9_-]\{20,\}\)$/INTENTRIVET_AGENT_KEY=\1/p' |
    docker compose exec -T -u hermes hermes sh -c "$ENV_SH" sh "$P"
fi
# Hermes' background gateway runs the tools and keeps plugin code loaded: restart it.
docker compose restart hermes
