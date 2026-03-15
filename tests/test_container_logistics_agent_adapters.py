# from apps.container_logistics import (
#     ContainerLogisticsActions,
#     FacilityAgentAdapter,
#     HaulierAgentAdapter,
# )


# def _step(adapter):
#     return adapter.process_payload({"action": "step", "time_step": 1})


# def test_runtime_agent_adapters_support_end_to_end_pickup_handoff_flow():
#     facility = FacilityAgentAdapter(facility_id="f1")
#     haulier = HaulierAgentAdapter(haulier_id="h1")

#     facility.process_payload({"action": "init", "time_step": 0})
#     haulier.process_payload({"action": "init", "time_step": 0})

#     haul_request = facility.publish_haul_request()
#     assert haul_request["action"] == ContainerLogisticsActions.HAUL_REQUEST

#     haulier.receive_haul_request()
#     assert haulier.state == "haul_request_received"

#     confirm_msg = haulier.accept_request()
#     facility.enqueue_message(confirm_msg)
#     _step(facility)
#     assert facility.state == "haulier_confirmed"

#     arrive_msg = haulier.arrive_for_pickup()
#     facility.enqueue_message(arrive_msg)
#     _step(facility)
#     assert facility.state == "haulier_arrived"

#     haulier.enqueue_message(facility.validate_pickup_checkin())
#     _step(haulier)
#     assert haulier.state == "pickup_checkin_completed"

#     haulier.enqueue_message(facility.allocate_pickup_slot())
#     _step(haulier)
#     assert haulier.state == "pickup_slot_allocated"

#     haulier.enqueue_message(facility.release_container())
#     _step(haulier)
#     assert haulier.state == "container_picked_up"

#     facility.complete_and_close_interaction()
#     assert facility.state == "interaction_closed"


# def test_runtime_agent_adapters_support_dropoff_completion_events():
#     haulier = HaulierAgentAdapter(haulier_id="h1")
#     haulier.process_payload({"action": "init", "time_step": 0})

#     haulier.receive_haul_request()
#     confirm_msg = haulier.accept_request()
#     pickup_arrival_msg = haulier.arrive_for_pickup()

#     facility = FacilityAgentAdapter(facility_id="f1")
#     facility.process_payload({"action": "init", "time_step": 0})

#     facility.publish_haul_request()
#     facility.enqueue_message(confirm_msg)
#     _step(facility)
#     facility.enqueue_message(pickup_arrival_msg)
#     _step(facility)

#     haulier.enqueue_message(facility.validate_pickup_checkin())
#     _step(haulier)
#     haulier.enqueue_message(facility.allocate_pickup_slot())
#     _step(haulier)
#     haulier.enqueue_message(facility.release_container())
#     _step(haulier)

#     haulier.leave_pickup()
#     assert haulier.state == "enroute_to_dropoff_facility"

#     haulier.arrive_for_dropoff()
#     assert haulier.state == "arrived_at_dropoff_facility"

#     haulier.enqueue_message(facility.emit_dropoff_checkin_ack())
#     _step(haulier)
#     assert haulier.state == "dropoff_checkin_completed"

#     haulier.enqueue_message(facility.emit_dropoff_slot())
#     _step(haulier)
#     assert haulier.state == "dropoff_slot_allocated"

#     haulier.enqueue_message(facility.emit_container_accept())
#     _step(haulier)
#     assert haulier.state == "container_dropped_off"

#     haulier.close_haul()
#     assert haulier.state == "haul_completed"
