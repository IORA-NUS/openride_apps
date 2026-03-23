
from __future__ import absolute_import
from apps.celery_proj import app
import time

from apps.ride_hail import (
    RideHailAnalyticsAdapter,
    RideHailAssignmentAdapter,
    RideHailDriverAdapter,
    RideHailPassengerAdapter,
)


AGENT_CLASS_MAP = {
    "PassengerAgentIndie": RideHailPassengerAdapter.get_agent_class(),
    "AnalyticsAgentIndie": RideHailAnalyticsAdapter.get_agent_class(),
    "AssignmentAgentIndie": RideHailAssignmentAdapter.get_agent_class(),
    "DriverAgentIndie": RideHailDriverAdapter.get_agent_class(),
}


@app.task
def execute_step(class_name, spec):
    unique_id = spec[0]
    run_id = spec[1]
    reference_date = spec[2]

    agent_cls = AGENT_CLASS_MAP[class_name]
    agent = agent_cls(unique_id, run_id, reference_date, None)

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
