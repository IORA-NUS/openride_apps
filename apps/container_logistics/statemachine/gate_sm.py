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
    # Compatibility: avoid `State | State` unions, use from_ for multi-source transitions.
    complete_service = available.from_(busy_pickup, busy_dropoff)
    close = available.to(closed)
    breakdown = out_of_service.from_(closed, available, busy_pickup, busy_dropoff)
    repair = out_of_service.to(closed)
