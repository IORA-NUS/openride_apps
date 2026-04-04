from statemachine import State, StateMachine


class HaulTripStateMachine(StateMachine):
    created = State("created", initial=True)
    assigned = State("assigned")
    repositioning_to_pickup = State("repositioning_to_pickup")
    queued_for_pickup = State("queued_for_pickup")
    at_pickup_gate = State("at_pickup_gate")
    loaded_in_transit = State("loaded_in_transit")
    queued_for_dropoff = State("queued_for_dropoff")
    at_dropoff_gate = State("at_dropoff_gate")
    completed = State("completed", final=True)
    cancelled = State("cancelled", final=True)

    assign = created.to(assigned)
    start_empty_reposition = assigned.to(repositioning_to_pickup)
    arrive_pickup_queue = (assigned | repositioning_to_pickup).to(queued_for_pickup)
    enter_pickup_gate = queued_for_pickup.to(at_pickup_gate)
    finish_pickup_service = at_pickup_gate.to(loaded_in_transit)
    arrive_dropoff_queue = loaded_in_transit.to(queued_for_dropoff)
    enter_dropoff_gate = queued_for_dropoff.to(at_dropoff_gate)
    finish_dropoff_service = at_dropoff_gate.to(completed)
    cancel = (
        created
        | assigned
        | repositioning_to_pickup
        | queued_for_pickup
        | at_pickup_gate
        | loaded_in_transit
        | queued_for_dropoff
        | at_dropoff_gate
    ).to(cancelled)
