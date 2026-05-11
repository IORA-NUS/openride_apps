truck_settings = {
    "num_trucks": 20,
    "steps_per_action": 1,
    "response_rate": 1.0,
    "step_only_on_events": True,
    "profile": {
        # Workflow lifecycle consumed by TruckAgent/TruckApp.
        "default_shift_start_time": 0,
        "default_shift_end_time": 4 * 60 * 60,
        # HaulTrip StateMachine steering.
        "idle_strategy": "stay_if_no_assignment",
        "cancel_probability_when_assigned": 0.0,
        "cancel_probability_in_queue": 0.0,
        "truck_size": "20ft", # 20ft, 40ft, 2x20ft
        "haulier_name": "Haulier", # Haulier name
        "restricted_areas": ["West Coast", "MBS"], # West Coast, MBS, etc.
        # Bounds for seeding profile estimated times (actual values live on each truck profile).
        "min_estimated_time_to_pickup": 300,
        "max_estimated_time_to_pickup": 1800,
        "min_estimated_time_to_dropoff": 600,
        "max_estimated_time_to_dropoff": 3600,
    },
}

order_settings = {
    "num_orders": 200,
    "steps_per_action": 1,
    "response_rate": 1.0,
    "step_only_on_events": True,
    "profile": {
        # Order -> HaulTrip shared fields expected by trip creation.
        # If generators do not provide explicit routes yet, allow None.
        "require_planned_routes": False,
        "cancel_probability_before_assignment": 0.0,
    },
}

facility_settings = {
    "num_facilities": 2,
    "steps_per_action": 1,
    "response_rate": 1.0,
    "step_only_on_events": True,
    "profile": {
        # FacilityQueueController + GateStateMachine tuning.
        "fifo_queue_policy": True,
        "gate_count": 2,
        "pickup_service_time": 120,
        "dropoff_service_time": 120,
        "max_queue_size": None,
        "facility_type": "Depo", # depo, warehouse, port
        "status": "Open", # Open, Closed
        "operating_hours": "24/7", # 24/7, 9-5, etc.
        "operating_days": "7 days a week", # 7 days a week, 5 days a week, etc.
        "facilities": [
            {
                "name": "pickup_terminal_a",
                "gate_count": 2,
                "pickup_service_time": 120,
                "dropoff_service_time": 120,
            },
            {
                "name": "dropoff_terminal_b",
                "gate_count": 2,
                "pickup_service_time": 120,
                "dropoff_service_time": 120,
            },
        ],
    },
}

assignment_settings = {
    "steps_per_action": 1,
    "response_rate": 1.0,
    "step_only_on_events": False,
    "profile": {
        "strategy": "GreedyMinPickupMatching",
        "max_travel_time_pickup": 7200,
        "online_metric_scale_strategy": "time",
        "respect_truck_online_state": True,
        "reject_if_active_haul_trip": True,
    },
}

analytics_settings = {
    "steps_per_action": 1,
    "response_rate": 1.0,
    "step_only_on_events": False,
    "profile": {
        "publish_realtime_data": False,
        "write_ws_output_to_file": True,
        "publish_paths_history": False,
        "write_ph_output_to_file": False,
        "paths_history_time_window": 1800,
    },
}