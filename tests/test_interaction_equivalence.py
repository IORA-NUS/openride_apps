from apps.driver_app.driver_agent_indie import DriverAgentIndie
from apps.passenger_app.passenger_agent_indie import PassengerAgentIndie
from apps.state_machine import RidehailDriverTripStateMachine, RidehailPassengerTripStateMachine


class _Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return _fn


class _FakeApp:
    def __init__(self, trip_doc):
        self._trip_doc = trip_doc
        self.trip = _Recorder()
        self.ping_calls = []
        self.create_calls = []

    def get_trip(self):
        return self._trip_doc

    def ping(self, *args, **kwargs):
        self.ping_calls.append((args, kwargs))

    def create_new_unoccupied_trip(self, *args, **kwargs):
        self.create_calls.append((args, kwargs))


def _build_passenger_agent(app):
    agent = PassengerAgentIndie.__new__(PassengerAgentIndie)
    agent.app = app
    agent.current_loc = {"type": "Point", "coordinates": [103.851959, 1.29027]}
    agent.behavior = {"trip_request_time": 0, "profile": {"patience": 999}}
    agent.step_size = 1
    agent.current_time_step = 10
    agent.get_current_time_str = lambda: "Wed, 01 Jan 2020 00:00:10 GMT"
    agent.get_transition_probability = lambda *_: 1
    agent._register_interaction_callbacks()
    return agent


def _build_driver_agent(app):
    agent = DriverAgentIndie.__new__(DriverAgentIndie)
    agent.app = app
    agent.current_loc = {"type": "Point", "coordinates": [103.851959, 1.29027]}
    agent.behavior = {
        "transition_time_pickup": 0,
        "transition_time_dropoff": 0,
        "empty_dest_loc": {"type": "Point", "coordinates": [103.9, 1.31]},
    }
    agent.action_when_free = "stay"
    agent.active_route = {"duration": 1}
    agent.route_calls = []

    def _set_route(_from, _to):
        agent.route_calls.append((_from, _to))

    agent.set_route = _set_route
    agent.get_tentative_travel_time = lambda *_args, **_kwargs: 60
    agent.get_current_time_str = lambda: "Wed, 01 Jan 2020 00:00:10 GMT"
    agent.get_transition_probability = lambda *_: 1
    agent._register_interaction_callbacks()
    return agent


def _legacy_passenger_driver_event(agent, payload):
    if RidehailPassengerTripStateMachine.is_driver_channel_open(agent.app.get_trip()["state"]):
        if agent.app.get_trip()["driver"] == payload["driver_id"]:
            driver_data = payload["data"]

            if driver_data.get("event") == "driver_confirmed_trip":
                agent.app.trip.driver_confirmed_trip(
                    agent.get_current_time_str(),
                    agent.current_loc,
                    driver_data.get("estimated_time_to_arrive", 0),
                )
            elif driver_data.get("location") is not None:
                agent.current_loc = driver_data.get("location")

                if driver_data.get("event") == "driver_arrived_for_pickup":
                    agent.app.trip.driver_arrived_for_pickup(
                        agent.get_current_time_str(),
                        agent.current_loc,
                        driver_data.get("driver_trip_id"),
                    )
                elif driver_data.get("event") == "driver_move_for_dropoff":
                    agent.app.trip.driver_move_for_dropoff(
                        agent.get_current_time_str(),
                        agent.current_loc,
                        route=driver_data["planned_route"],
                    )
                elif driver_data.get("event") == "driver_arrived_for_dropoff":
                    agent.app.trip.driver_arrived_for_dropoff(agent.get_current_time_str(), agent.current_loc)
                elif driver_data.get("event") == "driver_waiting_for_dropoff":
                    agent.app.trip.driver_waiting_for_dropoff(agent.get_current_time_str(), agent.current_loc)
                elif driver_data.get("event") == "driver_cancelled_trip":
                    agent.app.trip.driver_cancelled_trip(agent.get_current_time_str(), agent.current_loc)
                else:
                    agent.app.ping(agent.get_current_time_str(), current_loc=agent.current_loc)


def _legacy_driver_passenger_event(agent, payload):
    if RidehailDriverTripStateMachine.is_passenger_channel_open(agent.app.get_trip()["state"]):
        if agent.app.get_trip()["passenger"] == payload["passenger_id"]:
            data = payload["data"]

            if data.get("event") == "passenger_confirmed_trip":
                agent.set_route(agent.current_loc, agent.app.get_trip()["pickup_loc"])
                agent.app.trip.passenger_confirmed_trip(
                    agent.get_current_time_str(), current_loc=agent.current_loc, route=agent.active_route
                )

            if data.get("event") == "passenger_rejected_trip":
                agent.app.trip.force_quit(agent.get_current_time_str(), current_loc=agent.current_loc)
                agent.set_route(agent.current_loc, None)
                agent.app.create_new_unoccupied_trip(
                    agent.get_current_time_str(), current_loc=agent.current_loc, route=agent.active_route
                )

            if data.get("event") == "passenger_acknowledge_pickup":
                agent.set_route(agent.current_loc, agent.app.get_trip()["dropoff_loc"])
                agent.app.trip.passenger_acknowledge_pickup(
                    agent.get_current_time_str(), current_loc=agent.current_loc, route=agent.active_route
                )

            if data.get("event") == "passenger_acknowledge_dropoff":
                agent.app.trip.passenger_acknowledge_dropoff(agent.get_current_time_str(), current_loc=agent.current_loc)


def test_passenger_message_driver_confirmed_trip_equivalence():
    trip = {"state": RidehailPassengerTripStateMachine.passenger_accepted_trip.name, "driver": "d1"}
    payload = {
        "action": "driver_workflow_event",
        "driver_id": "d1",
        "data": {"event": "driver_confirmed_trip", "estimated_time_to_arrive": 42},
    }

    app_callback = _FakeApp(dict(trip))
    app_legacy = _FakeApp(dict(trip))
    callback_agent = _build_passenger_agent(app_callback)
    legacy_agent = _build_passenger_agent(app_legacy)

    callback_agent._interaction_callbacks.dispatch_message(
        action="driver_workflow_event", event="driver_confirmed_trip", payload=payload, data=payload["data"]
    )
    _legacy_passenger_driver_event(legacy_agent, payload)

    assert app_callback.trip.calls == app_legacy.trip.calls


def test_passenger_unknown_driver_event_with_location_equivalence():
    trip = {"state": RidehailPassengerTripStateMachine.passenger_accepted_trip.name, "driver": "d1"}
    payload = {
        "action": "driver_workflow_event",
        "driver_id": "d1",
        "data": {"event": "unknown_event", "location": {"type": "Point", "coordinates": [103.9, 1.3]}},
    }

    app_callback = _FakeApp(dict(trip))
    app_legacy = _FakeApp(dict(trip))
    callback_agent = _build_passenger_agent(app_callback)
    legacy_agent = _build_passenger_agent(app_legacy)

    handled = callback_agent._interaction_callbacks.dispatch_message(
        action="driver_workflow_event", event="unknown_event", payload=payload, data=payload["data"]
    )
    if handled is False and payload["data"].get("location") is not None:
        callback_agent.current_loc = payload["data"]["location"]
        callback_agent.app.ping(callback_agent.get_current_time_str(), current_loc=callback_agent.current_loc)

    _legacy_passenger_driver_event(legacy_agent, payload)

    assert app_callback.ping_calls == app_legacy.ping_calls


def test_driver_message_passenger_acknowledge_pickup_equivalence():
    trip = {
        "state": RidehailDriverTripStateMachine.driver_waiting_to_pickup.name,
        "passenger": "p1",
        "pickup_loc": {"type": "Point", "coordinates": [103.8, 1.29]},
        "dropoff_loc": {"type": "Point", "coordinates": [103.9, 1.3]},
    }
    payload = {
        "action": "passenger_workflow_event",
        "passenger_id": "p1",
        "data": {"event": "passenger_acknowledge_pickup"},
    }

    app_callback = _FakeApp(dict(trip))
    app_legacy = _FakeApp(dict(trip))
    callback_agent = _build_driver_agent(app_callback)
    legacy_agent = _build_driver_agent(app_legacy)

    callback_agent._interaction_callbacks.dispatch_message(
        action="passenger_workflow_event",
        event="passenger_acknowledge_pickup",
        payload=payload,
        data=payload["data"],
    )
    _legacy_driver_passenger_event(legacy_agent, payload)

    assert app_callback.trip.calls == app_legacy.trip.calls
    assert callback_agent.route_calls == legacy_agent.route_calls
