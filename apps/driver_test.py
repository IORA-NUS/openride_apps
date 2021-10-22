from multiprocessing import Pool
import asyncio, time
from driver_app import DriverAgentIndie
import logging


def run_agent(behavior):
    unique_id = behavior[0]
    run_id = behavior[1]
    reference_date = behavior[2]

    agent = DriverAgentIndie(unique_id, run_id, reference_date)
    # for i in range(1, 10):
    #     agent.step(i)

    agent.start_listening()
    # loop = asyncio.get_event_loop()
    # try:
    #     loop.run_forever()
    # except KeyboardInterrupt:
    #     pass
    # finally:
    #     loop.close()



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    behaviors = [
        ('001', 'abcd', '20200101080000'),
        ('002', 'abcd', '20200101080000'),
        ('003', 'abcd', '20200101080000'),
    ]

    with Pool(1) as p:
        print(p.map(run_agent, behaviors))

    # '001', '001', '20200101080000'

    # while True:
    #     time.sleep(10)
