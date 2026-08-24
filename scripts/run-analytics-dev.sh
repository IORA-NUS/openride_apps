#!/usr/bin/env bash
# Start Next.js analytics dev server (used by openride-analytics.service).
set -euo pipefail

# Repo root, derived from this script's own resolved location so a checkout
# somewhere else (or a differently-named one) works. These used to be literal
# /home/user/openride_apps paths.
_SELF="$(readlink -f "${BASH_SOURCE[0]}")"
REPO="$(cd "$(dirname "$_SELF")/.." && pwd)"
ROOT="${OPENRIDE_WORKSPACE_ROOT:-$(cd "$REPO/.." && pwd)}"
PYBIN="${OPENRIDE_PYTHON:-$REPO/venv/bin/python}"

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
  # shellcheck source=/dev/null
  source "$NVM_DIR/nvm.sh"
fi

cd "$ROOT/openride_server/analytics"
export OPENRIDE_WORKSPACE_ROOT="$ROOT"
export KAFKA_BROKER_URL="${KAFKA_BROKER_URL:-localhost:9094}"
export NODE_ENV="${NODE_ENV:-development}"
export OPENRIDE_PYTHON="$PYBIN"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found — install Node (nvm) or set PATH in openride-analytics.service" >&2
  exit 127
fi

exec npm run dev -- --port 3000
