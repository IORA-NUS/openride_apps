class ContainerLogisticsActions:
    HAUL_REQUEST = "haul_request"
    HAULIER_WORKFLOW_EVENT = "haulier_workflow_event"
    FACILITY_WORKFLOW_EVENT = "facility_workflow_event"


class ContainerLogisticsEvents:
    PUBLISH_HAUL_REQUEST = "publish_haul_request"
    HAULIER_ACCEPTS_REQUEST = "haulier_accepts_request"
    ARRIVE_FOR_PICKUP = "arrive_for_pickup"
    ARRIVE_FOR_DROPOFF = "arrive_for_dropoff"

    FACILITY_ACKNOWLEDGES_PICKUP_CHECKIN = "facility_acknowledges_pickup_checkin"
    FACILITY_ALLOCATES_PICKUP_SLOT = "facility_allocates_pickup_slot"
    FACILITY_RELEASES_CONTAINER = "facility_releases_container"

    FACILITY_ACKNOWLEDGES_DROPOFF_CHECKIN = "facility_acknowledges_dropoff_checkin"
    FACILITY_ALLOCATES_DROPOFF_SLOT = "facility_allocates_dropoff_slot"
    FACILITY_ACCEPTS_CONTAINER = "facility_accepts_container"

    CANCEL_HAUL = "cancel_haul"
    CANCEL_INTERACTION = "cancel_interaction"
