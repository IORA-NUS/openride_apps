
from statemachine import State, StateMachine

class RidehailDriverTripStateMachine(StateMachine):
    ''' '''

    driver_init_trip = State('driver_init_trip', initial=True)
    driver_looking_for_job = State('driver_looking_for_job')
    driver_received_trip = State('driver_received_trip') # alternate Initial State
    driver_rejected_trip = State('driver_rejected_trip')
    driver_accepted_trip = State('driver_accepted_trip')
    driver_cancelled_trip = State('driver_cancelled_trip')
    driver_moving_to_pickup = State('driver_moving_to_pickup')
    driver_waiting_to_pickup = State('driver_waiting_to_pickup')
    driver_pickedup = State('driver_pickedup')
    driver_moving_to_dropoff = State('driver_moving_to_dropoff')
    driver_waiting_to_dropoff = State('driver_waiting_to_dropoff')
    driver_droppedoff = State('driver_droppedoff')
    driver_completed_trip = State('driver_completed_trip')


    look_for_job = driver_init_trip.to(driver_looking_for_job)
    recieve = driver_init_trip.to(driver_received_trip)
    confirm = driver_received_trip.to(driver_accepted_trip)
    passenger_confirmed_trip = driver_accepted_trip.to(driver_moving_to_pickup)

    wait_to_pickup = driver_moving_to_pickup.to(driver_waiting_to_pickup)

    passenger_cancelled_trip = driver_completed_trip.from_(driver_accepted_trip, driver_moving_to_pickup, driver_waiting_to_pickup)

    passenger_acknowledge_pickup = driver_pickedup.from_(driver_waiting_to_pickup)
    move_to_dropoff = driver_pickedup.to(driver_moving_to_dropoff)
    wait_to_dropoff = driver_moving_to_dropoff.to(driver_waiting_to_dropoff)

    # passenger_acknowledge_dropoff = driver_moving_to_dropoff.to(driver_droppedoff) | driver_waiting_to_dropoff.to(driver_droppedoff)
    passenger_acknowledge_dropoff = driver_waiting_to_dropoff.to(driver_droppedoff)
    end_trip = driver_completed_trip.from_(driver_droppedoff, driver_init_trip, driver_looking_for_job, driver_rejected_trip)

    reject = driver_rejected_trip.from_(driver_received_trip)
    cancel = driver_cancelled_trip.from_(driver_accepted_trip, driver_moving_to_pickup, driver_waiting_to_pickup, driver_looking_for_job)

    force_quit = driver_cancelled_trip.from_(
                            driver_init_trip,
                            driver_looking_for_job,
                            driver_received_trip,
                            driver_rejected_trip,
                            driver_accepted_trip,
                            driver_moving_to_pickup,
                            driver_waiting_to_pickup,
                            driver_pickedup,
                            driver_moving_to_dropoff,
                            driver_waiting_to_dropoff,
                            driver_droppedoff,
                        )

    @classmethod
    def is_moving(cls, state):
        if state  in [cls.driver_waiting_to_pickup.identifier, cls.driver_waiting_to_dropoff.identifier]:
            return False
        else:
            return True

    @classmethod
    def is_passenger_channel_open(cls, state):
        if state in [cls.driver_received_trip.identifier,
                    cls.driver_accepted_trip.identifier,
                    cls.driver_moving_to_pickup.identifier,
                    cls.driver_waiting_to_pickup.identifier,
                    cls.driver_pickedup.identifier,
                    cls.driver_moving_to_dropoff.identifier,
                    cls.driver_waiting_to_dropoff.identifier,
                    ]:
            return True
        else:
            return False

    def on_end_trip(self, doc=None):
        if doc is not None:
            doc['is_active'] = False

    def on_reject(self, doc=None):
        if doc is not None:
            doc['is_active'] = False

    def on_cancel(self, doc=None):
        if doc is not None:
            doc['is_active'] = False
