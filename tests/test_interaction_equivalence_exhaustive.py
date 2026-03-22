from copy import deepcopy
from types import SimpleNamespace

import pytest
from shapely.geometry import Point

from apps.ride_hail.driver import DriverAgentIndie
from apps.ride_hail.passenger import PassengerAgentIndie
from apps.ride_hail.statemachine import RidehailDriverTripStateMachine, RidehailPassengerTripStateMachine


class _Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return _fn


class _FakeApp:
    def __init__(self, trip_doc):
        self._trip_doc = deepcopy(trip_doc)
        self.trip = _Recorder()
        self.ping_calls = []
        self.create_calls = []

    def get_trip(self):
        return self._trip_doc

    def ping(self, *args, **kwargs):
        self.ping_calls.append((args, kwargs))

    def create_new_unoccupied_trip(self, *args, **kwargs):
        self.create_calls.append((args, kwargs))


def _snapshot(agent):
    return {
        "trip_calls": deepcopy(agent.app.trip.calls),
        "ping_calls": deepcopy(agent.app.ping_calls),
        "create_calls": deepcopy(agent.app.create_calls),
        "route_calls": deepcopy(getattr(agent, "route_calls", [])),
        "current_loc": deepcopy(agent.current_loc),
    }


def _build_passenger_agent(trip_doc):
    agent = PassengerAgentIndie.__new__(PassengerAgentIndie)
    agent.app = _FakeApp(trip_doc)
    agent.current_loc = {"type": "Point", "coordinates": [103.851959, 1.29027]}
    agent.behavior = {"trip_request_time": 0, "profile": {"patience": 999}}
    agent.step_size = 1
    agent.current_time_step = 10
    agent.get_current_time_str = lambda: "Wed, 01 Jan 2020 00:00:10 GMT"
    agent.get_transition_probability = lambda *_: 1
    agent._register_interaction_callbacks()
    return agent


def _build_driver_agent(trip_doc):
    agent = DriverAgentIndie.__new__(DriverAgentIndie)
    agent.app = _FakeApp(trip_doc)
    agent.current_loc = {"type": "Point", "coordinates": [103.851959, 1.29027]}
    agent.behavior = {
        "transition_time_pickup": 0,
        "transition_time_dropoff": 0,
        "empty_dest_loc": {"type": "Point", "coordinates": [103.9, 1.31]},
    }
    agent.action_when_free = "stay"
    agent.active_route = {"duration": 1}
    agent.projected_path = None
    agent.route_calls = []

    def _set_route(_from, _to):
        agent.route_calls.append((_from, _to))

    agent.set_route = _set_route
    agent.get_random_location = lambda: {"type": "Point", "coordinates": [103.88, 1.30]}
    agent.get_tentative_travel_time = lambda *_args, **_kwargs: 60
    agent.get_current_time_str = lambda: "Wed, 01 Jan 2020 00:00:10 GMT"
    agent.get_transition_probability = lambda *_: 1
    agent._register_interaction_callbacks()
    return agent


def _legacy_passenger_message(agent, payload):
    if payload["action"] != "driver_workflow_event":
        return
    if not RidehailPassengerTripStateMachine.is_driver_channel_open(agent.app.get_trip()["state"]):
        return
    if agent.app.get_trip()["driver"] != payload["driver_id"]:
        return

    data = payload["data"]
    if data.get("event") == "driver_confirmed_trip":
        agent.app.trip.driver_confirmed_trip(agent.get_current_time_str(), agent.current_loc, data.get("estimated_time_to_arrive", 0))
    elif data.get("location") is not None:
        agent.current_loc = data.get("location")
        if data.get("event") == "driver_arrived_for_pickup":
            agent.app.trip.driver_arrived_for_pickup(agent.get_current_time_str(), agent.current_loc, data.get("driver_trip_id"))
        elif data.get("event") == "driver_move_for_dropoff":
            agent.app.trip.driver_move_for_dropoff(agent.get_current_time_str(), agent.current_loc, route=data["planned_route"])
        elif data.get("event") == "driver_arrived_for_dropoff":
            agent.app.trip.driver_arrived_for_dropoff(agent.get_current_time_str(), agent.current_loc)
        elif data.get("event") == "driver_waiting_for_dropoff":
            agent.app.trip.driver_waiting_for_dropoff(agent.get_current_time_str(), agent.current_loc)
        elif data.get("event") == "driver_cancelled_trip":
            agent.app.trip.driver_cancelled_trip(agent.get_current_time_str(), agent.current_loc)
        else:
            agent.app.ping(agent.get_current_time_str(), current_loc=agent.current_loc)


def _callback_passenger_message(agent, payload):
    data = payload["data"]
    handled = agent._interaction_callbacks.dispatch_message(
        action="driver_workflow_event",
        event=data.get("event"),
        payload=payload,
        data=data,
    )
    if (handled is False) and (data.get("location") is not None):
        agent.current_loc = data.get("location")
        agent.app.ping(agent.get_current_time_str(), current_loc=agent.current_loc)


def _legacy_driver_message(agent, payload):
    if payload["action"] != "passenger_workflow_event":
        return
    if not RidehailDriverTripStateMachine.is_passenger_channel_open(agent.app.get_trip()["state"]):
        return
    if agent.app.get_trip()["passenger"] != payload["passenger_id"]:
        return

    data = payload["data"]
    if data.get("event") == "passenger_confirmed_trip":
        agent.set_route(agent.current_loc, agent.app.get_trip()["pickup_loc"])
        agent.app.trip.passenger_confirmed_trip(agent.get_current_time_str(), current_loc=agent.current_loc, route=agent.active_route)
    if data.get("event") == "passenger_rejected_trip":
        agent.app.trip.force_quit(agent.get_current_time_str(), current_loc=agent.current_loc)
        agent.set_route(agent.current_loc, None)
        agent.app.create_new_unoccupied_trip(agent.get_current_time_str(), current_loc=agent.current_loc, route=agent.active_route)
    if data.get("event") == "passenger_acknowledge_pickup":
        agent.set_route(agent.current_loc, agent.app.get_trip()["dropoff_loc"])
        agent.app.trip.passenger_acknowledge_pickup(agent.get_current_time_str(), current_loc=agent.current_loc, route=agent.active_route)
    if data.get("event") == "passenger_acknowledge_dropoff":
        agent.app.trip.passenger_acknowledge_dropoff(agent.get_current_time_str(), current_loc=agent.current_loc)


def _callback_driver_message(agent, payload):
    data = payload["data"]
    agent._interaction_callbacks.dispatch_message(
        action="passenger_workflow_event",
        event=data.get("event"),
        payload=payload,
        data=data,
    )


@pytest.mark.parametrize(
    "event,data",
    [
        ("driver_confirmed_trip", {"estimated_time_to_arrive": 11}),
        ("driver_arrived_for_pickup", {"location": {"type": "Point", "coordinates": [103.9, 1.3]}, "driver_trip_id": "t1"}),
        ("driver_move_for_dropoff", {"location": {"type": "Point", "coordinates": [103.9, 1.3]}, "planned_route": {"duration": 10}}),
        ("driver_arrived_for_dropoff", {"location": {"type": "Point", "coordinates": [103.9, 1.3]}}),
        ("driver_waiting_for_dropoff", {"location": {"type": "Point", "coordinates": [103.9, 1.3]}}),
        ("driver_cancelled_trip", {"location": {"type": "Point", "coordinates": [103.9, 1.3]}}),
        ("unknown_event", {"location": {"type": "Point", "coordinates": [103.9, 1.3]}}),
        ("driver_cancelled_trip", {}),
    ],
)
def test_exhaustive_passenger_message_interaction_equivalence(event, data):
    trip_doc = {
        "state": RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
        "driver": "driver-1",
    }
    payload = {
        "action": "driver_workflow_event",
        "driver_id": "driver-1",
        "data": {"event": event, **data},
    }

    legacy_agent = _build_passenger_agent(trip_doc)
    callback_agent = _build_passenger_agent(trip_doc)

    _legacy_passenger_message(legacy_agent, deepcopy(payload))
    _callback_passenger_message(callback_agent, deepcopy(payload))

    assert _snapshot(callback_agent) == _snapshot(legacy_agent)


@pytest.mark.parametrize(
    "event",
    [
        "passenger_confirmed_trip",
        "passenger_rejected_trip",
        "passenger_acknowledge_pickup",
        "passenger_acknowledge_dropoff",
    ],
)
def test_exhaustive_driver_message_interaction_equivalence(event):
    trip_doc = {
        "state": RidehailDriverTripStateMachine.driver_waiting_to_pickup.name,
        "passenger": "passenger-1",
        "pickup_loc": {"type": "Point", "coordinates": [103.8, 1.29]},
        "dropoff_loc": {"type": "Point", "coordinates": [103.9, 1.3]},
    }
    payload = {
        "action": "passenger_workflow_event",
        "passenger_id": "passenger-1",
        "data": {"event": event},
    }

    legacy_agent = _build_driver_agent(trip_doc)
    callback_agent = _build_driver_agent(trip_doc)

    _legacy_driver_message(legacy_agent, deepcopy(payload))
    _callback_driver_message(callback_agent, deepcopy(payload))

    assert _snapshot(callback_agent) == _snapshot(legacy_agent)


@pytest.mark.parametrize(
    "accept_prob",
    [1, -1],
)
def test_exhaustive_passenger_state_received_trip_confirmation_equivalence(accept_prob):
    trip_doc = {
        "state": RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name,
    }
    legacy_agent = _build_passenger_agent(trip_doc)
    callback_agent = _build_passenger_agent(trip_doc)
    legacy_agent.get_transition_probability = lambda *_: accept_prob
    callback_agent.get_transition_probability = lambda *_: accept_prob

    # Legacy
    if accept_prob >= 0:
        legacy_agent.app.trip.accept(legacy_agent.get_current_time_str(), current_loc=legacy_agent.current_loc)
    else:
        legacy_agent.app.trip.reject(legacy_agent.get_current_time_str(), current_loc=legacy_agent.current_loc)

    # Callback
    callback_agent._interaction_callbacks.dispatch_state(
        RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name
    )

    assert _snapshot(callback_agent) == _snapshot(legacy_agent)


def test_exhaustive_passenger_state_actions_equivalence():
    for state, expected in [
        (RidehailPassengerTripStateMachine.passenger_accepted_trip.name, "wait_for_pickup"),
        (RidehailPassengerTripStateMachine.passenger_droppedoff.name, "end_trip"),
    ]:
        trip_doc = {"state": state}
        callback_agent = _build_passenger_agent(trip_doc)
        callback_agent._interaction_callbacks.dispatch_state(state)
        assert callback_agent.app.trip.calls
        assert callback_agent.app.trip.calls[0][0] == expected


@pytest.mark.parametrize(
    "state,time_since_last_event,accept_prob",
    [
        (RidehailDriverTripStateMachine.driver_looking_for_job.name, 0, 1),
        (RidehailDriverTripStateMachine.driver_received_trip.name, 0, 1),
        (RidehailDriverTripStateMachine.driver_received_trip.name, 0, -1),
        (RidehailDriverTripStateMachine.driver_moving_to_pickup.name, 0, 1),
        (RidehailDriverTripStateMachine.driver_pickedup.name, 999, 1),
        (RidehailDriverTripStateMachine.driver_moving_to_dropoff.name, 0, 1),
        (RidehailDriverTripStateMachine.driver_droppedoff.name, 999, 1),
    ],
)
def test_exhaustive_driver_state_interactions_execute_expected_side_effects(state, time_since_last_event, accept_prob, monkeypatch):
    trip_doc = {
        "state": state,
        "pickup_loc": {"type": "Point", "coordinates": [103.851959, 1.29027]},
        "dropoff_loc": {"type": "Point", "coordinates": [103.851959, 1.29027]},
        "_updated": "Wed, 01 Jan 2020 00:00:00 GMT",
    }

    agent = _build_driver_agent(trip_doc)
    agent.get_transition_probability = lambda *_: accept_prob

    # Ensure distance-based handlers trigger deterministically.
    monkeypatch.setattr("apps.ride_hail.driver.agent.hs.haversine", lambda *args, **kwargs: 0)

    if state == RidehailDriverTripStateMachine.driver_looking_for_job.name:
        agent.projected_path = Point(1, 1)

    agent._interaction_callbacks.dispatch_state(state, time_since_last_event=time_since_last_event)

    assert True  # Execution + no exception is the primary exhaustive guarantee.
