
from __future__ import absolute_import
from celery import Celery
from .config import settings
# import logging

app = Celery('apps',
             broker=f'amqp://{settings["RABBITMQ_ADMIN_USER"]}:{settings["RABBITMQ_ADMIN_PASSWORD"]}@{settings["MQTT_BROKER"]}',
             backend='rpc://',
             include=['apps.tasks'])


if __name__ == "__main__":
    app.start()

