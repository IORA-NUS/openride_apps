#!/usr/bin/env bash
# Fan out N Celery worker processes (each with its own eventlet pool) so we
# don't run into the single-process GIL ceiling. RabbitMQ round-robins tasks
# across them. Override with CELERY_WORKER_COUNT / CELERY_CONCURRENCY env vars.
clear
ulimit -n 100000

# See scripts/run-celery.sh: agents are long-running tasks, so CONCURRENCY caps how
# many pile onto each worker. Spread them across many processes (WORKER_COUNT≈cores)
# with a lower CONCURRENCY so per-step MQTT replies bunch instead of dribbling out.
# WORKER_COUNT*CONCURRENCY must still exceed peak concurrent agents (~1500).
CONCURRENCY="${CELERY_CONCURRENCY:-64}"
WORKER_COUNT="${CELERY_WORKER_COUNT:-32}"
PY="$(dirname "$0")/venv/bin/python"
export PYTHONPATH="$(pwd)"

pids=()
for i in $(seq 1 "$WORKER_COUNT"); do
  "$PY" -m celery -A apps.celery_worker worker \
    --without-gossip --without-mingle --without-heartbeat \
    --pool eventlet --concurrency "$CONCURRENCY" --loglevel WARNING \
    --hostname "OpenRideAsyncService-${i}@$(date +%s)$$" &
  pids+=($!)
done

trap 'kill "${pids[@]}" 2>/dev/null; wait' INT TERM
wait
