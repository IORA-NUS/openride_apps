#!/usr/bin/env bash
# apps/dataplane — the read/serve plane: consumes every OpenRide stream, stores to DuckDB,
# archives to Mongo, and serves the frontend's read API on :8620.
#
# The dashboard reads ONLY from this service (no Mongo fallback since 2026-08-07), so it is a
# hard dependency of openride-analytics, not an optional sink.
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
export DATAPLANE_DUCKDB_PATH="${DATAPLANE_DUCKDB_PATH:-$HOME/.openride/dataplane-live/dataplane.duckdb}"
export DATAPLANE_HTTP_PORT="${DATAPLANE_HTTP_PORT:-8620}"
export MONGODB_HOST="${MONGODB_HOST:-localhost}"
export MONGODB_PORT="${MONGODB_PORT:-27017}"
# Its own archive database: the dataplane writes its own collections and must not be confused
# with the legacy kpi_sink's exports in OpenRoadDB.
export MONGODB_NAME="${MONGODB_NAME:-OpenRoadDB_dataplane_live}"

mkdir -p "$(dirname "$DATAPLANE_DUCKDB_PATH")"

PYTHON="$PYBIN"
exec "$PYTHON" -m apps.dataplane
