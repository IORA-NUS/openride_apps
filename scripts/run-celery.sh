#!/usr/bin/env bash
# Celery worker pool (used by openride-celery.service).
set -euo pipefail

cd /home/user/openride_apps
ulimit -n 100000

export PYTHONPATH="/home/user/openride_apps:${PYTHONPATH:-}"
export CELERY_BROKER_URL="${CELERY_BROKER_URL:-amqp://guest:guest@127.0.0.1:5672//}"
# eventlet 0.34's green getaddrinfo rejects pymongo 4.x's `type=` keyword
# (breaks the order-lifecycle agent's Mongo batch writes). All worker traffic is
# localhost, so stdlib (blocking) DNS costs nothing. Must be set pre-import.
export EVENTLET_NO_GREENDNS=yes

# Agents are long-running tasks (they never return), so with prefetch_multiplier=1
# + acks_late each worker pins up to CONCURRENCY agents before RabbitMQ spills to
# the next worker. At CONCURRENCY=128 the ~500-1500 agents pile onto a handful of
# workers; 100+ eventlet greenlets per process then serialise their per-step MQTT
# replies, so the scheduler's collect phase drips out over ~1s. Keep WORKER_COUNT
# at the core count (32) for real parallelism and CONCURRENCY only as high as needed
# for capacity (WORKER_COUNT*CONCURRENCY must exceed peak concurrent agents, ~1500
# here): fewer greenlets/process => replies bunch => steps finish far faster.
CONCURRENCY="${CELERY_CONCURRENCY:-64}"
WORKER_COUNT="${CELERY_WORKER_COUNT:-32}"
PY="${OPENRIDE_PYTHON:-/home/user/openride_apps/venv/bin/python}"

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
