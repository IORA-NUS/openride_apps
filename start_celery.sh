
clear
RANDOM=$(date +%s)

celery -A apps.worker worker -P eventlet -c 1000 -l INFO -n OpenRideAsyncService@$RANDOM
