import logging

from config import settings
from sequential_openride_sim_randomised import SequentialOpenRideSimRandomised
from sequential_openride_sim_from_csv import SequentialOpenRideSimFromCSV
from distributed_openride_sim_randomised import DistributedOpenRideSimRandomised

if __name__ == "__main__":

    def run_sim():
        num_drivers =  2 # 2 # 2 #50
        num_passengers =  10 # 10 # 10 #100
        sim = SequentialOpenRideSimRandomised(num_drivers, num_passengers)
        # sim = SequentialOpenRideSimFromCSV("20171201_Dist5")
        # sim = DistributedOpenRideSimRandomised(num_drivers, num_passengers)
        # print(f"{sim.run_id = }")
        for s in range(settings['SIM_DURATION']):
            sim.step()

        logging.info(f"{sim.run_id = }")


    logging.basicConfig(filename='app.log', level=settings['LOG_LEVEL'], filemode='w')
    # logging.basicConfig(level=settings['LOG_LEVEL'])
    run_sim()

