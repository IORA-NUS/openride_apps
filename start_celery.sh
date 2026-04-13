
clear
ulimit -n 100000
RANDOM=$(date +%s)

# Default 128: high eventlet concurrency hammers Mongo/API with parallel logins (see TooManyFilesOpen). Override: CELERY_CONCURRENCY=256 ./start_celery.sh
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-128}"

# celery -A apps.worker worker --without-gossip --without-mingle --without-heartbeat --pool eventlet --concurrency 1000 --loglevel WARNING --hostname OpenRideAsyncService@$RANDOM

# celery -A orsim.worker worker --without-gossip --without-mingle --without-heartbeat --pool eventlet --concurrency 1000 --loglevel DEBUG --hostname OpenRideAsyncService@$RANDOM

PYTHONPATH=$(pwd) celery -A orsim.worker worker --without-gossip --without-mingle --without-heartbeat --pool eventlet --concurrency "$CELERY_CONCURRENCY" --loglevel DEBUG --hostname OpenRideAsyncService@$RANDOM
