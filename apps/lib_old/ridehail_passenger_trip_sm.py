from statemachine import State, StateMachine

class RidehailPassengerTripStateMachine(StateMachine):
    ''' '''

    # passenger_online = State('passenger_online', initial=True)
    passenger_requested_trip = State('passenger_requested_trip', initial=True)
    passenger_assigned_trip = State('passenger_assigned_trip')
    passenger_received_trip_confirmation = State('passenger_received_trip_confirmation')
    passenger_accepted_trip = State('passenger_accepted_trip')
    passenger_cancelled_trip = State('passenger_cancelled_trip')
    passenger_moving_for_pickup = State('passenger_moving_for_pickup')
    passenger_waiting_for_pickup = State('passenger_waiting_for_pickup')
    passenger_pickedup = State('passenger_pickedup')
    passenger_moving_for_dropoff = State('passenger_moving_for_dropoff')
    passenger_waiting_for_dropoff = State('passenger_waiting_for_dropoff')
    passenger_droppedoff = State('passenger_droppedoff')
    passenger_completed_trip = State('passenger_completed_trip')


    # request_trip = passenger_online.to(passenger_requested_trip)
    assign = passenger_requested_trip.to(passenger_assigned_trip)
    driver_confirmed_trip = passenger_assigned_trip.to(passenger_received_trip_confirmation)
    # accept = passenger_assigned_trip.to(passenger_accepted_trip)
    accept = passenger_received_trip_confirmation.to(passenger_accepted_trip)
    # deny = passenger_requested_trip.to(passenger_completed_trip)
    reject = passenger_received_trip_confirmation.to(passenger_requested_trip)

    move_for_pickup = passenger_accepted_trip.to(passenger_moving_for_pickup)
    wait_for_pickup = passenger_accepted_trip.to(passenger_waiting_for_pickup) | passenger_moving_for_pickup.to(passenger_waiting_for_pickup)

    driver_cancelled_trip = passenger_requested_trip.from_(passenger_accepted_trip, passenger_moving_for_pickup, passenger_waiting_for_pickup)

    # pickedup = passenger_pickedup.from_(passenger_accepted_trip, passenger_moving_for_pickup, passenger_waiting_for_pickup)
    driver_arrived_for_pickup = passenger_pickedup.from_(passenger_accepted_trip, passenger_moving_for_pickup, passenger_waiting_for_pickup)
    # move_for_dropoff = passenger_pickedup.to(passenger_moving_for_dropoff)
    driver_move_for_dropoff = passenger_pickedup.to(passenger_moving_for_dropoff)
    driver_arrived_for_dropoff = passenger_moving_for_dropoff.to(passenger_waiting_for_dropoff)
    # droppedoff = passenger_moving_for_dropoff.to(passenger_droppedoff) | passenger_waiting_for_dropoff.to(passenger_droppedoff)
    driver_waiting_for_dropoff = passenger_moving_for_dropoff.to(passenger_droppedoff) | passenger_waiting_for_dropoff.to(passenger_droppedoff)
    end_trip = passenger_droppedoff.to(passenger_completed_trip)

    # cancel = passenger_cancelled_trip.from_(passenger_requested_trip, passenger_accepted_trip, passenger_moving_for_pickup, passenger_waiting_for_pickup)
    cancel = passenger_cancelled_trip.from_(passenger_requested_trip,
                                            passenger_received_trip_confirmation,
                                            passenger_accepted_trip,
                                            passenger_moving_for_pickup,
                                            passenger_waiting_for_pickup)

    @classmethod
    def is_moving(cls, state):
        if state in [cls.passenger_requested_trip.identifier,
                    cls.passenger_assigned_trip.identifier,
                    cls.passenger_accepted_trip.identifier,
                    cls.passenger_cancelled_trip.identifier,
                    cls.passenger_waiting_for_dropoff.identifier,
                    cls.passenger_droppedoff.identifier,
                    cls.passenger_completed_trip,
                    ]:
            return False
        else:
            return True


    @classmethod
    def is_driver_channel_open(cls, state):
        if state in [cls.passenger_assigned_trip.identifier,
                    cls.passenger_accepted_trip.identifier,
                    cls.passenger_moving_for_pickup.identifier,
                    cls.passenger_waiting_for_pickup.identifier,
                    cls.passenger_pickedup.identifier,
                    cls.passenger_moving_for_dropoff.identifier,
                    cls.passenger_waiting_for_dropoff.identifier,
                    ]:
            return True
        else:
            return False
