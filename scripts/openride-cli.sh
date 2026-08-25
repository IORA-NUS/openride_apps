#!/usr/bin/env bash
# Back-compat shim — the package was renamed openride_cli -> openride and grew verb groups.
# The old flat headless-run flags now live under the `run` verb. With no subcommand we keep
# the historical behavior (interactive run), so existing invocations still work:
#   scripts/openride-cli.sh --scenario X --solver GreedyNearest   ->  openride run --scenario X ...
# Prefer scripts/openride.sh going forward.
set -euo pipefail
_SELF="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="${OPENRIDE_WORKSPACE_ROOT:-$(cd "$(dirname "$_SELF")/../.." && pwd)}"
# Prefer the apps venv (it carries rich/questionary now); pyjupenv is only a
# fallback. These shims used to REQUIRE pyjupenv, contradicting openride.sh.
if   [ -n "${OPENRIDE_CLI_PYTHON:-}" ]; then PY="$OPENRIDE_CLI_PYTHON"
elif [ -x "$ROOT/openride_apps/venv/bin/python" ]; then PY="$ROOT/openride_apps/venv/bin/python"
elif [ -x "$ROOT/pyjupenv/bin/python" ]; then PY="$ROOT/pyjupenv/bin/python"
else PY="python3"; fi
cd "$ROOT"
# If the first arg is already a verb group, pass through untouched; otherwise default to `run`.
case "${1:-}" in
  run|scenario|analyze|runs|services|solver|-h|--help|"") verb=() ;;
  *) verb=(run) ;;
esac
exec env PYTHONPATH="$ROOT:$ROOT/openride_apps${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m openride "${verb[@]}" "$@"
