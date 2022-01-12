
from .config import settings, messenger_backend
# import logging

## Broker settings.
# broker_url = f'amqp://{settings["RABBITMQ_ADMIN_USER"]}:{settings["RABBITMQ_ADMIN_PASSWORD"]}@{settings["MQTT_BROKER"]}'
broker_url = f'amqp://{messenger_backend["RABBITMQ_ADMIN_USER"]}:{messenger_backend["RABBITMQ_ADMIN_PASSWORD"]}@{messenger_backend["MQTT_BROKER"]}'

## Disable result backent and also ignore results.
# result_backend = 'rpc://'
task_ignore_result = True

# List of modules to import when the Celery worker starts.
imports = ('apps.tasks',)

