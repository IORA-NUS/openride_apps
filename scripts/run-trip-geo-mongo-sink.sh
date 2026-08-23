#!/usr/bin/env bash
# trip_geo_stream → MongoDB sink (used by openride-trip-geo-sink.service).
# Persists live haul-trip route geometry so post-run/replay reads it verbatim
# (no straight-line fallback, no OSRM at read time).
set -euo pipefail

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
  # shellcheck source=/dev/null
  source "$NVM_DIR/nvm.sh"
fi

cd /home/user/openride_server/analytics

export KAFKA_BROKER_URL="${KAFKA_BROKER_URL:-localhost:9094}"
export MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017/OpenRoadDB}"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found — install Node (nvm) or set PATH in openride-trip-geo-sink.service" >&2
  exit 127
fi

exec npm run trip-geo:mongo-sink
