#!/usr/bin/env bash
# Back-compat shim — the package was renamed openride_cli -> openride and grew verb groups.
# The old flat headless-run flags now live under the `run` verb. With no subcommand we keep
# the historical behavior (interactive run), so existing invocations still work:
#   scripts/openride-cli.sh --scenario X --solver GreedyNearest   ->  openride run --scenario X ...
# Prefer scripts/openride.sh going forward.
set -euo pipefail
ROOT="${OPENRIDE_WORKSPACE_ROOT:-/home/user}"
PY="${OPENRIDE_CLI_PYTHON:-$ROOT/pyjupenv/bin/python}"
cd "$ROOT"
# If the first arg is already a verb group, pass through untouched; otherwise default to `run`.
case "${1:-}" in
  run|scenario|analyze|runs|services|solver|-h|--help|"") verb=() ;;
  *) verb=(run) ;;
esac
exec env PYTHONPATH="$ROOT:$ROOT/openride_apps${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m openride "${verb[@]}" "$@"
