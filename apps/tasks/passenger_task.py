


from __future__ import absolute_import
from apps.worker import app
import time

from apps.passenger_app import PassengerAgentIndie


@app.task
def start_passenger(**kwargs):

    agent = PassengerAgentIndie(**kwargs)
    agent.start_listening()
