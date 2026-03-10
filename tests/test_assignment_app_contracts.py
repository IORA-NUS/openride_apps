from apps.ride_hail.assignment import AssignmentApp
from apps.ride_hail import RideHailActions


class _FakeMessenger:
    def __init__(self):
        self.client = self
        self.published = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))


def test_assignment_publish_uses_ride_hail_requested_trip_action():
    app = AssignmentApp.__new__(AssignmentApp)
    app.run_id = "run-1"
    app.messenger = _FakeMessenger()

    assignment = [
        (
            {"driver": "driver-1"},
            {"passenger": "passenger-1", "_id": "trip-1", "pickup_loc": {"type": "Point", "coordinates": [1, 2]}},
        )
    ]

    app.publish(assignment)

    assert len(app.messenger.published) == 1
    topic, payload = app.messenger.published[0]
    assert topic == "run-1/driver-1"
    assert f'"action": "{RideHailActions.REQUESTED_TRIP}"' in payload
