#!/usr/bin/env bash
# Stop OpenRide bootstrap services (Kafka, control agent, analytics, Celery).
set -euo pipefail

DISABLE=false
for arg in "$@"; do
  case "$arg" in
    --disable)
      DISABLE=true
      ;;
    -h|--help)
      echo "Usage: $0 [--disable]"
      echo ""
      echo "  Stops openride-kafka, openride-dataplane, openride-trip-geo-sink, openride-control-agent, openride-analytics, and openride-celery."
      echo "  --disable   Also disable units so they do not auto-start on next login/boot."
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (try --help)" >&2
      exit 1
      ;;
  esac
done

# openride-kpi-duckdb-sink retired 2026-08-12 — see the note in start_openride.sh. Kept in
# the stop list so a manually-revived sink is still stopped by this script; it is absent
# from the start list and from the --disable list because it must not come back on its own.
units=(openride-celery openride-analytics openride-dataplane openride-control-agent openride-trip-geo-sink openride-kpi-duckdb-sink openride-kafka)

echo "Stopping bootstrap services…"
for unit in "${units[@]}"; do
  if systemctl --user is-active --quiet "${unit}.service" 2>/dev/null; then
    echo "  stopping ${unit}.service"
    systemctl --user stop "${unit}.service"
  else
    echo "  ${unit}.service already stopped"
  fi
done

if [[ "$DISABLE" == true ]]; then
  echo ""
  echo "Disabling auto-start on boot…"
  for unit in openride-dataplane openride-kafka openride-trip-geo-sink openride-control-agent openride-analytics openride-celery; do
    if systemctl --user is-enabled --quiet "${unit}.service" 2>/dev/null; then
      echo "  disabling ${unit}.service"
      systemctl --user disable "${unit}.service"
    fi
  done
fi

echo ""
echo "Bootstrap services stopped."
if [[ "$DISABLE" == false ]]; then
  echo "Units remain enabled — they will start again on next login/boot."
  echo "To prevent that: $0 --disable"
else
  echo "Units disabled — run scripts/start_openride.sh to start again."
fi
echo ""
systemctl --user status openride-kafka.service openride-dataplane.service openride-kpi-duckdb-sink.service openride-trip-geo-sink.service openride-control-agent.service openride-analytics.service openride-celery.service --no-pager 2>&1 || true
