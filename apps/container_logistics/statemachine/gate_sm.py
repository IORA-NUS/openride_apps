from statemachine import State, StateMachine


class GateStateMachine(StateMachine):
    closed = State("closed", initial=True)
    available = State("available")
    busy = State("busy")
    out_of_service = State("out_of_service")

    open = closed.to(available)
    assign_truck = available.to(busy)
    complete_service = busy.to(available)
    close = available.to(closed)
    breakdown = out_of_service.from_(closed, available, busy)
    repair = out_of_service.to(closed)
