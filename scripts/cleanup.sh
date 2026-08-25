#!/usr/bin/env bash
systemctl --user daemon-reload

# Stop any running containers so prune can reclaim them
running=$(docker ps -q)
if [[ -n "$running" ]]; then
  echo "Stopping running containers…"
  docker stop $running
fi

docker system prune -af
pkill -f 'celery -A worker orsim.worker'
