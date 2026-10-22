"""The legacy per-run environment variables, translated into the dispatch selection.

What is locked (``docs/plans/policy_slots_round2_whole_tick.md`` §3.6, §5 C4):
- ``ORSIM_PLANNER_TOPOLOGY`` makes the top ``PooledMarket`` / ``Partitioned`` and KEEPS
  the current per-scope solver; a new ``PooledMarket`` takes the profile's
  ``planner.market``. It runs exactly the experiment a compiled pooled scenario runs.
- ``ORSIM_OFFER_POLICY`` / ``ORSIM_CLAIM_POLICY`` / ``ORSIM_ARBITRATION_RULE`` /
  ``ORSIM_MARKET_MAX_ROUNDS`` configure ``PooledMarket`` and are LOUD without it
  (message 9); ``max_rounds`` is honoured over 1..100 (it used to ignore 11..100).
- Conflicts between the channels are loud (messages 10, 12).
- The run-name segments of every legacy input are byte-equal to ``ec085f5`` (the
  pre-branch base): `` · <solver>``, `` · <structure>``, `` · <topology>``, in that
  order. The expected names below were produced by driving ``ec085f5``'s own
  ``main()`` with the same harness (a scratch worktree), and are hardcoded.

Why: the env path is how the dashboard and the A/B scripts select arms; a silent
change of solver, market or run name corrupts a comparison nobody re-checks.
"""

from __future__ import annotations

import copy
import gzip
import json
import tempfile
from pathlib import Path

import pytest

from apps.container_logistics.policies.framework import PolicyError, UnknownPolicyError
from apps.container_logistics.scenario.scenario_overrides import (
    current_policies_for_profile,
    run_dispatch_overrides,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "container-logistics-sim"
WORLD = "collab_scale_500t_v2"  # tick states A and B of the golden fixture come from it


def _spec(slug: str) -> dict:
    return json.loads((REPO_ROOT / "scenarios" / slug / "spec.json").read_text())


def _compile(spec: dict, root: Path) -> dict:
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    folder = tempfile.mkdtemp(dir=root)
    return compile_spec_to_bundle(copy.deepcopy(spec), domain=DOMAIN, scenario_dir=folder, slug=spec["slug"])


def _coll(profile: dict) -> dict:
    return {"assignment_0": {"profile": copy.deepcopy(profile)}}


def _dispatch(coll: dict):
    return current_policies_for_profile(coll["assignment_0"]["profile"])[0]["planner"]


GREEDY_BONUS_2 = {"strategy": "GreedyNearest", "solver_params": {"dual_cycle_bonus_km": 2.0},
                  "planner": {"topology": "partitioned", "market": {
                      "offer": {"type": "OfferSpare", "params": {"keep_below_km": 4.0}}, "max_rounds": 6}}}


def test_topology_env_pooled_keeps_inner_and_market_block():
    """Verified to have teeth: resetting the carried inner on a top-type change
    (``_carried_inner`` returning ``{}``) failed the label and the bonus; ignoring
    ``planner.market`` in ``_dispatch_entry`` failed the market assertions."""
    coll = _coll(GREEDY_BONUS_2)
    merged, labels = run_dispatch_overrides(coll, topology="pooled")
    node = _dispatch(coll)
    assert node.label == "PooledMarket/GreedyNearest"
    assert node.params["inner"]["params"]["dual_cycle_bonus_km"] == 2.0, "the per-scope solver is kept"
    assert node.params["offer"] == {"type": "OfferSpare", "params": {"keep_below_km": 4.0, "only_when_short": True}}
    assert node.params["max_rounds"] == 6
    assert labels == {"planner": None, "topology": "pooled"}, "the env adds only its own segment"
    prof = coll["assignment_0"]["profile"]
    assert prof["planner"]["topology"] == "pooled" and prof["planner"]["market"]["max_rounds"] == 6
    # And back: partitioned keeps the solver too; two-stage is pooled.
    run_dispatch_overrides(coll, topology="partitioned")
    assert _dispatch(coll).label == "Partitioned/GreedyNearest"
    assert _dispatch(coll).params["inner"]["params"]["dual_cycle_bonus_km"] == 2.0
    assert run_dispatch_overrides(_coll(GREEDY_BONUS_2), topology="two-stage")[1]["topology"] == "pooled"
    with pytest.raises(PolicyError, match=r"^ORSIM_PLANNER_TOPOLOGY='nonsense' is not a legacy topology"):
        run_dispatch_overrides(_coll(GREEDY_BONUS_2), topology="nonsense")


@pytest.fixture(scope="module")
def tick_states():
    raw = gzip.open(REPO_ROOT / "tests" / "golden" / "dispatch_golden_v1.json.gz").read()
    return json.loads(raw)["tick_states"]


def _case(profile: dict, state: str = "A") -> dict:
    prof = copy.deepcopy(profile)
    prof["cooperation"]["active"] = "chain"  # share tags carry benefit_km
    return json.loads(json.dumps({"profile": prof, "strategy": prof["strategy"], "tick_state": state},
                                 sort_keys=True))


def test_topology_env_equals_compiled_pooled_scenario_outputs(tmp_path_factory, monkeypatch, tick_states):
    """``ORSIM_PLANNER_TOPOLOGY=pooled`` on the partitioned bundle == the scenario compiled
    with ``planner.topology: pooled`` (pairs, share tags, published strings, round stats,
    module RNG), on both golden tick states of the world.

    Verified to have teeth: reading the market from the PooledMarket defaults instead of
    ``planner.market`` failed it once the spec's market differed (``max_rounds`` 3);
    dropping ``_commit``'s ``mirror_dispatch_selection`` failed the profile check."""
    from tests.golden.harness import run_case

    root = tmp_path_factory.mktemp("scen")
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    spec = _spec(WORLD)
    spec.setdefault("planner", {})["market"] = {"offer": {"type": "OfferSpare", "params": {}}, "max_rounds": 3}
    base = next(iter(_compile(spec, root)["agents"]["assignment"].values()))["profile"]
    pooled_spec = copy.deepcopy(spec)
    pooled_spec["planner"]["topology"] = "pooled"
    compiled = next(iter(_compile(pooled_spec, root)["agents"]["assignment"].values()))["profile"]
    coll = _coll(base)
    run_dispatch_overrides(coll, topology="pooled")
    via_env = coll["assignment_0"]["profile"]
    assert via_env["policies"] == compiled["policies"]
    assert via_env["planner"]["topology"] == compiled["planner"]["topology"] == "pooled"
    from apps.container_logistics.policies.dispatch.profile import assert_assignment_profiles_resolvable

    assert_assignment_profiles_resolvable(coll)  # the rewritten mirrors pass the run-start check
    for state in ("A", "B"):
        got, want = run_case(_case(via_env, state), tick_states), run_case(_case(compiled, state), tick_states)
        assert got["pairs"] and got["round_stats"], f"[{state}] vacuous: the market did not run"
        for field in ("pairs", "share_tags", "published", "round_stats", "rng_probe"):
            assert got[field] == want[field], f"[{state}] env path differs from the compiled pooled scenario on {field!r}"


def test_market_env_without_pooled_market_is_loud():
    """Message 9, naming the first given variable. Verified to have teeth: deleting the
    top check in ``_dispatch_entry`` let the offer reach Partitioned, whose resolver
    then refused it with an unknown-key error instead."""
    coll = _coll(GREEDY_BONUS_2)
    with pytest.raises(PolicyError) as exc:
        run_dispatch_overrides(coll, offer="OfferSpare")
    assert str(exc.value) == (
        "ORSIM_OFFER_POLICY='OfferSpare' configures the PooledMarket dispatch, but this run's dispatch "
        "is Partitioned/GreedyNearest. Add ORSIM_PLANNER_TOPOLOGY=pooled, or select --policy "
        "planner=PooledMarket --policy-param planner.offer=OfferSpare.")
    with pytest.raises(PolicyError, match=r"^ORSIM_MARKET_MAX_ROUNDS=5 configures"):
        run_dispatch_overrides(coll, max_rounds=5)
    assert coll == _coll(GREEDY_BONUS_2), "nothing mutated"
    # With the topology in the same request (the env order: topology, then the market) it applies.
    run_dispatch_overrides(coll, topology="pooled", claim="ClaimNone", arbitration="OwnerFirst")
    node = _dispatch(coll)
    assert (node.params["claim"]["type"], node.params["arbitration"]["type"]) == ("ClaimNone", "OwnerFirst")
    assert node.params["offer"]["params"]["keep_below_km"] == 4.0, "an untouched role keeps planner.market's"
    with pytest.raises(UnknownPolicyError, match="'ClaimNope'"):
        run_dispatch_overrides(coll, claim="ClaimNope")


def test_env_conflicts_are_loud():
    """Messages 10 and 12, plus a market variable against an explicit override.

    Verified to have teeth: removing the named-top check in ``_dispatch_entry`` let the
    topology silently replace the override's type; removing ``_refuse_market_conflict``
    let ORSIM_OFFER_POLICY silently replace the override's offer."""
    with pytest.raises(PolicyError) as exc:
        run_dispatch_overrides(_coll(GREEDY_BONUS_2), topology="pooled",
                               overrides={"planner": {"type": "Partitioned"}})
    assert str(exc.value) == (
        "ORSIM_PLANNER_TOPOLOGY='pooled' selects PooledMarket but ORSIM_POLICY_OVERRIDES sets "
        "planner.type='Partitioned'; pass one of them.")
    run_dispatch_overrides(_coll(GREEDY_BONUS_2), topology="pooled",
                           overrides={"planner": {"type": "PooledMarket"}})  # agreeing is fine
    with pytest.raises(PolicyError) as exc:
        run_dispatch_overrides(_coll(GREEDY_BONUS_2), solver="RandomAssignment",
                               overrides={"planner": {"params": {"inner": {"type": "GreedyNearest"}}}})
    assert str(exc.value) == (
        "--solver 'RandomAssignment' conflicts with planner.inner.type='GreedyNearest' from "
        "--policy/--policy-param/--policy-overrides; --solver is shorthand for the innermost solver, pass one of them.")
    with pytest.raises(PolicyError, match=r"ORSIM_OFFER_POLICY='OfferSpare' conflicts with planner.offer.type='OfferAll'"):
        run_dispatch_overrides(_coll(GREEDY_BONUS_2), topology="pooled", offer="OfferSpare",
                               overrides={"planner": {"params": {"offer": {"type": "OfferAll"}}}})


def test_market_max_rounds_env_range_1_to_100():
    """Verified to have teeth: restoring the old ``1 <= rounds <= 10`` bound failed the
    11 and 100 cases; accepting a bool (``isinstance(True, int)``) failed the True case."""
    pooled = {**GREEDY_BONUS_2, "planner": {"topology": "pooled"}}
    for good, want in ((1, 1), (11, 11), (100, 100), ("42", 42), (" 7 ", 7)):
        coll = _coll(pooled)
        run_dispatch_overrides(coll, max_rounds=good)
        assert _dispatch(coll).params["max_rounds"] == want, good
    for bad in (0, 101, -1, "x", "1.5", 2.0, True):
        with pytest.raises(PolicyError, match="must be an integer in 1..100"):
            run_dispatch_overrides(_coll(pooled), max_rounds=bad)


# --- run names: byte-equal to ec085f5 ------------------------------------------------


class _Stop(Exception):
    pass


@pytest.fixture(scope="module")
def smoke_assignment(tmp_path_factory):
    root = tmp_path_factory.mktemp("smoke")
    mp = pytest.MonkeyPatch()
    mp.setenv("ORSIM_SCENARIOS_DIR", str(root))
    try:
        return _compile(_spec("collab_smoke_12t"), root)["agents"]["assignment"]
    finally:
        mp.undo()


def _run_name(monkeypatch, tmp_path, assignment: dict, env: dict) -> str:
    """Drive the sim ``main()`` up to its first ``push_run_status`` and return the run name."""
    import apps.simulation.run_container_logistics_simulation as launcher
    from apps.utils import kafka_utils

    for name in ("ORSIM_SOLVER", "ORSIM_POLICY_OVERRIDES", "ORSIM_PLANNER_TOPOLOGY", "ORSIM_OFFER_POLICY",
                 "ORSIM_CLAIM_POLICY", "ORSIM_ARBITRATION_RULE", "ORSIM_MARKET_MAX_ROUNDS",
                 "ORSIM_COOP_STRUCTURE", "ORSIM_SHARING_POLICY", "ORSIM_CAPTURE_DISPATCH", "ORSIM_HEADLESS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {"ORSIM_RUN_NAME": "base", "ORSIM_ORDER_LIFECYCLE": "agents", **env}.items():
        monkeypatch.setenv(name, value)
    collections = {"assignment": copy.deepcopy(assignment), "truck": {}, "order": {}, "facility": {}}

    class _Manager:
        def __init__(self, *args, **kwargs):
            self.orsim_settings = {}
            self.reference_time = None

        def get_agent_collection(self, role):
            return collections[role]

        def get_scenario_display_name(self):
            return "x"

    def _push(topic, run_id, status, **kwargs):
        raise _Stop(kwargs.get("run_name"))

    monkeypatch.setattr(launcher, "ScenarioManager", _Manager)
    monkeypatch.setattr(launcher, "get_datahub_dir", lambda: str(tmp_path))
    monkeypatch.setattr(launcher, "_resolve_run_id", lambda: "run_x")
    monkeypatch.setattr(launcher, "_ensure_transport_lane_topics", lambda run_id: None)
    monkeypatch.setattr(launcher, "assert_simulation_dependencies", lambda *a, **k: None)
    monkeypatch.setattr(kafka_utils, "bootstrap_kafka_for_run", lambda run_id: "topic")
    monkeypatch.setattr(kafka_utils, "flush_producer", lambda *a, **k: None)
    monkeypatch.setattr(kafka_utils, "push_run_status", _push)
    with pytest.raises(_Stop) as stop:
        launcher.main()
    return stop.value.args[0]


# Produced by ec085f5's main() through this harness (collab_smoke_12t, active structure 'pair').
_EC085F5_NAMES = {
    "solver": ({"ORSIM_SOLVER": "RandomAssignment"}, "base · RandomAssignment · pair · lifecycle:agents"),
    "topology": ({"ORSIM_PLANNER_TOPOLOGY": "pooled"}, "base · pooled · pair · lifecycle:agents"),
    "solver+structure+topology": (
        {"ORSIM_SOLVER": "RandomAssignment", "ORSIM_COOP_STRUCTURE": "no-coop",
         "ORSIM_PLANNER_TOPOLOGY": "two-stage"},
        "base · RandomAssignment · no-coop · pooled · lifecycle:agents"),
}


@pytest.mark.parametrize("legacy_bundle", [False, True], ids=["compiled", "legacy-bundle"])
@pytest.mark.parametrize("case", sorted(_EC085F5_NAMES))
def test_run_name_segments_unchanged_for_legacy_inputs(case, legacy_bundle, monkeypatch, tmp_path,
                                                       smoke_assignment, capsys):
    """Verified to have teeth: appending the topology segment next to the dispatch label
    (one segment block at the override spot) failed the solver+structure+topology case
    (``pooled`` before ``no-coop``); labelling a solver-only override with the stamp
    label (``Partitioned/RandomAssignment``) failed the solver cases."""
    env, expected = _EC085F5_NAMES[case]
    assignment = copy.deepcopy(smoke_assignment)
    if legacy_bundle:  # a bundle compiled before policy slots: no policies block
        for behavior in assignment.values():
            behavior["profile"].pop("policies")
            behavior["profile"].pop("policies_format")
    assert _run_name(monkeypatch, tmp_path, assignment, env) == expected
    out = capsys.readouterr().out
    assert "Policies effective: planner=" in out and "information_model=" in out
