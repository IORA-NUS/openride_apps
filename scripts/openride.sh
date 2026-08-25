#!/usr/bin/env bash
# Launch the unified OpenRide CLI.
# Runs under the apps venv, which carries rich/questionary/pymongo along with the
# simulation dependencies (all declared in requirements.txt). It used to default to
# ~/pyjupenv -- a general-purpose Jupyter environment that no requirements file
# described -- which meant a clone could install everything this repo declares and
# still not be able to start the CLI. Falls back to pyjupenv if the venv is absent,
# and OPENRIDE_CLI_PYTHON still overrides.
#
# Verb groups: run | scenario | analyze | runs | services | solver
# Examples:
#   scripts/openride.sh scenario list
#   scripts/openride.sh scenario new --name "Demo" --trucks 200 --orders 2000 --days 7
#   scripts/openride.sh run 200_trucks_7_days --solver GreedyNearest
#   scripts/openride.sh analyze run_20260623_103824
set -euo pipefail
# Resolve THIS script through symlinks before walking up: /home/user/scripts is a
# symlink into this repo, and the script is often invoked by a relative path from
# another directory (systemd units do exactly this). Using $BASH_SOURCE unresolved
# made ROOT depend on the caller's cwd -- from /home/user it computed ROOT=/ and
# fell through to a bare `python3`, which has no openride module.
_SELF="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="${OPENRIDE_WORKSPACE_ROOT:-$(cd "$(dirname "$_SELF")/../.." && pwd)}"
if [ -n "${OPENRIDE_CLI_PYTHON:-}" ]; then PY="$OPENRIDE_CLI_PYTHON"
elif [ -x "$ROOT/openride_apps/venv/bin/python" ]; then PY="$ROOT/openride_apps/venv/bin/python"
elif [ -x "$ROOT/pyjupenv/bin/python" ]; then PY="$ROOT/pyjupenv/bin/python"
else PY="python3"; fi
cd "$ROOT"
exec env PYTHONPATH="$ROOT:$ROOT/openride_apps${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m openride "$@"
