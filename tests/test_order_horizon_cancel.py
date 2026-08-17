"""
Unit tests for OrderAgent's post-horizon local exit + the bulk-cancel utility.

Past the horizon, unservable orders must leave the market so the agent scheduler drains
(otherwise HorizonDrainTermination hangs). Leaving is a LOCAL shutdown only — the order
records are terminalized in one bulk update at completion (bulk_cancel_nonterminal_orders),
because per-agent HTTP cancellation cannot drain a large backlog within the post-horizon
window. OrderAgent is built via __new__ to skip MQTT/Celery; the app/manager is faked.
"""
from apps.container_logistics.order.agent import OrderAgent


class _FakeManager:
    def __init__(self, state):
        self._state = state
        self.cancel_calls = 0

    def as_dict(self):
        return {"state": self._state}

    def cancel(self, sim_clock=None, **extra):  # must NOT be called past horizon (bulk handles it)
        self.cancel_calls += 1
        self._state = "cancelled"


class _FakeApp:
    def __init__(self, state):
        self.manager = _FakeManager(state)


def _make_order(state, *, current_step, horizon=2520, grace=60):
    o = OrderAgent.__new__(OrderAgent)
    o.app = _FakeApp(state)
    o.orsim_settings = {"SIMULATION_LENGTH_IN_STEPS": horizon, "POST_HORIZON_GRACE_STEPS": grace}
    o.current_time_step = current_step
    o.unique_id = "order_test"
    o._shutdown_called = False
    o.shutdown = lambda: setattr(o, "_shutdown_called", True)
    o.get_current_time_str = lambda: "Mon, 01 Jan 2020 04:00:00 GMT"
    return o


def test_completed_order_exits_normally():
    o = _make_order("completed", current_step=100)
    assert o.exiting_market() is True
    assert o._shutdown_called is True


def test_unassigned_before_horizon_stays():
    o = _make_order("unassigned", current_step=2519, horizon=2520)
    assert o.exiting_market() is False
    assert o._shutdown_called is False


def test_unassigned_at_horizon_leaves_locally_without_http_cancel():
    o = _make_order("unassigned", current_step=2520, horizon=2520)
    assert o.exiting_market() is True
    assert o._shutdown_called is True
    assert o.app.manager.cancel_calls == 0  # bulk cancel terminalizes the record, not a PATCH


def test_created_at_horizon_leaves_locally():
    o = _make_order("created", current_step=2520, horizon=2520)
    assert o.exiting_market() is True
    assert o._shutdown_called is True
    assert o.app.manager.cancel_calls == 0


def test_inflight_within_grace_is_left_alone():
    o = _make_order("in_transit", current_step=2540, horizon=2520, grace=60)
    assert o.exiting_market() is False
    assert o._shutdown_called is False


def test_inflight_after_grace_leaves_locally():
    o = _make_order("in_transit", current_step=2580, horizon=2520, grace=60)
    assert o.exiting_market() is True
    assert o._shutdown_called is True
    assert o.app.manager.cancel_calls == 0


# ── bulk_cancel_nonterminal_orders ─────────────────────────────────────────────

class _FakeResult:
    def __init__(self, n):
        self.modified_count = n


class _FakeCollection:
    def __init__(self):
        self.calls = []

    def update_many(self, flt, update):
        self.calls.append((flt, update))
        return _FakeResult(3)


class _FakeClient:
    def __init__(self, collection):
        self._collection = collection
        self.closed = False

    def __getitem__(self, _db):
        return {"container_logistics_order": self._collection}

    def close(self):
        self.closed = True


def test_bulk_cancel_targets_only_nonterminal_orders(monkeypatch):
    from apps.container_logistics.order import bulk_cancel

    coll = _FakeCollection()
    client = _FakeClient(coll)
    monkeypatch.setattr(bulk_cancel, "_mongo_client", lambda: client)

    n = bulk_cancel.bulk_cancel_nonterminal_orders("run_X", sim_clock="Mon, 01 Jan 2020 04:00:00 GMT")

    assert n == 3
    assert client.closed is True
    flt, update = coll.calls[0]
    assert flt["run_id"] == "run_X"
    # Only non-terminal orders are touched; terminal ones are excluded.
    assert set(flt["state"]["$nin"]) == {"completed", "cancelled"}
    assert update["$set"]["state"] == "cancelled"
    assert update["$set"]["_updated"] == "Mon, 01 Jan 2020 04:00:00 GMT"
