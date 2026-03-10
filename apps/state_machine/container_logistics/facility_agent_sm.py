from statemachine import State, StateMachine


class FacilityAgentInteractionStateMachine(StateMachine):
    """Facility-side interaction workflow for haulier handoffs."""

    facility_idle = State("facility_idle", initial=True)
    haul_request_announced = State("haul_request_announced")
    haulier_confirmed = State("haulier_confirmed")
    haulier_arrived = State("haulier_arrived")
    checkin_validated = State("checkin_validated")
    handling_slot_allocated = State("handling_slot_allocated")
    handoff_in_progress = State("handoff_in_progress")
    handoff_completed = State("handoff_completed")
    interaction_closed = State("interaction_closed")
    interaction_cancelled = State("interaction_cancelled")

    publish_haul_request = haul_request_announced.from_(facility_idle)
    receive_haulier_confirmation = haulier_confirmed.from_(haul_request_announced)
    receive_haulier_arrival = haulier_arrived.from_(haulier_confirmed)

    validate_checkin = checkin_validated.from_(haulier_arrived)
    allocate_handling_slot = handling_slot_allocated.from_(checkin_validated)
    start_handoff = handoff_in_progress.from_(handling_slot_allocated)
    complete_handoff = handoff_completed.from_(handoff_in_progress)

    close_interaction = interaction_closed.from_(handoff_completed)
    reset = facility_idle.from_(interaction_closed, interaction_cancelled)

    cancel_interaction = interaction_cancelled.from_(
        haul_request_announced,
        haulier_confirmed,
        haulier_arrived,
        checkin_validated,
        handling_slot_allocated,
        handoff_in_progress,
    )


class PortAgentInteractionStateMachine(FacilityAgentInteractionStateMachine):
    pass


class DepotAgentInteractionStateMachine(FacilityAgentInteractionStateMachine):
    pass


class WarehouseAgentInteractionStateMachine(FacilityAgentInteractionStateMachine):
    pass
