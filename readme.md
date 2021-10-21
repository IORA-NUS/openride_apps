# Instructions

- install requirements.txt


## Running with Celery:
- set 'EXECUTION_STRATEGY': 'CELERY'
- set 'CONCURRENCY_STRATEGY': 'EVENTLET'
- Navigate to the root folder one containing the apps folder and run celery task server

`celery -A apps.celery_proj worker -P eventlet -c 1000 -l INFO`

Note: to shutdown celery task server run

`pkill -9 -f 'apps.celery_proj worker'`

- Navigate into apps folder `cd apps` and run

`python distributed_openride_sim_randomised.py`


## Running with multiprocessing:
- set 'EXECUTION_STRATEGY': 'MULTIPROCESSING'
- set 'CONCURRENCY_STRATEGY': 'ASYNCIO'
- Navigate into apps folder `cd apps` and run

`python distributed_openride_sim_randomised.py`


