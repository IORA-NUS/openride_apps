# Instructions

- The following codebase is developed on Python 3.8.5. It should run on python > 3.7, but has not been tested.
- Create a Vitrual environment and activate it.
- install requirements.txt
- The package assumes the following are installed and accessivle over the network (or localhost)

## Prerequisites:
1. Routing Service: Currently only supports [OSRM](http://project-osrm.org). The instructions for downloading open source routing datasets are provided in the link.
   The codes have been tested with the "malaysia-singapore-brunei" Open Street map data set with OSRM service running on docker. If OSRM is not installed on the localhost, apprpriately modify the `ROUTING_SERVER` value in `config.py`
2. [RabbitMQ](https://www.rabbitmq.com) is the Message broker of choice. Ensure the following details are appropriately modified in the `config.py`
    - `RABBITMQ_MANAGEMENT_SERVER`
    - `RABBITMQ_ADMIN_USER`
    - `RABBITMQ_ADMIN_PASSWORD`
    - `MQTT_BROKER`
    - `WEB_MQTT_PORT`
3. Ensure the [OpenRide Server](https://github.com/IORA-NUS/openride_server) is running and provide the server url in the `OPENRIDE_SERVER_URL`.
   1. OpenRIde Server depends on [MongoDB](https://www.mongodb.com) as the data backend. Details on running the OpenRide Server is given in that repo.


## Running with Celery:
- set `EXECUTION_STRATEGY`: `CELERY`
- set `CONCURRENCY_STRATEGY`: `EVENTLET`
- Navigate to the root folder containing the apps folder and the shell scripts.
- To start Celery task manager, run

`./start_celery.sh`

- NOTE: Multiple celery workers can be executed at the same time (this provides extention for running the simulation in clusters). To run multiple worker pools, run the start script multiple times.
- To shutdown celery task manager, run

`./stop_celery.sh`

- NOTE: The stop script shuts down all celety tasks running on a device. If the workers are running on multiple machines, please run the stop script on each of them separately.
- To Start the simulation, run

`./start_simulation.sh`

