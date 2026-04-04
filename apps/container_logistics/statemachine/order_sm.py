from statemachine import State, StateMachine


class OrderStateMachine(StateMachine):
    created = State("created", initial=True)
    unassigned = State("unassigned")
    assigned = State("assigned")
    pickup_in_progress = State("pickup_in_progress")
    in_transit = State("in_transit")
    dropoff_in_progress = State("dropoff_in_progress")
    completed = State("completed", final=True)
    cancelled = State("cancelled", final=True)

    publish = created.to(unassigned)
    assign = unassigned.to(assigned)
    pickup_started = assigned.to(pickup_in_progress)
    pickup_done = pickup_in_progress.to(in_transit)
    dropoff_started = in_transit.to(dropoff_in_progress)
    deliver = dropoff_in_progress.to(completed)
    cancel = (
        created
        | unassigned
        | assigned
        | pickup_in_progress
        | in_transit
        | dropoff_in_progress
    ).to(cancelled)
