
from statemachine import State, StateMachine

class HLDeliveryDriverTourStateMachine(StateMachine):
    ''' '''

    driver_init_tour = State('driver_init_tour', initial=True)

    driver_assigned_package = State('driver_assigned_package')
    driver_accepted_package = State('driver_accepted_package')
    driver_rejected_package = State('driver_rejected_package')

    driver_confirmed_package_list = State('driver_confirmed_package_list')

    driver_confirmed_tour = State('driver_confirmed_tour')

    driver_moving_to_pickup = State('driver_moving_to_pickup')
    driver_waiting_to_pickup = State('driver_waiting_to_pickup')
    driver_pickup_packages = State('driver_pickup_packages')

    driver_moving_to_deliver = State('driver_moving_to_deliver')
    driver_waiting_to_deliver = State('driver_waiting_to_deliver')
    driver_delivery_successful = State('driver_delivery_successful')
    driver_delivery_failure = State('driver_delivery_failure')

    driver_moving_to_return = State('driver_moving_to_return')
    driver_waiting_to_return = State('driver_waiting_to_return')
    driver_return_packages = State('driver_return_packages')

    driver_completed_tour = State('driver_completed_tour')


    assign = driver_assigned_package.from_(driver_init_tour,
                                            driver_accepted_package,
                                            driver_rejected_package)

    accept = driver_accepted_package.from_(driver_assigned_package)
    reject = driver_rejected_package.from_(driver_assigned_package)

    confirm_package_list = driver_confirmed_package_list.from_(driver_accepted_package,
                                                driver_rejected_package)

    confirm_tour = driver_confirmed_tour.from_(driver_confirmed_package_list)


    move_to_pickup = driver_moving_to_pickup.from_(driver_confirmed_tour,
                                                    driver_pickup_packages,
                                                    driver_delivery_successful,
                                                    driver_delivery_failure,
                                                    )
    wait_to_pickup = driver_moving_to_pickup.to(driver_waiting_to_pickup)
    pickup = driver_waiting_to_pickup.to(driver_pickup_packages)


    move_to_deliver = driver_moving_to_deliver.from_(driver_pickup_packages,
                                                    driver_delivery_successful,
                                                    driver_delivery_failure)
    wait_to_deliver = driver_waiting_to_deliver.from_(driver_moving_to_deliver)
    delivery_success = driver_delivery_successful.from_(driver_waiting_to_deliver)
    delivery_fail = driver_delivery_failure.from_(driver_waiting_to_deliver)


    move_to_return = driver_moving_to_return.from_(driver_pickup_packages,
                                                    driver_delivery_successful,
                                                    driver_delivery_failure,
                                                    driver_return_packages)

    wait_to_return = driver_waiting_to_return.from_(driver_moving_to_return)
    return_packages = driver_waiting_to_return.to(driver_return_packages)

    end_tour = driver_completed_tour.from_(driver_delivery_successful,
                                            driver_return_packages)

