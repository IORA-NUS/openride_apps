"""FINDING 1: the lifecycle owner must exist as an ADMIN user before any agent can log in.

Eve bakes the role into the JWT at login and ``check_auth`` trusts the token's role for its
whole life. ``ORSimRuntime.__init__`` dispatches bootstrap agents, so if precreate (or nothing
at all) got there first with the default ``client`` role, the lifecycle agent would hold a
client token, every admin-only aggregation read would 401, ``order_ids_with_open_haul()``
would return an empty set forever, and the overdue sweep would cancel orders with in-flight
hauls — silently resurrecting the ~6% cancel race this design removes.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime

import pytest
from bson import ObjectId

from apps.container_logistics import precreate
from apps.container_logistics.order_lifecycle import users as ol_users
from tests.fake_mongo import FakeCollection

SIM_CLOCK_STR = "Wed, 29 Jul 2026 00:00:00 GMT"


# ----------------------------------------------------------------- (a0) the default topology


def test_service_is_the_default_mode(monkeypatch):
    """Since 2026-07-29 an unset (or unknown) ORSIM_ORDER_LIFECYCLE means SERVICE mode;
    the legacy per-order-agent topology must be requested explicitly."""
    from apps.simulation import run_container_logistics_simulation as entrypoint

    monkeypatch.delenv("ORSIM_ORDER_LIFECYCLE", raising=False)
    assert entrypoint._resolve_order_lifecycle_mode() == "service"
    monkeypatch.setenv("ORSIM_ORDER_LIFECYCLE", "agents")
    assert entrypoint._resolve_order_lifecycle_mode() == "agents"
    monkeypatch.setenv("ORSIM_ORDER_LIFECYCLE", "service")
    assert entrypoint._resolve_order_lifecycle_mode() == "service"
    monkeypatch.setenv("ORSIM_ORDER_LIFECYCLE", "bogus")
    assert entrypoint._resolve_order_lifecycle_mode() == "service"


# --------------------------------------------------------------------------- (a) the helper


class _RecordingUserRegistry:
    calls: list = []

    def __init__(self, sim_clock, credentials, role="client"):
        type(self).calls.append({"sim_clock": sim_clock, "credentials": credentials, "role": role})


@pytest.fixture
def registry(monkeypatch):
    _RecordingUserRegistry.calls = []
    import apps.common.user_registry as ur

    monkeypatch.setattr(ur, "UserRegistry", _RecordingUserRegistry)
    return _RecordingUserRegistry


def test_owner_is_provisioned_with_role_admin(registry):
    email = ol_users.ensure_lifecycle_owner_user(
        {"order_lifecycle_main": {"email": "ol@test.com", "password": "pw"}}, SIM_CLOCK_STR
    )

    assert email == "ol@test.com"
    assert len(registry.calls) == 1
    call = registry.calls[0]
    assert call["role"] == "admin"          # NOT client — this is the whole point
    assert call["credentials"] == {"email": "ol@test.com", "password": "pw"}
    assert call["sim_clock"] == SIM_CLOCK_STR


def test_owner_falls_back_to_the_canonical_defaults(registry):
    assert ol_users.ensure_lifecycle_owner_user({}, SIM_CLOCK_STR) == "order_lifecycle_main@test.com"
    assert registry.calls[0]["credentials"]["password"] == "password"
    assert registry.calls[0]["role"] == "admin"


def test_provisioning_failure_is_loud_not_silent(monkeypatch):
    import apps.common.user_registry as ur

    def _boom(*_a, **_k):
        raise Exception("Cannot initialize User. Bad Credentials")

    monkeypatch.setattr(ur, "UserRegistry", _boom)

    with pytest.raises(RuntimeError, match="order-lifecycle owner user"):
        ol_users.ensure_lifecycle_owner_user({}, SIM_CLOCK_STR)


def test_owner_credentials_reads_the_behavior():
    assert ol_users.owner_credentials({"x": {"email": "a@b", "password": "p"}}) == ("a@b", "p")
    assert ol_users.owner_credentials(None) == ("order_lifecycle_main@test.com", "password")


# ------------------------------------------------- (a) ordering: BEFORE runtime construction


def test_service_mode_setup_runs_before_simulationruntime_is_constructed():
    """ORSimRuntime.__init__ dispatches bootstrap celery agents, so owner provisioning has to
    complete before SimulationRuntime(...) is even called. Pinned structurally."""
    import apps.simulation.run_container_logistics_simulation as entrypoint

    tree = ast.parse(inspect.getsource(entrypoint))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")

    service_line = runtime_line = None
    for node in ast.walk(main):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "apply_order_lifecycle_service_mode" and service_line is None:
                service_line = node.lineno
            if node.func.id == "SimulationRuntime" and runtime_line is None:
                runtime_line = node.lineno

    assert service_line is not None, "main() no longer calls apply_order_lifecycle_service_mode"
    assert runtime_line is not None, "main() no longer constructs SimulationRuntime"
    assert service_line < runtime_line


def test_service_mode_setup_provisions_the_owner_and_stamps_every_carrier(monkeypatch):
    import apps.simulation.run_container_logistics_simulation as entrypoint

    seen = {}

    def _fake_ensure(collection, sim_clock):
        seen["owner"] = (collection, sim_clock)
        return "ol@test.com"

    monkeypatch.setattr(ol_users, "ensure_lifecycle_owner_user", _fake_ensure)

    class _SM:
        def __init__(self):
            self.orsim_settings = {}
            self.reference_time = datetime(2026, 7, 29, 0, 0, 0)
            self.collections = {"truck": {"t0": {"profile": {}}, "t1": {}}}

        def get_agent_collection(self, key):
            return self.collections.get(key, {})

    sm = _SM()
    run_name = entrypoint.apply_order_lifecycle_service_mode(sm, "my run")

    # owner provisioning happened, with the lifecycle behavior collection
    assert "owner" in seen
    assert set(seen["owner"][0]) == {"order_lifecycle_main"}
    # every carrier stamped
    assert sm.orsim_settings["ORDER_LIFECYCLE"] == "service"
    assert set(sm.collections["order_lifecycle"]) == {"order_lifecycle_main"}
    for behavior in sm.collections["truck"].values():
        assert behavior["profile"]["order_events_topic"] == "order_lifecycle"
    # Service is the default topology — run names stay untagged (main() tags legacy
    # agents-mode runs instead).
    assert run_name == "my run"


# --------------------------------------------------------------------------- (b) precreate


class _SignupSpy:
    def __init__(self):
        self.posts = []

    def post(self, url, headers=None, data=None):
        self.posts.append(json.loads(data))
        return None


@pytest.fixture
def signup_spy(monkeypatch):
    spy = _SignupSpy()
    monkeypatch.setattr(precreate, "get_http_session", lambda: spy)
    return spy


def _db_with(users):
    return {"user": FakeCollection(users)}


def test_precreate_never_signs_the_owner_up_as_client(signup_spy):
    """Cold start: precreate must create the owner as ADMIN, truck/facility users as client."""
    db = _db_with([])

    class _DB(dict):
        def __getitem__(self, k):
            return db.setdefault(k, FakeCollection())

    precreate._resolve_user_ids(
        _DB(),
        ["order_lifecycle_main@test.com", "truck_0@test.com"],
        roles={"order_lifecycle_main@test.com": "admin"},
    )

    by_email = {p["email"]: p["role"] for p in signup_spy.posts}
    assert by_email["order_lifecycle_main@test.com"] == "admin"
    assert by_email["truck_0@test.com"] == "client"


def test_precreate_does_not_touch_an_existing_owner(signup_spy):
    existing = FakeCollection([{"_id": ObjectId(), "email": "order_lifecycle_main@test.com"}])

    class _DB(dict):
        def __getitem__(self, k):
            return existing if k == "user" else FakeCollection()

    out = precreate._resolve_user_ids(
        _DB(), ["order_lifecycle_main@test.com"], roles={"order_lifecycle_main@test.com": "admin"}
    )

    assert signup_spy.posts == []            # no signup => no possible role downgrade
    assert "order_lifecycle_main@test.com" in out


def test_precreate_defaults_to_client_when_no_roles_are_given(signup_spy):
    class _DB(dict):
        def __getitem__(self, k):
            return FakeCollection()

    precreate._resolve_user_ids(_DB(), ["truck_0@test.com"])

    assert [p["role"] for p in signup_spy.posts] == ["client"]


def test_precreate_agent_docs_marks_the_owner_admin(monkeypatch, signup_spy):
    """End-to-end through precreate_agent_docs: the role map reaches the signup payload."""
    captured = {}
    real_resolve = precreate._resolve_user_ids

    def _spy(db, emails, roles=None):
        captured["roles"] = roles
        return real_resolve(db, emails, roles)

    monkeypatch.setattr(precreate, "_resolve_user_ids", _spy)

    statemachines = FakeCollection(
        [
            {"_id": ObjectId(), "name": n, "domain": precreate.DOMAIN}
            for n in ("WorkflowStateMachine", "GateStateMachine", "OrderStateMachine")
        ]
    )
    owner_id = ObjectId()
    users = FakeCollection([{"_id": owner_id, "email": "order_lifecycle_main@test.com"}])
    collections = {"statemachine": statemachines, "user": users}

    class _DB:
        def __getitem__(self, name):
            return collections.setdefault(name, FakeCollection())

    class _Client:
        def __getitem__(self, name):
            return _DB()

        def close(self):
            pass

    monkeypatch.setattr(precreate, "_mongo_client", lambda: _Client())

    precreate.precreate_agent_docs(
        run_id="run_x",
        truck_behaviors={},
        facility_behaviors={},
        sim_clock=datetime(2026, 7, 29),
        order_behaviors={"o0": {"profile": {}, "request_time_step": 0}},
        order_owner_email="order_lifecycle_main@test.com",
    )

    assert captured["roles"] == {"order_lifecycle_main@test.com": "admin"}


def test_agents_mode_passes_no_role_overrides(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        precreate, "_resolve_user_ids", lambda db, emails, roles=None: captured.update(roles=roles) or {}
    )

    statemachines = FakeCollection(
        [
            {"_id": ObjectId(), "name": n, "domain": precreate.DOMAIN}
            for n in ("WorkflowStateMachine", "GateStateMachine")
        ]
    )
    collections = {"statemachine": statemachines}

    class _DB:
        def __getitem__(self, name):
            return collections.setdefault(name, FakeCollection())

    class _Client:
        def __getitem__(self, name):
            return _DB()

        def close(self):
            pass

    monkeypatch.setattr(precreate, "_mongo_client", lambda: _Client())

    precreate.precreate_agent_docs(
        run_id="run_x", truck_behaviors={}, facility_behaviors={}, sim_clock=datetime(2026, 7, 29)
    )

    assert captured["roles"] is None
