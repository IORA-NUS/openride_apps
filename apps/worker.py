
from __future__ import absolute_import
from celery import Celery

app = Celery('apps',
            #  broker='amqp://guest:guest@localhost',
             broker='amqp://test:test@192.168.10.115',
             backend='rpc://',
             include=['apps.tasks'])


if __name__ == "__main__":
    app.start()

