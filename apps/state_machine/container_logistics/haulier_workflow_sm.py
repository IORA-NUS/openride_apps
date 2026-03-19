from statemachine import State, StateMachine


class HaulierContainerWorkflowStateMachine(StateMachine):
    """Haulier workflow focused on interactions with facility agents."""

    haulier_idle = State("haulier_idle", initial=True)
    haul_request_received = State("haul_request_received")
    haul_request_accepted = State("haul_request_accepted")

    enroute_to_pickup_facility = State("enroute_to_pickup_facility")
    arrived_at_pickup_facility = State("arrived_at_pickup_facility")
    pickup_checkin_completed = State("pickup_checkin_completed")
    pickup_slot_allocated = State("pickup_slot_allocated")
    container_picked_up = State("container_picked_up")
    departed_pickup_facility = State("departed_pickup_facility")

    enroute_to_dropoff_facility = State("enroute_to_dropoff_facility")
    arrived_at_dropoff_facility = State("arrived_at_dropoff_facility")
    dropoff_checkin_completed = State("dropoff_checkin_completed")
    dropoff_slot_allocated = State("dropoff_slot_allocated")
    container_dropped_off = State("container_dropped_off")

    haul_completed = State("haul_completed", final=True)
    haul_cancelled = State("haul_cancelled", final=True)

    facility_publishes_request = haul_request_received.from_(haulier_idle)
    haulier_accepts_request = haul_request_accepted.from_(haul_request_received)

    drive_to_pickup = enroute_to_pickup_facility.from_(haul_request_accepted)
    arrive_for_pickup = arrived_at_pickup_facility.from_(enroute_to_pickup_facility)
    facility_acknowledges_pickup_checkin = pickup_checkin_completed.from_(arrived_at_pickup_facility)
    facility_allocates_pickup_slot = pickup_slot_allocated.from_(pickup_checkin_completed)
    facility_releases_container = container_picked_up.from_(pickup_slot_allocated)
    leave_pickup = departed_pickup_facility.from_(container_picked_up)

    drive_to_dropoff = enroute_to_dropoff_facility.from_(departed_pickup_facility)
    arrive_for_dropoff = arrived_at_dropoff_facility.from_(enroute_to_dropoff_facility)
    facility_acknowledges_dropoff_checkin = dropoff_checkin_completed.from_(arrived_at_dropoff_facility)
    facility_allocates_dropoff_slot = dropoff_slot_allocated.from_(dropoff_checkin_completed)
    facility_accepts_container = container_dropped_off.from_(dropoff_slot_allocated)

    close_haul = haul_completed.from_(container_dropped_off)
    # reset = haulier_idle.from_(haul_completed, haul_cancelled)
    # Removed reset from final states to comply with statemachine rules

    cancel_haul = haul_cancelled.from_(
        haul_request_received,
        haul_request_accepted,
        enroute_to_pickup_facility,
        arrived_at_pickup_facility,
        pickup_checkin_completed,
        pickup_slot_allocated,
        container_picked_up,
        departed_pickup_facility,
        enroute_to_dropoff_facility,
        arrived_at_dropoff_facility,
        dropoff_checkin_completed,
        dropoff_slot_allocated,
    )
