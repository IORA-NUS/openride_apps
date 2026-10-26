"""Shared replay harness for the dispatch golden fixture (plan Step G0).

Responsibility: turn one stored golden case (a tick state + a full assignment
profile + a strategy name) into the observable outputs of the REAL
``AssignmentApp.assign`` / ``publish``: ordered pairs, share tags, the exact
published MQTT strings, pooled round stats, and a module-RNG probe.

Contract: the capture script and the replay test both go through
:func:`run_case`, so a difference between capture and replay can only come from
production code. This module imports only pre-policy-slots public APIs
(``AssignmentApp``, ``get_solver``) and must stay that way, otherwise the
fixture would stop measuring the refactor it guards.

The stub manager/messenger are COPIED from ``tests/test_assignment_cooperation.py``
(not imported) so later edits to that test cannot silently change what the
golden replay feeds the app. The copy is extended in two places only: the
manager returns the tick state's claimed order ids / busy truck ids, and the
messenger keeps the raw payload string (byte identity is the point).
"""

from __future__ import annotations

import copy
import random
from typing import Any

from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.assignment.solver import get_solver

__all__ = [
    "MODULE_SEED",
    "TIME_STEP",
    "RUN_ID",
    "StubManager",
    "StubMessenger",
    "build_app",
    "run_case",
]

MODULE_SEED = 11
TIME_STEP = 42
RUN_ID = "run_golden"


class StubManager:
    """In-memory stand-in for ``AssignmentManager`` serving one tick state."""

    def __init__(
        self,
        trucks: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        claimed_order_ids: list[str],
        busy_ids: list[str],
    ):
        self._trucks = trucks
        self._orders = orders
        self._claimed = set(claimed_order_ids)
        self._busy = list(busy_ids)

    def list_trucks(self, projection=None, online_only=True):
        return list(self._trucks)

    def list_unassigned_orders(self, projection=None):
        return list(self._orders)

    def order_ids_with_open_haul(self):
        return set(self._claimed)

    def active_haul_truck_ids(self):
        return list(self._busy)

    def online_state_name(self):
        return "online"

    def resolve_facility_resource_id(self, name):
        return None


class StubMessenger:
    """Records every ``client.publish(topic, message)`` as the raw string pair."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.client = self

    def publish(self, topic, message):
        self.published.append((topic, message))


def build_app(
    profile: dict[str, Any], strategy: str, tick_state: dict[str, Any]
) -> AssignmentApp:
    """Build an ``AssignmentApp`` exactly like ``tests/test_assignment_pooled._app``.

    ``tick_state`` is deep-copied so a case can never see a previous case's
    mutations. Only the attributes the existing ``__new__`` harnesses set are set.
    """
    state = copy.deepcopy(tick_state)
    app = AssignmentApp.__new__(AssignmentApp)
    app.behavior = {"profile": copy.deepcopy(profile)}
    app.run_id = RUN_ID
    app.manager = StubManager(
        state["trucks"], state["orders"], state["claimed_order_ids"], state["busy_ids"]
    )
    app.messenger = StubMessenger()
    app._solver = get_solver(strategy, profile.get("solver_params"))
    app._solver_params = profile.get("solver_params") or {}
    app._warned_haulier_block = False
    app._share_tags = {}
    return app


def run_case(case: dict[str, Any], tick_states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Run one golden case and return its observable outputs (JSON-ready).

    Returns ``{"pairs", "share_tags", "published", "round_stats", "rng_probe"}``.
    ``round_stats`` is ``None`` for partitioned cases (it is pooled provenance).
    Consumes and reseeds the ``random`` MODULE on purpose: the partitioned path
    draws from it, and ``rng_probe`` locks how many draws a tick consumes.
    """
    tick_state = tick_states[case["tick_state"]]
    app = build_app(case["profile"], case["strategy"], tick_state)
    random.seed(MODULE_SEED)
    result = app.assign(tick_state["sim_clock"], TIME_STEP)
    app.publish(result)
    rng_probe = random.random()
    share_tags = sorted(
        ([list(key), tag] for key, tag in (app._share_tags or {}).items()),
        key=lambda row: (row[0][0], row[0][1]),
    )
    pooled = case["profile"]["planner"]["topology"] == "pooled"
    return {
        "pairs": [[str(t.get("_id")), str(o.get("_id"))] for t, o in result],
        "share_tags": share_tags,
        "published": [list(p) for p in app.messenger.published],
        "round_stats": app.round_stats() if pooled else None,
        "rng_probe": rng_probe,
    }
