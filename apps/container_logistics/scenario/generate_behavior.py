from random import choice, randint, uniform

from shapely.geometry import Point, mapping

from apps.orsim_config import orsim_settings

from .scenario_config import (
    analytics_settings,
    assignment_settings,
    facility_settings,
    order_settings,
    truck_settings,
)


class GenerateBehavior:
    """Behavior factory for container logistics simulation actors."""

    _default_facility_centers = [
        {"name": "pickup_terminal_a", "lat": 1.290, "lon": 103.850},
        {"name": "dropoff_terminal_b", "lat": 1.330, "lon": 103.930},
    ]

    @classmethod
    def _get_facilities(cls):
        facilities = facility_settings.get("profile", {}).get("facilities", [])
        if facilities:
            resolved = []
            for idx, item in enumerate(facilities):
                resolved.append(
                    {
                        "name": item.get("name", f"facility_{idx:03d}"),
                        "lat": item.get("lat", cls._default_facility_centers[idx % len(cls._default_facility_centers)]["lat"]),
                        "lon": item.get("lon", cls._default_facility_centers[idx % len(cls._default_facility_centers)]["lon"]),
                        "gate_count": item.get("gate_count", facility_settings.get("profile", {}).get("gate_count", 1)),
                        "pickup_service_time": item.get(
                            "pickup_service_time", facility_settings.get("profile", {}).get("pickup_service_time", 0)
                        ),
                        "dropoff_service_time": item.get(
                            "dropoff_service_time", facility_settings.get("profile", {}).get("dropoff_service_time", 0)
                        ),
                    }
                )
            return resolved
        return list(cls._default_facility_centers)

    @classmethod
    def _random_location_near(cls, center, jitter=0.01):
        lon = center["lon"] + uniform(-jitter, jitter)
        lat = center["lat"] + uniform(-jitter, jitter)
        return mapping(Point(lon, lat))

    @classmethod
    def _simulation_end_step(cls):
        return max(0, orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 1) - 1)

    @classmethod
    def container_truck(cls, agent_id, record=None):
        facilities = cls._get_facilities()
        simulation_end = cls._simulation_end_step()
        profile_cfg = truck_settings.get("profile", {})
        truck_tp = truck_settings.get("profile", {})

        if record is None:
            home_facility = choice(facilities)
            shift_start_time = profile_cfg.get("default_shift_start_time", 0)
            shift_end_time = min(profile_cfg.get("default_shift_end_time", simulation_end), simulation_end)
            init_loc = cls._random_location_near(home_facility)
            eta_pickup = randint(
                truck_tp.get("min_estimated_time_to_pickup", 300),
                truck_tp.get("max_estimated_time_to_pickup", 1800),
            )
            eta_dropoff = randint(
                truck_tp.get("min_estimated_time_to_dropoff", 600),
                truck_tp.get("max_estimated_time_to_dropoff", 3600),
            )
        else:
            shift_start_time = record.get("shift_start_time", 0)
            shift_end_time = record.get("shift_end_time", simulation_end)
            if "start_lon" in record and "start_lat" in record:
                init_loc = mapping(Point(record["start_lon"], record["start_lat"]))
                home_facility = choice(facilities)
            else:
                home_facility = choice(facilities)
                init_loc = cls._random_location_near(home_facility)
            eta_pickup = record.get(
                "estimated_time_to_pickup",
                randint(
                    truck_tp.get("min_estimated_time_to_pickup", 300),
                    truck_tp.get("max_estimated_time_to_pickup", 1800),
                ),
            )
            eta_dropoff = record.get(
                "estimated_time_to_dropoff",
                randint(
                    truck_tp.get("min_estimated_time_to_dropoff", 600),
                    truck_tp.get("max_estimated_time_to_dropoff", 3600),
                ),
            )

        others = [f for f in facilities if f is not home_facility]
        idle_facility = choice(others) if others else home_facility
        empty_dest_loc = mapping(Point(idle_facility["lon"], idle_facility["lat"]))
        rec = record or {}
        planned_repo = rec.get("planned_reposition_route")
        planned_drop = rec.get("planned_dropoff_route")

        _bounds_keys = {
            "min_estimated_time_to_pickup",
            "max_estimated_time_to_pickup",
            "min_estimated_time_to_dropoff",
            "max_estimated_time_to_dropoff",
        }
        base_profile = {k: v for k, v in profile_cfg.items() if k not in _bounds_keys}

        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {
                "role": "truck",
                "domain": orsim_settings.get("DOMAIN"),
            },
            "steps_per_action": truck_settings.get("steps_per_action", 1),
            "response_rate": truck_settings.get("response_rate", 1.0),
            "step_only_on_events": truck_settings.get("step_only_on_events", True),
            "shift_start_time": shift_start_time,
            "shift_end_time": shift_end_time,
            "init_loc": init_loc,
            #"empty_dest_loc": empty_dest_loc,
            "profile": {
                **base_profile,
                "home_facility_name": home_facility.get("name"),
                "truck_size": profile_cfg.get("truck_size", "20ft"),
                "haulier_name": profile_cfg.get("haulier_name", "Haulier"),
                "restricted_areas": profile_cfg.get("restricted_areas", ["West Coast", "MBS"]),
                "planned_reposition_route": planned_repo,
                "planned_dropoff_route": planned_drop,
                "estimated_time_to_pickup": eta_pickup,
                "estimated_time_to_dropoff": eta_dropoff,
            },
        }

    @classmethod
    def container_order(cls, agent_id, record=None):
        facilities = cls._get_facilities()
        profile_cfg = order_settings.get("profile", {})

        if len(facilities) < 2:
            pickup_facility = facilities[0]
            dropoff_facility = facilities[0]
        else:
            pickup_facility = facilities[0]
            dropoff_facility = facilities[1]

        if record is None:
            request_time = randint(0, cls._simulation_end_step())
            pickup_loc = cls._random_location_near(pickup_facility)
            dropoff_loc = cls._random_location_near(dropoff_facility)
        else:
            request_time = record.get("request_time_step", 0)
            pickup_loc = mapping(Point(record["pickup_lon"], record["pickup_lat"]))
            dropoff_loc = mapping(Point(record["dropoff_lon"], record["dropoff_lat"]))

        pickup_service_time = profile_cfg.get("pickup_service_time", facility_settings.get("profile", {}).get("pickup_service_time", 120))
        dropoff_service_time = profile_cfg.get("dropoff_service_time", facility_settings.get("profile", {}).get("dropoff_service_time", 120))

        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {
                "role": "order",
                "domain": orsim_settings.get("DOMAIN"),
            },
            "steps_per_action": order_settings.get("steps_per_action", 1),
            "response_rate": order_settings.get("response_rate", 1.0),
            "step_only_on_events": order_settings.get("step_only_on_events", True),
            "request_time_step": request_time,
            "pickup_loc": pickup_loc,
            "dropoff_loc": dropoff_loc,
            "pickup_facility": pickup_facility,
            "dropoff_facility": dropoff_facility,
            "pickup_service_time": pickup_service_time,
            "dropoff_service_time": dropoff_service_time,
            "order_size": profile_cfg.get("order_size", "1x20"),
            "order_type": profile_cfg.get("order_type", "import"), # import, export
            "vessel_number": profile_cfg.get("vessel_number", "Vessel123"), # vessel number which can introduce discount windows
            "shipping_line": profile_cfg.get("shipping_line", "Shipping Line"), # container owner
            "order_owner": profile_cfg.get("order_owner", ""),
            "pickup_location_type": profile_cfg.get("pickup_location_type", "Depo"), # depo, warehouse, port
            "dropoff_location_type": profile_cfg.get("dropoff_location_type", "Warehouse"), # depo, warehouse, port
            "container_status": profile_cfg.get("container_status", "Empty"), # Empty, Loaded
            # Planned routes and leg ETAs are carried on the assigned truck profile (see container_truck).
            "profile": {
                **profile_cfg,
                "pickup_facility_name": pickup_facility.get("name"),
                "dropoff_facility_name": dropoff_facility.get("name"),
                "pickup_loc": pickup_loc,
                "dropoff_loc": dropoff_loc,
                "pickup_service_time": pickup_service_time,
                "dropoff_service_time": dropoff_service_time,
            },
        }

    @classmethod
    def container_facility(cls, agent_id, facility_index=0, record=None):
        facilities = cls._get_facilities()
        profile_cfg = facility_settings.get("profile", {})
        facility = facilities[facility_index % len(facilities)]

        gate_count = facility.get("gate_count", profile_cfg.get("gate_count", 1))
        pickup_service_time = facility.get("pickup_service_time", profile_cfg.get("pickup_service_time", 0))
        dropoff_service_time = facility.get("dropoff_service_time", profile_cfg.get("dropoff_service_time", 0))

        if record is not None:
            gate_count = record.get("gate_count", gate_count)
            pickup_service_time = record.get("pickup_service_time", pickup_service_time)
            dropoff_service_time = record.get("dropoff_service_time", dropoff_service_time)

        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {
                "role": "facility",
                "domain": orsim_settings.get("DOMAIN"),
            },
            "steps_per_action": facility_settings.get("steps_per_action", 1),
            "response_rate": facility_settings.get("response_rate", 1.0),
            "step_only_on_events": facility_settings.get("step_only_on_events", True),
            # Top-level keys are required by FacilityApp.runtime_behavior_schema.
            "gate_count": gate_count,
            "pickup_service_time": pickup_service_time,
            "dropoff_service_time": dropoff_service_time,
            "profile": {
                **profile_cfg,
                "name": facility.get("name"),
                "facility_type": facility_tp.get("facility_type", "Depo"), # depo, warehouse, port
                "location": mapping(Point(facility["lon"], facility["lat"])),
                "gate_count": gate_count,
                "max_queue_size": profile_cfg.get("max_queue_size", None),
                "status": profile_cfg.get("status", "Open"), # Open, Closed
                "operating_hours": profile_cfg.get("operating_hours", "24/7"), # 24/7, 9-5, etc.
                "operating_days": profile_cfg.get("operating_days", "7 days a week"), # 7 days a week, 5 days a week, etc.
                "pickup_service_time": pickup_service_time,
                "dropoff_service_time": dropoff_service_time,
            },
        }

    @classmethod
    def container_assignment(cls, agent_id, record=None):
        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {
                "role": "engine",
                "domain": orsim_settings.get("DOMAIN"),
            },
            "steps_per_action": assignment_settings.get("steps_per_action", 1),
            "response_rate": assignment_settings.get("response_rate", 1.0),
            "step_only_on_events": assignment_settings.get("step_only_on_events", False),
            "profile": assignment_settings.get("profile", {}),

        }

    @classmethod
    def container_analytics(cls, agent_id, record=None):
        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {
                "role": "analytics",
                "domain": orsim_settings.get("DOMAIN"),
            },
            "steps_per_action": analytics_settings.get("steps_per_action", 1),
            "response_rate": analytics_settings.get("response_rate", 1.0),
            "step_only_on_events": analytics_settings.get("step_only_on_events", False),
            "profile": analytics_settings.get("profile", {}),
        }
