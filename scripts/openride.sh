#!/usr/bin/env bash
# Launch the unified OpenRide CLI.
# Runs under pyjupenv (has rich/questionary/pymongo); the CLI shells out to the apps venv
# (via openride_control.command) for every scenario/service primitive and launches the sim.
#
# Verb groups: run | scenario | analyze | runs | services | solver
# Examples:
#   scripts/openride.sh scenario list
#   scripts/openride.sh scenario new --name "Demo" --trucks 200 --orders 2000 --days 7
#   scripts/openride.sh run 200_trucks_7_days --solver GreedyNearest
#   scripts/openride.sh analyze run_20260623_103824
set -euo pipefail
ROOT="${OPENRIDE_WORKSPACE_ROOT:-/home/user}"
PY="${OPENRIDE_CLI_PYTHON:-$ROOT/pyjupenv/bin/python}"
cd "$ROOT"
exec env PYTHONPATH="$ROOT:$ROOT/openride_apps${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m openride "$@"
