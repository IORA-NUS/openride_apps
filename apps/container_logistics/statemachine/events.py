class ContainerLogisticsActions:
    ASSIGNED_HAUL_TRIP = "assigned_haul_trip"
    ORDER_WORKFLOW_EVENT = "order_workflow_event"
    FACILITY_WORKFLOW_EVENT = "facility_workflow_event"


class ContainerLogisticsEvents:
    ORDER_ASSIGNED_TO_TRUCK = "order_assigned_to_truck"
    ORDER_PICKUP_STARTED = "order_pickup_started"
    ORDER_PICKUP_COMPLETED = "order_pickup_completed"
    ORDER_DROPOFF_STARTED = "order_dropoff_started"
    ORDER_DELIVERED = "order_delivered"
    ORDER_CANCELLED = "order_cancelled"

    TRUCK_ARRIVED_PICKUP_QUEUE = "truck_arrived_pickup_queue"
    TRUCK_ARRIVED_DROPOFF_QUEUE = "truck_arrived_dropoff_queue"
    GATE_SLOT_ASSIGNED_FOR_PICKUP = "gate_slot_assigned_for_pickup"
    GATE_SLOT_ASSIGNED_FOR_DROPOFF = "gate_slot_assigned_for_dropoff"
    PICKUP_GATE_SERVICE_COMPLETED = "pickup_gate_service_completed"
    DROPOFF_GATE_SERVICE_COMPLETED = "dropoff_gate_service_completed"
    FACILITY_QUEUE_CANCELLED = "facility_queue_cancelled"
