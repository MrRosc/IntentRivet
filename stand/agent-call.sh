#!/bin/sh
# Call one tool through Hermes' own tool pipeline (model_tools.handle_function_call) in
# the demo profile, exactly as the agent would, but without a model. Like Hermes at start, it
# first loads the profile's .env (load_hermes_dotenv), where get_secret finds the plugin's settings.
# Usage: stand/agent-call.sh <tool> '<json arguments>'
set -eu
cd "$(dirname "$0")/.."
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-demo}"   # compose.live.yaml: the example Hermes
[ $# -eq 2 ] || { echo "Usage: $0 <tool> '<json arguments>'" >&2; exit 2; }
docker compose exec -T -u hermes -w /opt/hermes -e HERMES_HOME="/opt/data/profiles/${INTENTRIVET_PROFILE:-intentrivet-demo}" \
  -e PYTHONPATH=/opt/hermes hermes /opt/hermes/.venv/bin/python -c '
import json, sys
from hermes_cli.env_loader import load_hermes_dotenv
load_hermes_dotenv()
from hermes_cli.plugins import discover_plugins
from model_tools import handle_function_call
discover_plugins()
print(handle_function_call(sys.argv[1], json.loads(sys.argv[2])))' "$1" "$2"
