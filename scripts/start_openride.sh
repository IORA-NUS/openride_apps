#!/usr/bin/env bash
# One-time bootstrap: persist Kafka, control agent, analytics, and Celery across SSH logout.
set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

echo "Enabling user lingering (services survive logout)…"
loginctl enable-linger "$USER" 2>/dev/null || echo "Note: loginctl enable-linger may require sudo on some systems."

echo "Reloading systemd user daemon…"
# Install the unit templates before enabling anything. They live in systemd/*.in
# with @WORKSPACE@ standing in for the workspace root; without this step a machine
# that never had them hand-installed dies here under `set -euo pipefail`.
_SELF="$(readlink -f "${BASH_SOURCE[0]}")"
REPO="$(cd "$(dirname "$_SELF")/.." && pwd)"
WORKSPACE="${OPENRIDE_WORKSPACE_ROOT:-$(cd "$REPO/.." && pwd)}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
for tpl in "$REPO"/systemd/*.service.in; do
  unit="$(basename "${tpl%.in}")"
  sed "s|@WORKSPACE@|$WORKSPACE|g" "$tpl" > "$UNIT_DIR/$unit"
done
echo "Installed $(ls "$REPO"/systemd/*.service.in | wc -l) unit files into $UNIT_DIR (workspace=$WORKSPACE)"

systemctl --user daemon-reload

# RETIRED 2026-08-12: openride-kpi-duckdb-sink. `apps/dataplane` is now the single writer.
# The legacy sink exported to Mongo `kpi` only on a terminal `run_status`, so a killed or
# crashed run persisted NOTHING, and its consumer was at-most-once by construction
# (`enable.auto.commit: True` with no manual commit, `sink.py:49`) — 38 of 73 runs in `kpi`
# hold a truncated series as a result. The dataplane writes DuckDB continuously at ingest
# and reconciles to Mongo every 300 s with OPEN summaries, so a killed run keeps its data.
# The unit file, `scripts/run-kpi-duckdb-sink.sh`, the `kpi` collection and every per-run
# DuckDB file under ~/.openride/kpi-duckdb are all DELIBERATELY LEFT IN PLACE — retiring a
# service here means stopping and disabling the unit, never removing data. To revive it:
#   systemctl --user enable --now openride-kpi-duckdb-sink.service
for unit in openride-kafka openride-dataplane openride-trip-geo-sink openride-control-agent openride-analytics openride-celery; do
  echo "Enabling and starting ${unit}.service…"
  systemctl --user enable --now "${unit}.service"
done

echo ""
echo "Bootstrap complete. Dashboard: http://127.0.0.1:3000"
echo "  Services:  http://127.0.0.1:3000/services"
echo "  Scenario:  http://127.0.0.1:3000/scenario"
echo ""
echo "These units are now enabled — they auto-start on login/boot (linger permitting)."
echo "To stop:  bash scripts/stop_openride.sh"
echo "To disable auto-start: bash scripts/stop_openride.sh --disable"
echo ""
systemctl --user status openride-kafka.service openride-dataplane.service openride-trip-geo-sink.service openride-control-agent.service openride-analytics.service openride-celery.service --no-pager || true
