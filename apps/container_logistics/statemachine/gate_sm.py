from statemachine import State, StateMachine


class GateStateMachine(StateMachine):
    closed = State("closed", initial=True)
    available = State("available")
    busy_pickup = State("busy_pickup")
    busy_dropoff = State("busy_dropoff")
    out_of_service = State("out_of_service")

    open = closed.to(available)
    assign_pickup_truck = available.to(busy_pickup)
    assign_dropoff_truck = available.to(busy_dropoff)
    complete_service = (busy_pickup | busy_dropoff).to(available)
    close = available.to(closed)
    breakdown = (closed | available | busy_pickup | busy_dropoff).to(out_of_service)
    repair = out_of_service.to(closed)
