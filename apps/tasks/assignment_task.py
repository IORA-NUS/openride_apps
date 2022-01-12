


from __future__ import absolute_import
from apps.worker import app
import time

from apps.assignment_app import AssignmentAgentIndie


@app.task
def start_assignment(**kwargs):

    from orsim import ORSimEnv
    from apps.config import messenger_backend
    ORSimEnv.set_backend(messenger_backend)

    agent = AssignmentAgentIndie(**kwargs)
    agent.start_listening()
