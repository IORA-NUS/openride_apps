from types import SimpleNamespace

from apps.ride_hail.driver import DriverAgentIndie
from apps.ride_hail.passenger import PassengerAgentIndie
from apps.ride_hail.statemachine import RidehailDriverTripStateMachine, RidehailPassengerTripStateMachine
# from apps.agent_core.state_machine import WorkflowStateMachine
from orsim.utils import WorkflowStateMachine


class _FakeTripMethods:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return _fn


class _FakeApp:
    def __init__(self, trip_doc, queue=None):
        self._trip_doc = trip_doc
        self._queue = list(queue or [])
        self.trip = _FakeTripMethods()
        self.ping_calls = []
        self.create_calls = []

    def get_trip(self):
        return self._trip_doc

    def get_driver(self):
        return {"state": WorkflowStateMachine.online.name}

    def get_passenger(self):
        return {"state": WorkflowStateMachine.online.name}

    def dequeue_message(self):
        if self._queue:
            return self._queue.pop(0)
        return None

    def ping(self, *args, **kwargs):
        self.ping_calls.append((args, kwargs))

    def create_new_unoccupied_trip(self, *args, **kwargs):
        self.create_calls.append((args, kwargs))

    def enfront_message(self, payload):
        self._queue.insert(0, payload)


def _build_passenger_agent(app):
    agent = PassengerAgentIndie.__new__(PassengerAgentIndie)
    agent.app = app
    agent.current_loc = {"type": "Point", "coordinates": [103.851959, 1.29027]}
    agent.behavior = {"trip_request_time": 0, "profile": {"patience": 999}}
    agent.step_size = 1
    agent.current_time_step = 10
    agent.unique_id = "passenger-test"
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
    agent.projected_path = None
    agent.unique_id = "driver-test"
    agent.get_current_time_str = lambda: "Wed, 01 Jan 2020 00:00:10 GMT"
    agent.get_transition_probability = lambda *_: 1
    agent.set_route = lambda *_args, **_kwargs: None
    agent.get_random_location = lambda: {"type": "Point", "coordinates": [103.88, 1.30]}
    agent.get_tentative_travel_time = lambda *_args, **_kwargs: 60
    agent._register_interaction_callbacks()
    return agent


def test_passenger_consumes_driver_confirmed_event_dispatches_trip_method():
    trip_doc = {
        "state": RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
        "driver": "driver-1",
        "_updated": "Wed, 01 Jan 2020 00:00:00 GMT",
    }
    queue = [
        {
            "action": "driver_workflow_event",
            "driver_id": "driver-1",
            "data": {"event": "driver_confirmed_trip", "estimated_time_to_arrive": 123},
        }
    ]

    app = _FakeApp(trip_doc, queue)
    agent = _build_passenger_agent(app)

    agent.consume_messages()

    assert app.trip.calls
    name, args, _kwargs = app.trip.calls[0]
    assert name == "driver_confirmed_trip"
    assert args[2] == 123


def test_passenger_unknown_event_with_location_falls_back_to_ping():
    trip_doc = {
        "state": RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
        "driver": "driver-1",
        "_updated": "Wed, 01 Jan 2020 00:00:00 GMT",
    }
    queue = [
        {
            "action": "driver_workflow_event",
            "driver_id": "driver-1",
            "data": {"event": "unhandled_event", "location": {"type": "Point", "coordinates": [103.9, 1.3]}},
        }
    ]

    app = _FakeApp(trip_doc, queue)
    agent = _build_passenger_agent(app)

    agent.consume_messages()

    assert app.ping_calls, "Expected fallback ping for unhandled event with location"


def test_driver_consumes_passenger_acknowledge_dropoff_event_dispatches_trip_method():
    trip_doc = {
        "state": RidehailDriverTripStateMachine.driver_waiting_to_dropoff.name,
        "passenger": "passenger-1",
        "dropoff_loc": {"type": "Point", "coordinates": [103.9, 1.3]},
        "pickup_loc": {"type": "Point", "coordinates": [103.8, 1.29]},
        "_updated": "Wed, 01 Jan 2020 00:00:00 GMT",
    }
    queue = [
        {
            "action": "passenger_workflow_event",
            "passenger_id": "passenger-1",
            "data": {"event": "passenger_acknowledge_dropoff"},
        }
    ]

    app = _FakeApp(trip_doc, queue)
    agent = _build_driver_agent(app)

    agent.consume_messages()

    assert app.trip.calls
    assert app.trip.calls[0][0] == "passenger_acknowledge_dropoff"


def test_passenger_state_action_dispatch_wait_for_pickup():
    trip_doc = {
        "state": RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
        "_updated": "Wed, 01 Jan 2020 00:00:00 GMT",
    }
    app = _FakeApp(trip_doc)
    agent = _build_passenger_agent(app)

    agent.perform_workflow_actions()

    assert app.trip.calls
    assert app.trip.calls[0][0] == "wait_for_pickup"


def test_driver_state_action_dispatch_move_to_dropoff_after_pickup_delay():
    trip_doc = {
        "state": RidehailDriverTripStateMachine.driver_pickedup.name,
        "_updated": "Wed, 01 Jan 2020 00:00:00 GMT",
        "dropoff_loc": {"type": "Point", "coordinates": [103.9, 1.3]},
        "pickup_loc": {"type": "Point", "coordinates": [103.8, 1.29]},
    }
    app = _FakeApp(trip_doc)
    agent = _build_driver_agent(app)

    agent.perform_workflow_actions()

    assert app.trip.calls
    assert app.trip.calls[0][0] == "move_to_dropoff"
