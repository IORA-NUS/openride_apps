


from __future__ import absolute_import
from apps.worker import app
import time

from apps.passenger_app import PassengerAgentIndie


@app.task
def start_passenger(**kwargs):

    from orsim import ORSimEnv
    from apps.config import messenger_backend
    ORSimEnv.set_backend(messenger_backend)

    agent = PassengerAgentIndie(**kwargs)
    agent.start_listening()
