"""
The idle-trip resource is written ONCE, at the end of the idle period.

Background: `TruckIdleTripManager` used to POST an `idle` document when a truck went
idle and PATCH it to `ended` when the truck was assigned -- two blocking HTTP round
trips inside agent ticks, ~31k per 1000-truck consortium run, to persist two
timestamps. Its only consumer is
`apps/container_logistics/analytics/manager.py::accumulate_idle_trips`, which reads
`state == "ended"` with `projection={start_sim_clock, sim_clock}`.

These tests pin the contract that makes the deferral safe:
  * nothing is written while the truck is merely idle,
  * the single write is a complete `ended` document, and
  * `sim_clock - start_sim_clock` is preserved exactly, because that difference IS
    the `avg_idle_time_seconds` KPI.
"""
import pytest

from apps.container_logistics.truck.idle_trip_manager import TruckIdleTripManager


class _Recorder(TruckIdleTripManager):
    """TruckIdleTripManager with the HTTP layer replaced by a call log."""

    def __init__(self, *a, **kw):
        self.posts = []
        self.patches = []
        super().__init__(*a, **kw)

    def resource_post(self, data, timeout=None):
        self.posts.append(data)
        return {**data, "_id": f"idle_{len(self.posts)}", "_etag": "etag1"}

    def resource_patch(self, resource_id, data, etag=None, timeout=None):
        self.patches.append((resource_id, data, etag))
        return {**data, "_id": resource_id, "_etag": "etag2"}

    def resource_get(self, resource_id=None, params={}, timeout=None):  # pragma: no cover
        raise AssertionError("idle trip must never GET")


def _mgr(start="2020-01-01T00:00:00", loc=(1.0, 2.0)):
    return _Recorder(run_id="run_x", sim_clock=start, user=None, current_loc=loc,
                     persona={"role": "truck", "haulier_id": "h1"})


def test_construction_writes_nothing():
    m = _mgr()
    assert m.posts == [] and m.patches == []
    assert m.resource is None


def test_ping_while_idle_writes_nothing_even_when_location_changes():
    m = _mgr(loc=(1.0, 2.0))
    for i in range(50):
        m.ping(sim_clock=f"2020-01-01T00:{i:02d}:00", current_loc=(1.0 + i, 2.0))
    assert m.posts == [] and m.patches == []


def test_end_writes_exactly_one_post_and_no_patch():
    m = _mgr()
    m.end(sim_clock="2020-01-01T01:00:00", current_loc=(9.0, 9.0))
    assert len(m.posts) == 1
    assert m.patches == []


def test_end_document_is_a_complete_ended_record():
    m = _mgr(start="2020-01-01T00:00:00", loc=(1.0, 2.0))
    m.end(sim_clock="2020-01-01T01:00:00", current_loc=(9.0, 9.0))
    doc = m.posts[0]
    # The two fields accumulate_idle_trips projects, plus the filter it selects on.
    assert doc["state"] == "ended"
    assert doc["start_sim_clock"] == "2020-01-01T00:00:00"
    assert doc["sim_clock"] == "2020-01-01T01:00:00"
    # Schema-required fields must be present or Eve rejects the POST outright.
    assert doc["kind"] == "idle"
    assert doc["persona"]["role"] == "truck_idle_trip"
    assert doc["end_loc"] == (9.0, 9.0)
    assert doc["start_loc"] == (1.0, 2.0)


def test_idle_duration_is_preserved_across_the_deferral():
    """The KPI is end - start; the deferral must not shift either stamp."""
    m = _mgr(start="2020-01-02T03:00:00")
    m.ping(sim_clock="2020-01-02T03:30:00", current_loc=(5.0, 5.0))
    m.end(sim_clock="2020-01-02T04:00:00", current_loc=(5.0, 5.0))
    doc = m.posts[0]
    assert doc["start_sim_clock"] == "2020-01-02T03:00:00"   # NOT the ping's stamp
    assert doc["sim_clock"] == "2020-01-02T04:00:00"


def test_end_is_idempotent_and_never_creates_a_second_period():
    m = _mgr()
    m.end(sim_clock="2020-01-01T01:00:00", current_loc=(9.0, 9.0))
    m.end(sim_clock="2020-01-01T02:00:00", current_loc=(9.0, 9.0))
    # Second call closes the existing document instead of opening a new period.
    assert len(m.posts) == 1
    assert len(m.patches) == 1


def test_legacy_document_is_closed_by_patch_not_duplicated():
    """If a document already exists (legacy create_new path), end() must PATCH it."""
    m = _mgr()
    m.create_new(sim_clock="2020-01-01T00:00:00", current_loc=(1.0, 2.0))
    assert len(m.posts) == 1
    m.end(sim_clock="2020-01-01T01:00:00", current_loc=(9.0, 9.0))
    assert len(m.posts) == 1          # no duplicate period
    assert len(m.patches) == 1
    assert m.patches[0][1]["state"] == "ended"
