"""
Container Logistics Events and Actions
Event constants for state machine transitions, following the ride_hail pattern.
"""

# Haul Trip Events
HAULTRIP_CREATED = "haultrip_created"
HAULTRIP_ASSIGNED = "haultrip_assigned"
HAULTRIP_EN_ROUTE_TO_PICKUP = "haultrip_en_route_to_pickup"
HAULTRIP_WAITING_AT_PICKUP = "haultrip_waiting_at_pickup"
HAULTRIP_AT_PICKUP_GATE = "haultrip_at_pickup_gate"
HAULTRIP_LOADING = "haultrip_loading"
HAULTRIP_LOADED = "haultrip_loaded"
HAULTRIP_EN_ROUTE_TO_DROPOFF = "haultrip_en_route_to_dropoff"
HAULTRIP_WAITING_AT_DROPOFF = "haultrip_waiting_at_dropoff"
HAULTRIP_AT_DROPOFF_GATE = "haultrip_at_dropoff_gate"
HAULTRIP_UNLOADING = "haultrip_unloading"
HAULTRIP_COMPLETED = "haultrip_completed"
HAULTRIP_CANCELLED = "haultrip_cancelled"

# Truck Workflow Events
TRUCK_OFF_DUTY = "truck_off_duty"
TRUCK_ON_DUTY = "truck_on_duty"
TRUCK_RESTING = "truck_resting"

# Facility Gate Events
GATE_CLOSED = "gate_closed"
GATE_AVAILABLE = "gate_available"
GATE_OCCUPIED = "gate_occupied"
GATE_SERVICE_IN_PROGRESS = "gate_service_in_progress"
GATE_SERVICE_COMPLETE = "gate_service_complete"
GATE_UNAVAILABLE = "gate_unavailable"

# Order Events
ORDER_CREATED = "order_created"
ORDER_IN_MARKET = "order_in_market"
ORDER_ASSIGNED = "order_assigned"
ORDER_COMPLETED = "order_completed"
ORDER_EXPIRED = "order_expired"
ORDER_CANCELLED = "order_cancelled"

