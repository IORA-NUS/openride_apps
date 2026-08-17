"""WP3: order documents are bulk pre-created in ``created`` (service mode only).

The ``agents``-mode path must be untouched — same call, same inserts, no order-collection
access at all.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from bson import ObjectId

from apps.container_logistics import precreate
from tests.fake_mongo import FakeCollection

RUN_ID = "run_test"
SIM_CLOCK = datetime(2026, 7, 29, 0, 0, 0)
OWNER_EMAIL = "order_lifecycle_main@test.com"
OWNER_UID = ObjectId()
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class FakeDB:
    def __init__(self, collections):
        self._collections = collections
        self.accessed = []

    def __getitem__(self, name):
        self.accessed.append(name)
        return self._collections.setdefault(name, FakeCollection())


class FakeClient:
    def __init__(self, db):
        self._db = db
        self.closed = False

    def __getitem__(self, name):
        return self._db

    def close(self):
        self.closed = True


@pytest.fixture
def db(monkeypatch):
    statemachines = FakeCollection(
        [
            {"_id": ObjectId(), "name": name, "domain": precreate.DOMAIN}
            for name in ("WorkflowStateMachine", "GateStateMachine", "OrderStateMachine")
        ]
    )
    users = FakeCollection(
        [
            {"_id": OWNER_UID, "email": OWNER_EMAIL},
            {"_id": ObjectId(), "email": "truck_0@test.com"},
            {"_id": ObjectId(), "email": "facility_0@test.com"},
        ]
    )
    collections = {
        "statemachine": statemachines,
        "user": users,
        "container_logistics_truck": FakeCollection(),
        "container_logistics_facility": FakeCollection(),
        "container_logistics_order": FakeCollection(),
    }
    fake_db = FakeDB(collections)
    monkeypatch.setattr(precreate, "_mongo_client", lambda: FakeClient(fake_db))
    return fake_db


TRUCKS = {
    "truck_0": {
        "email": "truck_0@test.com",
        "profile": {"haulier_id": "acme"},
        "init_loc": {"type": "Point", "coordinates": [103.8, 1.3]},
    }
}
FACILITIES = {
    "facility_0": {"email": "facility_0@test.com", "profile": {"name": "port_a", "gate_count": 2}}
}
ORDERS = {
    f"order_{i}": {
        "email": f"order_{i}@test.com",
        "request_time_step": 10 * i,
        "profile": {
            "haulier_id": "acme",
            "pickup_loc": {"type": "Point", "coordinates": [103.8, 1.3]},
            "dropoff_loc": {"type": "Point", "coordinates": [103.9, 1.4]},
            "pickup_facility_name": "port_a",
        },
        "persona": {"domain": "container-logistics-sim"},
    }
    for i in range(3)
}


# --------------------------------------------------------------------------- agents mode


def test_agents_mode_never_touches_the_order_collection(db):
    stats = precreate.precreate_agent_docs(
        run_id=RUN_ID,
        truck_behaviors=TRUCKS,
        facility_behaviors=FACILITIES,
        sim_clock=SIM_CLOCK,
    )

    assert stats["trucks_inserted"] == 1 and stats["facilities_inserted"] == 1
    assert stats["orders_inserted"] == 0 and stats["orders_skipped"] == 0
    assert "container_logistics_order" not in db.accessed
    assert db._collections["container_logistics_order"].docs == {}


# --------------------------------------------------------------------------- service mode


def _run_service_mode():
    return precreate.precreate_agent_docs(
        run_id=RUN_ID,
        truck_behaviors=TRUCKS,
        facility_behaviors=FACILITIES,
        sim_clock=SIM_CLOCK,
        order_behaviors=ORDERS,
        order_owner_email=OWNER_EMAIL,
    )


def test_service_mode_inserts_one_created_doc_per_order(db):
    stats = _run_service_mode()

    assert stats["orders_inserted"] == 3 and stats["orders_skipped"] == 0
    docs = list(db._collections["container_logistics_order"].docs.values())
    assert len(docs) == 3

    by_step = {d["meta"]["request_time_step"]: d for d in docs}
    assert sorted(by_step) == [0, 10, 20]

    doc = by_step[10]
    assert doc["run_id"] == RUN_ID
    assert doc["state"] == "created"
    assert doc["feasible_transitions"] == []
    assert doc["user"] == str(OWNER_UID)  # ALL orders owned by the lifecycle service user
    assert doc["persona"]["role"] == "order"
    assert doc["statemachine"]["name"] == "OrderStateMachine"
    assert doc["statemachine"]["domain"] == precreate.DOMAIN
    assert isinstance(doc["statemachine"]["id"], ObjectId)
    # profile verbatim from the behavior
    assert doc["profile"] == ORDERS["order_1"]["profile"]
    # no top-level pickup/dropoff (they live under profile, as in agents mode)
    assert "pickup_loc" not in doc and "dropoff_loc" not in doc
    # Eve-managed stamps
    assert doc["sim_clock"] == SIM_CLOCK
    assert doc["_created"] == SIM_CLOCK and doc["_updated"] == SIM_CLOCK
    assert isinstance(doc["_created"], datetime)
    assert HEX40.match(doc["_etag"])


def test_every_order_doc_shares_the_one_owner_uid(db):
    _run_service_mode()
    owners = {d["user"] for d in db._collections["container_logistics_order"].docs.values()}
    assert owners == {str(OWNER_UID)}


def test_service_mode_is_idempotent_per_run_and_user(db):
    _run_service_mode()
    second = _run_service_mode()

    assert second["orders_inserted"] == 0
    assert second["orders_skipped"] == 3
    assert len(db._collections["container_logistics_order"].docs) == 3


def test_owner_email_defaults_when_not_supplied(db):
    stats = precreate.precreate_agent_docs(
        run_id=RUN_ID,
        truck_behaviors=TRUCKS,
        facility_behaviors=FACILITIES,
        sim_clock=SIM_CLOCK,
        order_behaviors=ORDERS,
        order_owner_email=None,
    )
    assert stats["orders_inserted"] == 3
    owners = {d["user"] for d in db._collections["container_logistics_order"].docs.values()}
    assert owners == {str(OWNER_UID)}


def test_order_doc_builder_defaults_a_missing_request_time_step():
    doc = precreate._order_doc(RUN_ID, {"profile": {}}, "uid", ObjectId(), SIM_CLOCK)
    assert doc["meta"]["request_time_step"] == 0
    assert doc["state"] == "created"


# --------------------------------------------------------------------------- runtime routing


def _fake_runtime(orsim_settings, collections):
    """SimulationRuntime with only the attributes ``_precreate_agent_docs`` reads."""
    from apps.config import simulation_domains
    from apps.simulation.simulation_runtime import SimulationRuntime

    runtime = SimulationRuntime.__new__(SimulationRuntime)
    runtime.run_id = RUN_ID
    runtime.domain = simulation_domains.get("container_logistics", "container-logistics-sim")
    runtime.orsim_settings = orsim_settings
    runtime.reference_time = SIM_CLOCK

    class _SM:
        def get_agent_collection(self, key):
            return collections.get(key, {})

    runtime.scenario_manager = _SM()
    return runtime


def _capture(monkeypatch):
    seen = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(precreate, "precreate_agent_docs", _fake)
    return seen


COLLECTIONS = {
    "truck": TRUCKS,
    "facility": FACILITIES,
    "order": ORDERS,
    "order_lifecycle": {"order_lifecycle_main": {"email": OWNER_EMAIL}},
}


def test_runtime_agents_mode_passes_no_order_behaviors(monkeypatch):
    seen = _capture(monkeypatch)
    _fake_runtime({}, COLLECTIONS)._precreate_agent_docs()

    assert set(seen) == {"run_id", "truck_behaviors", "facility_behaviors", "sim_clock"}


def test_runtime_service_mode_passes_orders_and_the_lifecycle_owner(monkeypatch):
    seen = _capture(monkeypatch)
    _fake_runtime({"ORDER_LIFECYCLE": "service"}, COLLECTIONS)._precreate_agent_docs()

    assert seen["order_behaviors"] == ORDERS
    assert seen["order_owner_email"] == OWNER_EMAIL


def test_runtime_service_mode_falls_back_to_the_default_owner_email(monkeypatch):
    seen = _capture(monkeypatch)
    collections = dict(COLLECTIONS, order_lifecycle={})
    _fake_runtime({"ORDER_LIFECYCLE": "service"}, collections)._precreate_agent_docs()

    assert seen["order_owner_email"] == "order_lifecycle_main@test.com"
