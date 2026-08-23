#!/usr/bin/env bash
# KPI stream → DuckDB sink (used by openride-kpi-duckdb-sink.service).
set -euo pipefail

export PYTHONPATH="/home/user:/home/user/openride_apps:${PYTHONPATH:-}"
export KAFKA_BOOTSTRAP_SERVERS="${KAFKA_BOOTSTRAP_SERVERS:-localhost:9094}"
export KPI_DUCKDB_DATA_DIR="${KPI_DUCKDB_DATA_DIR:-$HOME/.openride/kpi-duckdb}"
export MONGODB_HOST="${MONGODB_HOST:-localhost}"
export MONGODB_PORT="${MONGODB_PORT:-27017}"
export MONGODB_NAME="${MONGODB_NAME:-OpenRoadDB}"

PYTHON="${OPENRIDE_PYTHON:-/home/user/openride_apps/venv/bin/python}"
exec "$PYTHON" -m apps.kpi_sink
