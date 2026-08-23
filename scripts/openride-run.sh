#!/usr/bin/env bash
# Non-interactive OpenRide run — the scriptable / cron-callable entry (back-compat shim).
# Forces the vanilla JSON run path. The package was renamed openride_cli -> openride; the
# headless run flags now live under the `run` verb. Exit codes:
#   0 completed · 1 failed · 2 bad args · 3 host busy · 4 backend/mongo not ready · 5 timed out
#
# Examples:
#   scripts/openride-run.sh --scenario 200_trucks_7_days --solver GreedyNearest
#   scripts/openride-run.sh --scenario 200_trucks_7_days --max-wall-seconds 3600 > result.json
set -euo pipefail
ROOT="${OPENRIDE_WORKSPACE_ROOT:-/home/user}"
PY="${OPENRIDE_CLI_PYTHON:-$ROOT/pyjupenv/bin/python}"
cd "$ROOT"
# --json forces the vanilla non-interactive path even from a TTY.
exec env PYTHONPATH="$ROOT:$ROOT/openride_apps${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m openride run --json "$@"
