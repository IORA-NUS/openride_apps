from apps.ride_hail.driver import DriverAgentIndie, DriverApp
from apps.ride_hail.passenger import PassengerAgentIndie, PassengerApp
from apps.ride_hail import (
    AssignedPayload,
    DriverWorkflowPayload,
    PassengerWorkflowPayload,
    RequestedTripPayload,
    RideHailActions,
    RideHailEvents,
    validate_assigned_payload,
    validate_driver_workflow_payload,
    validate_passenger_workflow_payload,
    validate_requested_trip_payload,
)
from apps.state_machine import RidehailDriverTripStateMachine, RidehailPassengerTripStateMachine


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class _TripRecorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return _fn


class _QueueApp:
    def __init__(self, trip_doc, queue):
        self._trip_doc = trip_doc
        self._queue = list(queue)
        self.trip = _TripRecorder()
        self.ping_calls = []
        self.create_calls = []

    def get_trip(self):
        return self._trip_doc

    def dequeue_message(self):
        if self._queue:
            return self._queue.pop(0)
        return None

    def enfront_message(self, payload):
        self._queue.insert(0, payload)

    def ping(self, *args, **kwargs):
        self.ping_calls.append((args, kwargs))

    def create_new_unoccupied_trip(self, *args, **kwargs):
        self.create_calls.append((args, kwargs))


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


def test_requested_trip_validator():
    valid = {
        "action": RideHailActions.REQUESTED_TRIP,
        "passenger_id": "p1",
        "requested_trip": {"pickup_loc": "a"},
    }
    assert validate_requested_trip_payload(valid) is True
    assert RequestedTripPayload.parse(valid) is not None
    assert validate_requested_trip_payload({"action": RideHailActions.REQUESTED_TRIP}) is False


def test_assigned_validator():
    assert validate_assigned_payload({"action": RideHailActions.ASSIGNED, "driver_id": "d1"}) is True
    assert AssignedPayload.parse({"action": RideHailActions.ASSIGNED, "driver_id": "d1"}) is not None
    assert validate_assigned_payload({"action": RideHailActions.ASSIGNED}) is False


def test_workflow_validators():
    assert validate_passenger_workflow_payload(
        {
            "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
            "passenger_id": "p1",
            "data": {"event": RideHailEvents.PASSENGER_CONFIRMED_TRIP},
        }
    )
    assert PassengerWorkflowPayload.parse(
        {
            "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
            "passenger_id": "p1",
            "data": {"event": RideHailEvents.PASSENGER_CONFIRMED_TRIP},
        }
    ) is not None
    assert validate_driver_workflow_payload(
        {
            "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
            "driver_id": "d1",
            "data": {"event": RideHailEvents.DRIVER_CONFIRMED_TRIP},
        }
    )
    assert DriverWorkflowPayload.parse(
        {
            "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
            "driver_id": "d1",
            "data": {"event": RideHailEvents.DRIVER_CONFIRMED_TRIP},
        }
    ) is not None
    assert validate_passenger_workflow_payload({"action": RideHailActions.PASSENGER_WORKFLOW_EVENT, "data": {}}) is False
    assert validate_driver_workflow_payload({"action": RideHailActions.DRIVER_WORKFLOW_EVENT, "data": {}}) is False


def test_driver_app_message_handler_ignores_invalid_requested_trip_payload():
    app = DriverApp.__new__(DriverApp)
    app.latest_sim_clock = "Wed, 01 Jan 2020 00:00:10 GMT"
    app.latest_loc = {"type": "Point", "coordinates": [103.9, 1.3]}
    app.enqueue_message = _Recorder()
    app.handle_requested_trip = _Recorder()

    app.message_handler({"action": RideHailActions.REQUESTED_TRIP, "passenger_id": "p1"})

    assert len(app.handle_requested_trip.calls) == 0
    assert len(app.enqueue_message.calls) == 0


def test_passenger_app_message_handler_ignores_invalid_assigned_payload():
    app = PassengerApp.__new__(PassengerApp)
    app.latest_sim_clock = "Wed, 01 Jan 2020 00:00:10 GMT"
    app.latest_loc = {"type": "Point", "coordinates": [103.9, 1.3]}
    app.enqueue_message = _Recorder()
    app.handle_overbooking = _Recorder()
    app.trip = _TripRecorder()
    app.get_trip = lambda: {"state": RidehailPassengerTripStateMachine.passenger_requested_trip.name}

    app.message_handler({"action": RideHailActions.ASSIGNED})

    assert len(app.trip.calls) == 0
    assert len(app.handle_overbooking.calls) == 0
    assert len(app.enqueue_message.calls) == 0


def test_driver_agent_skips_invalid_passenger_workflow_payload_and_processes_next():
    trip_doc = {
        "state": RidehailDriverTripStateMachine.driver_waiting_to_dropoff.name,
        "passenger": "p1",
        "dropoff_loc": {"type": "Point", "coordinates": [103.9, 1.3]},
        "pickup_loc": {"type": "Point", "coordinates": [103.8, 1.29]},
    }
    queue = [
        {
            "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
            "passenger_id": "p1",
            "data": {},
        },
        {
            "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
            "passenger_id": "p1",
            "data": {"event": RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF},
        },
    ]

    app = _QueueApp(trip_doc, queue)
    agent = _build_driver_agent(app)
    agent.consume_messages()

    assert app.trip.calls
    assert app.trip.calls[0][0] == "passenger_acknowledge_dropoff"


def test_passenger_agent_skips_invalid_driver_workflow_payload_and_processes_next():
    trip_doc = {
        "state": RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
        "driver": "d1",
        "_updated": "Wed, 01 Jan 2020 00:00:00 GMT",
    }
    queue = [
        {
            "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
            "driver_id": "d1",
            "data": {},
        },
        {
            "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
            "driver_id": "d1",
            "data": {"event": RideHailEvents.DRIVER_CONFIRMED_TRIP, "estimated_time_to_arrive": 11},
        },
    ]

    app = _QueueApp(trip_doc, queue)
    agent = _build_passenger_agent(app)
    agent.consume_messages()

    assert app.trip.calls
    assert app.trip.calls[0][0] == "driver_confirmed_trip"
