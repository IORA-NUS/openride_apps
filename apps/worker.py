
from __future__ import absolute_import
from celery import Celery

app = Celery('apps',
             broker='amqp://guest:guest@localhost',
             backend='rpc://',
             include=['apps.tasks'])


if __name__ == "__main__":
    app.start()

