"""Dispatch golden identity, replayed through the REAL ``AssignmentApp.__init__``.

What is locked: for a subset of the stored golden cases (both strategies x both
topologies x spatial on/off x truck cap 40/500, plus the all-ties world T), an app
built by ``AssignmentApp.__init__`` -- so the dispatch policy comes from
``_build_dispatch_policy(profile)`` (resolve the profile, instantiate the policy) --
reproduces the stored pairs, share tags, published MQTT strings, pooled round stats
and module-RNG probe exactly, and the innermost solver of its ``_solver`` tree (the
bound tree's leaf, ``dispatch.bind.BoundTree.leaf``) is the class the case names.

Why: ``tests/test_dispatch_golden_identity.py`` builds the app with ``__new__`` and
injects ``get_solver(strategy, solver_params)``, so it never exercises the profile
-> policy resolution that every real agent goes through (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P1 tests, P1.10). Only ``ORSimApp.__init__``
is stubbed (it builds a user, a manager and validates the messenger, none of which
the dispatch decision reads); the stub manager/messenger and the comparison come
from the golden harness and the golden test so they cannot drift.

Verified to have teeth (each mutation reverted afterwards): building the policy
from an empty profile in ``AssignmentApp.__init__`` (``_build_dispatch_policy({})``,
i.e. the slot default) failed all 9 GreedyNearest cases here while the ``__new__``
golden test cannot see it; dropping the ``use_spatial_matching: false`` ->
``candidates="sampled"`` translation in ``legacy_dispatch_from_profile`` failed
all 4 greedy sampled cases.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest
from orsim.lifecycle import ORSimApp

from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.assignment.solver import GreedyNearestSolver, RandomAssignmentSolver
from tests import test_dispatch_golden_identity as _golden
from tests.golden.compare import published_mismatch
from tests.golden.harness import MODULE_SEED, RUN_ID, TIME_STEP, StubManager, StubMessenger

_FIXTURE = _golden._FIXTURE
_CASES = {c["case_id"]: c for c in _FIXTURE["cases"]}

_SOLVER_CLASS = {"GreedyNearest": GreedyNearestSolver, "RandomAssignment": RandomAssignmentSolver}

# Both strategies x both topologies x spatial on/off x cap 40/500, plus world T.
# (RandomAssignment has no spatial-off cases in the fixture: its default
# ``candidates`` is ``sampled`` whatever ``use_spatial_matching`` says.)
VIA_INIT_CASES = [
    "A-greedy-partitioned-chain-spatial-cap500",
    "A-greedy-partitioned-no-coop-sampled-cap40",
    "A-greedy-pooled-chain-sampled-cap500",
    "A-greedy-pooled-no-coop-spatial-cap40",
    "A-random-partitioned-chain-spatial-cap40",
    "A-random-pooled-no-coop-spatial-cap500",
    "B-greedy-partitioned-chain-sampled-cap500",
    "B-greedy-pooled-chain-spatial-cap500",
    "B-random-partitioned-no-coop-spatial-cap500",
    "B-random-pooled-chain-spatial-cap40",
    "C-greedy-partitioned-nocoop-only-spatial-cap40",
    "C-greedy-pooled-nocoop-only-sampled-cap40",
    "T-greedy-partitioned-chain-spatial-cap500",
    "T-random-pooled-no-coop-spatial-cap500",
]


def _stub_orsim_init(self, run_id, sim_clock, behavior, messenger=None, **kwargs):
    """Sets only what ``AssignmentApp.__init__`` / ``assign`` / ``publish`` read."""
    self.run_id = run_id
    self.sim_clock = sim_clock
    self.behavior = behavior
    self.messenger = messenger
    for k, v in kwargs.items():
        setattr(self, k, v)
    self.manager = None


def _run_case_via_init(case: dict[str, Any]) -> tuple[dict[str, Any], AssignmentApp]:
    """``tests.golden.harness.run_case``, with the app built by the real ``__init__``."""
    tick_state = copy.deepcopy(_FIXTURE["tick_states"][case["tick_state"]])
    app = AssignmentApp(
        run_id=RUN_ID,
        sim_clock=tick_state["sim_clock"],
        behavior={"profile": copy.deepcopy(case["profile"])},
        messenger=StubMessenger(),
        agent_helper=None,
    )
    app.manager = StubManager(
        tick_state["trucks"], tick_state["orders"],
        tick_state["claimed_order_ids"], tick_state["busy_ids"],
    )
    random.seed(MODULE_SEED)
    result = app.assign(tick_state["sim_clock"], TIME_STEP)
    app.publish(result)
    rng_probe = random.random()
    share_tags = sorted(
        ([list(key), tag] for key, tag in (app._share_tags or {}).items()),
        key=lambda row: (row[0][0], row[0][1]),
    )
    pooled = case["profile"]["planner"]["topology"] == "pooled"
    out = {
        "pairs": [[str(t.get("_id")), str(o.get("_id"))] for t, o in result],
        "share_tags": share_tags,
        "published": [list(p) for p in app.messenger.published],
        "round_stats": app.round_stats() if pooled else None,
        "rng_probe": rng_probe,
    }
    return out, app


def test_via_init_subset_covers_the_grid():
    """The chosen subset spans every axis it claims to (guards a silent edit of the list)."""
    cases = [_CASES[c] for c in VIA_INIT_CASES]
    strategies = {c["strategy"] for c in cases}
    topologies = {c["profile"]["planner"]["topology"] for c in cases}
    spatial = {(c["strategy"], c["profile"].get("use_spatial_matching")) for c in cases}
    caps = {c["profile"].get("max_trucks_per_haulier") for c in cases}
    worlds = {c["tick_state"] for c in cases}
    assert strategies == {"GreedyNearest", "RandomAssignment"}, strategies
    assert topologies == {"partitioned", "pooled"}, topologies
    assert {("GreedyNearest", True), ("GreedyNearest", False)} <= spatial, spatial
    assert caps == {40, 500}, caps
    assert worlds == {"A", "B", "C", "T"}, worlds


@pytest.mark.parametrize("case_id", VIA_INIT_CASES)
def test_golden_case_via_init(case_id, monkeypatch):
    monkeypatch.setattr(ORSimApp, "__init__", _stub_orsim_init)
    case = _CASES[case_id]
    actual, app = _run_case_via_init(case)

    expected_cls = _SOLVER_CLASS[case["strategy"]]
    tree = app._dispatch_runner(app.behavior["profile"]).tree
    assert tree.top.instance is app._solver
    leaf = tree.leaf.instance
    assert type(leaf) is expected_cls, (
        f"{case_id}: AssignmentApp.__init__ built a {type(app._solver).__name__} whose innermost "
        f"solver is a {type(leaf).__name__} from a profile whose strategy is {case['strategy']!r}"
    )

    # JSON round trip so tuples/lists and float reprs compare like the stored form.
    actual = json.loads(json.dumps(actual))
    expected = case["expected"]
    tick = _FIXTURE["tick_states"][case["tick_state"]]
    published_problem = published_mismatch(
        actual["published"], expected["published"], sim_clock=tick["sim_clock"], time_step=TIME_STEP
    )
    assert published_problem is None, f"{case_id}: 'published' differs: {published_problem}"
    for field in ("pairs", "share_tags", "round_stats", "rng_probe"):
        assert actual[field] == expected[field], (
            f"{case_id} (built via AssignmentApp.__init__): '{field}' is no longer "
            f"byte-identical to the golden capture ({_FIXTURE['header']['capture_commit']}); "
            f"{_golden._first_difference(actual[field], expected[field])}"
        )
