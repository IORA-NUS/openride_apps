


from __future__ import absolute_import
from apps.worker import app

from apps.analytics_app import AnalyticsAgentIndie
from celery.signals import after_setup_task_logger

@app.task
def start_analytics(**kwargs):

    from orsim import ORSimEnv
    from apps.config import messenger_backend
    ORSimEnv.set_backend(messenger_backend)

    agent = AnalyticsAgentIndie(**kwargs)
    agent.start_listening()
