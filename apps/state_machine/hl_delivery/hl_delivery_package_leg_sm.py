from statemachine import State, StateMachine

class HLDeliveryPackageTripStateMachine(StateMachine):
    ''' '''

    package_delivery_requested = State('package_delivery_requested', initial=True)
    package_assigned_driver = State('package_assigned_driver')
    package_waiting_for_pickup = State('package_waiting_for_pickup')
    package_pickedup = State('package_pickedup')
    package_moving_for_dropoff = State('package_moving_for_dropoff')
    package_waiting_for_dropoff = State('package_waiting_for_dropoff')
    package_droppedoff = State('package_droppedoff')
    package_droppedoff_failed = State('package_droppedoff_failed')
    package_completed_leg = State('package_completed_leg')
    package_completed_delivery = State('package_completed_delivery')


    assign = package_delivery_requested.to(package_assigned_driver)
    ready_for_pickup = package_waiting_for_pickup.from_(package_assigned_driver)

    driver_arrived_for_pickup = package_pickedup.from_(package_assigned_driver, package_waiting_for_pickup)
    driver_cancelled_trip = package_delivery_requested.from_(package_waiting_for_pickup)

    driver_move_for_dropoff = package_pickedup.to(package_moving_for_dropoff)
    driver_arrived_for_dropoff = package_moving_for_dropoff.to(package_waiting_for_dropoff)
    dropoff_success = package_waiting_for_dropoff.to(package_droppedoff)
    dropoff_failed = package_waiting_for_dropoff.to(package_droppedoff_failed)
    returning_package = package_droppedoff_failed.to(package_moving_for_dropoff) # Delivery location should change to warehouse in this case.

    end_leg = package_droppedoff.to(package_completed_leg)
    end_delivery = package_completed_leg.to(package_completed_delivery)

    request_next_leg_delivery = package_completed_leg.to(package_delivery_requested)
