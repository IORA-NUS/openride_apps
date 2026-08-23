#!/usr/bin/env bash
# Start Next.js analytics dev server (used by openride-analytics.service).
set -euo pipefail

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
  # shellcheck source=/dev/null
  source "$NVM_DIR/nvm.sh"
fi

cd /home/user/openride_server/analytics
export OPENRIDE_WORKSPACE_ROOT="${OPENRIDE_WORKSPACE_ROOT:-/home/user}"
export KAFKA_BROKER_URL="${KAFKA_BROKER_URL:-localhost:9094}"
export NODE_ENV="${NODE_ENV:-development}"
export OPENRIDE_PYTHON="${OPENRIDE_PYTHON:-/home/user/openride_apps/venv/bin/python}"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found — install Node (nvm) or set PATH in openride-analytics.service" >&2
  exit 127
fi

exec npm run dev -- --port 3000
