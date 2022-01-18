
clear
RANDOM=$(date +%s)

# celery -A apps.worker worker --without-gossip --without-mingle --without-heartbeat --pool eventlet --concurrency 1000 --loglevel WARNING --hostname OpenRideAsyncService@$RANDOM

celery -A orsim.worker worker --without-gossip --without-mingle --without-heartbeat --pool eventlet --concurrency 1000 --loglevel WARNING --hostname OpenRideAsyncService@$RANDOM
