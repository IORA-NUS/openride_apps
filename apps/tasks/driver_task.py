


from __future__ import absolute_import
from apps.worker import app
import time

from apps.driver_app import DriverAgentIndie


@app.task
def start_driver(**kwargs):

    from orsim import ORSimEnv
    from apps.config import messenger_backend
    ORSimEnv.set_backend(messenger_backend)

    agent = DriverAgentIndie(**kwargs)
    agent.start_listening()
