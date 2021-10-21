
from __future__ import absolute_import
from .celery import app
import time


@app.task
def execute_step(agent):
    try:
        agent.step()
    except Exception as e:
        # raise e
        pass

    print ('long time task begins')
    # # sleep 5 seconds
    time.sleep(50)
    print ('long time task finished')
    return True
