#!/usr/bin/env bash
# KPI stream → DuckDB sink (used by openride-kpi-duckdb-sink.service).
set -euo pipefail

# Repo root, derived from this script's own resolved location so a checkout
# somewhere else (or a differently-named one) works. These used to be literal
# /home/user/openride_apps paths.
_SELF="$(readlink -f "${BASH_SOURCE[0]}")"
REPO="$(cd "$(dirname "$_SELF")/.." && pwd)"
ROOT="${OPENRIDE_WORKSPACE_ROOT:-$(cd "$REPO/.." && pwd)}"
PYBIN="${OPENRIDE_PYTHON:-$REPO/venv/bin/python}"

export PYTHONPATH="$ROOT:$REPO:${PYTHONPATH:-}"
export KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:-localhost:9094}"
export KPI_DUCKDB_DATA_DIR="${KPI_DUCKDB_DATA_DIR:-$HOME/.openride/kpi-duckdb}"
export MONGODB_HOST="${MONGODB_HOST:-localhost}"
export MONGODB_PORT="${MONGODB_PORT:-27017}"
export MONGODB_NAME="${MONGODB_NAME:-OpenRoadDB}"

PYTHON="$PYBIN"
exec "$PYTHON" -m apps.kpi_sink
