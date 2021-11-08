


from __future__ import absolute_import
from apps.worker import app
import time

from apps.assignment_app import AssignmentAgentIndie


@app.task
def start_assignment(**kwargs):

    agent = AssignmentAgentIndie(**kwargs)
    agent.start_listening()
