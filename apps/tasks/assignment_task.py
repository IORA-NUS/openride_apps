


from __future__ import absolute_import
from apps.worker import app
import time

from apps.assignment_app import AssignmentAgentIndie


@app.task
def start_assignment(**kwargs):
    # unique_id = spec[0]
    # run_id = spec[1]
    # reference_date = spec[2]

    # agent = AssignmentAgentIndie(unique_id, run_id, reference_date, None)
    agent = AssignmentAgentIndie(**kwargs)

    agent.start_listening()
    # try:
    #     agent.step()
    # except Exception as e:
    #     # raise e
    #     pass

    # print ('long time task begins')
    # # # sleep 5 seconds
    # time.sleep(50)
    # print ('long time task finished')
    # return True