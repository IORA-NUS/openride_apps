


from __future__ import absolute_import
from apps.celery_proj import app
import time

from apps.passenger_app import PassengerAgentIndie


@app.task
def start_passenger(spec):
    unique_id = spec[0]
    run_id = spec[1]
    reference_date = spec[2]

    agent = PassengerAgentIndie(unique_id, run_id, reference_date, None)

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
