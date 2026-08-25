#!/usr/bin/env bash
# Run the Kafka service control agent (consumes service_control, publishes service_status).
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)/openride_apps"
exec python3 -m openride_control.agent "$@"
