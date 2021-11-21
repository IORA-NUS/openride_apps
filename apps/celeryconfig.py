
from .config import settings
# import logging

## Broker settings.
broker_url = f'amqp://{settings["RABBITMQ_ADMIN_USER"]}:{settings["RABBITMQ_ADMIN_PASSWORD"]}@{settings["MQTT_BROKER"]}'

## Disable result backent and also ignore results. 
# result_backend = 'rpc://'
task_ignore_result = True

# List of modules to import when the Celery worker starts.
imports = ('apps.tasks',)

