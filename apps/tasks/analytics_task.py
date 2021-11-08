


from __future__ import absolute_import
from apps.worker import app
# import time, logging

from apps.analytics_app import AnalyticsAgentIndie
from celery.signals import after_setup_task_logger

@app.task
def start_analytics(**kwargs):

    agent = AnalyticsAgentIndie(**kwargs)
    agent.start_listening()
