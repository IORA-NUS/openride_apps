"""The ``policies`` block in ``spec.json``: shape, legacy translation, mirrors, bundle.

What is locked (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2.1-P2.3):
- ``policies.planner`` is the ONE dispatch dial. Without it the legacy keys
  (``solver`` > ``planner.deployment.type`` > default; ``deployment.params`` then
  ``solverParams``) are translated, and ``profile.strategy`` / ``solver_params`` /
  ``planner.deployment`` are mirrors of whatever won.
- The legacy spellings may not contradict a ``policies`` block; dead
  ``planner.timing`` modes, unknown/planned/legacy slots, unknown entry keys, the
  not-yet-executable ``plan`` timing and unknown params are refused loudly.
- The bundle's assignment profile carries the RESOLVED block (version, defaults);
  the recipe echoes the AUTHORED block.
- Invariant I1: an equivalent ``policies`` block never perturbs generated agents.

Why: a policy selection that is silently coerced, dropped or contradicted runs a
different experiment than the one the author configured.
"""

from __future__ import annotations

import glob
import json
import logging
from copy import deepcopy
from pathlib import Path

import pytest

from apps.container_logistics.datagen import preprocess as P
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError
from apps.container_logistics.policies.framework import resolve as R

DOMAIN = "container-logistics-sim-test"
REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SPEC = REPO_ROOT / "scenarios" / "collab_smoke_12t" / "spec.json"


def _spec(**kw):
    spec = {
        "name": "Policies Test", "slug": "policies_test", "simulationDays": 1,
        "orderCountUnit": "total",
        "agents": {"truck": {"count": 6}, "order": {"count": 12}, "facility": {"count": 30}},
        "earlyOrderCount": 0,
    }
    spec.update(deepcopy(kw))
    return spec


def _compile(raw):
    return Preprocessor.compile(deepcopy(raw), domain=DOMAIN)


def _profile(compiled):
    return compiled.spec.assignment_settings["profile"]


def _bundle(tmp_path, monkeypatch, spec, name="b"):
    from apps.container_logistics.scenario.spec_compile import compile_spec_to_bundle

    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(tmp_path / "root"))
    out = tmp_path / name
    out.mkdir(exist_ok=True)
    return compile_spec_to_bundle(deepcopy(spec), domain=DOMAIN, scenario_dir=str(out),
                                  slug=spec["slug"])


def _assignment_profile(bundle):
    return next(iter(bundle["agents"]["assignment"].values()))["profile"]


def test_policy_entry_keys_match_the_framework():
    """datagen cannot import the registry, so its shape list is a copy: keep it equal."""
    assert P.POLICY_ENTRY_KEYS == R.ENTRY_KEYS


def test_legacy_solver_translates_to_partitioned_inner():
    """Round 2 (§3.6): the legacy solver is the per-scope solver of the decomposition
    ``planner.topology`` names (absent: ``Partitioned``)."""
    compiled = _compile(_spec(solver="GreedyNearest", solverParams={"dual_cycle_bonus_km": 3.0}))
    assert compiled.policies_provenance == {"planner": "legacy"}
    assert compiled.recipe["policies"] == {"planner": {
        "type": "Partitioned", "timing": "online",
        "params": {"inner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 3.0}}}}}
    assert _profile(compiled)["strategy"] == "GreedyNearest"
    assert _profile(compiled)["solver_params"]["dual_cycle_bonus_km"] == 3.0


def test_planner_topology_pooled_translates_to_pooled_market_with_market_block():
    """Round 2 (§3.6): ``planner.topology`` pooled (or ``two-stage``) wraps the legacy
    solver in ``PooledMarket`` whose params are the ``planner.market`` block; absent
    roles take their kind's default and an absent ``max_rounds`` is 20.

    Verified to have teeth: making ``wrap_legacy_leaf`` ignore the topology failed the
    type assertion; dropping the market read failed the params assertion."""
    market = {"offer": {"type": "OfferSpare", "params": {"keep_below_km": 3.0}}, "max_rounds": 5}
    for topology in ("pooled", "two-stage"):
        compiled = _compile(_spec(solver="GreedyNearest",
                                  planner={"topology": topology, "market": market}))
        entry = compiled.recipe["policies"]["planner"]
        assert entry == {"type": "PooledMarket", "timing": "online", "params": {
            "max_rounds": 5,
            "offer": {"type": "OfferSpare", "params": {"keep_below_km": 3.0}},
            "claim": {"type": "ClaimAllPlanned", "params": {}},
            "arbitration": {"type": "LowestCost", "params": {}},
            "inner": {"type": "GreedyNearest", "params": {}}}}, topology
        assert _profile(compiled)["strategy"] == "GreedyNearest"
    with pytest.raises(SpecValidationError, match="unknown market.offer component 'OfferAl'"):
        _compile(_spec(planner={"topology": "pooled", "market": {"offer": {"type": "OfferAl"}}}))


def _part(leaf="GreedyNearest", **params):
    inner = {"type": leaf, "params": params} if params else {"type": leaf}
    return {"planner": {"type": "Partitioned", "params": {"inner": inner}}}


_POOLED = {"planner": {"type": "PooledMarket", "params": {
    "max_rounds": 5, "offer": {"type": "OfferSpare", "params": {"keep_below_km": 3.0}},
    "inner": {"type": "GreedyNearest"}}}}


def test_authored_topology_disagreeing_with_policies_is_rejected():
    """Message 7, read from the RAW planner: agreeing spellings (and an absent or empty
    topology) compile; any value next to a central top is refused.

    Verified to have teeth: reading the normalised ``planner_cfg`` (which fills
    ``partitioned``) made the central-without-topology case raise; deleting the check
    let the pooled/Partitioned pair compile."""
    with pytest.raises(SpecValidationError) as exc:
        _compile(_spec(planner={"topology": "pooled"}, policies=_part()))
    assert str(exc.value) == (
        "spec sets planner.topology='pooled' but policies.planner.type='Partitioned'. "
        "planner.topology is the legacy spelling of the dispatch decomposition (pooled = "
        "PooledMarket, partitioned = Partitioned); remove planner.topology.")
    with pytest.raises(SpecValidationError, match="planner.topology='partitioned' but policies.planner.type='GreedyNearest'"):
        _compile(_spec(planner={"topology": "partitioned"}, policies={"planner": {"type": "GreedyNearest"}}))
    with pytest.raises(SpecValidationError, match="planner.topology='partitioned' but policies.planner.type='PooledMarket'"):
        _compile(_spec(planner={"topology": "partitioned"}, policies=_POOLED))
    _compile(_spec(planner={"topology": "partitioned"}, policies=_part()))
    _compile(_spec(planner={"topology": "two-stage"}, policies=_POOLED))
    _compile(_spec(policies={"planner": {"type": "GreedyNearest"}}))  # no planner: central is fine
    _compile(_spec(planner={"timing": {"mode": "online"}}, policies={"planner": {"type": "GreedyNearest"}}))


def test_authored_market_disagreeing_with_pooled_market_is_rejected():
    """Message 8: an authored ``planner.market`` next to ``PooledMarket`` must resolve to
    its params (defaults filled on both sides); next to another top it is inert.

    Verified to have teeth: deleting the market comparison let the first spec compile;
    comparing without filling defaults refused the agreeing (defaults-only) case."""
    with pytest.raises(SpecValidationError) as exc:
        _compile(_spec(planner={"market": {"offer": {"type": "OfferAll"}, "max_rounds": 9}}, policies=_POOLED))
    assert str(exc.value) == (
        "spec sets planner.market that disagrees with policies.planner PooledMarket params "
        "(offer.type='OfferAll' vs 'OfferSpare', offer.params.keep_below_km=None vs 3.0, "
        "offer.params.only_when_short=None vs True, max_rounds=9 vs 5). planner.market is the "
        "legacy spelling of the PooledMarket params; move the values into policies.planner.params "
        "and remove planner.market.")
    agreeing = {"offer": {"type": "OfferSpare", "params": {"keep_below_km": 3.0, "only_when_short": True}},
                "claim": {"type": "ClaimAllPlanned", "params": {}}, "max_rounds": 5}
    _compile(_spec(planner={"topology": "pooled", "market": agreeing}, policies=_POOLED))
    defaults_only = {"planner": {"type": "PooledMarket", "params": {"inner": {"type": "GreedyNearest"}}}}
    _compile(_spec(planner={"market": {}}, policies=defaults_only))
    # Inert next to another top (the pooled-arm configuration ORSIM_PLANNER_TOPOLOGY reads).
    compiled = _compile(_spec(planner={"market": {"offer": {"type": "OfferNone"}}}, policies=_part()))
    assert compiled.recipe["planner"] == {"market": {"offer": {"type": "OfferNone"}}}


def test_recipe_echoes_an_authored_planner_verbatim(tmp_path, monkeypatch):
    """Next to an authored block the recipe (= ``spec.json``) echoes the AUTHORED
    ``planner`` (absent stays absent), so the compile-normalised default topology never
    comes back as an authored ``partitioned`` that would refuse a central or PooledMarket
    recompile; a legacy translation keeps echoing the normalised block (with its mirror).

    Verified to have teeth: echoing ``deepcopy(planner_cfg)`` for an authored block in
    ``Preprocessor._recipe_planner`` made both recompiles raise message 7."""
    for policies in ({"planner": {"type": "GreedyNearest"}}, _POOLED):
        first = _bundle(tmp_path, monkeypatch, _spec(policies=policies), "a")
        assert first["recipe"]["planner"] is None
        second = _bundle(tmp_path, monkeypatch, first["recipe"], "b")
        assert _assignment_profile(second) == _assignment_profile(first)
    authored = {"timing": {"mode": "online"}, "sharing": None, "topology": "pooled"}
    first = _bundle(tmp_path, monkeypatch, _spec(planner=authored, policies=_POOLED), "c")
    assert first["recipe"]["planner"] == authored
    legacy = _compile(_spec(solver="GreedyNearest"))
    assert legacy.recipe["planner"]["topology"] == "partitioned"
    assert legacy.recipe["planner"]["deployment"]["type"] == "GreedyNearest"


def test_compiled_profile_carries_the_round2_format_marker(tmp_path, monkeypatch):
    """C2 review #7: every compiled ``profile.policies`` carries ``policies_format: 2``,
    and the run-start check refuses a block without it (a round-1 flat bundle, whose
    ``{type: GreedyNearest}`` meant the per-scope solver, would otherwise run CENTRAL).
    A per-run override writes the marker too; a legacy profile needs none.

    Verified to have teeth: dropping the marker write in
    ``spec_compile._patch_assignment_profiles`` failed the first assertion; dropping the
    ``check_policies_format`` call in ``assert_assignment_profiles_resolvable`` (the sim
    main's run-start check; the separate ``assert_policies_format`` wrapper was dead and
    is gone, verification review MINOR-11) let the round-1 profile through (exit code
    assertion)."""
    import apps.simulation.run_container_logistics_simulation as launcher
    from apps.container_logistics.policies.dispatch.legacy import check_policies_format
    from apps.container_logistics.policies.dispatch.profile import assert_assignment_profiles_resolvable
    from apps.container_logistics.scenario.scenario_overrides import apply_policy_overrides

    prof = _assignment_profile(_bundle(tmp_path, monkeypatch, _spec(solver="GreedyNearest")))
    assert prof["policies_format"] == 2
    assert_assignment_profiles_resolvable({"a0": {"profile": prof}})
    round1 = {k: v for k, v in prof.items() if k != "policies_format"}
    round1["policies"] = {"planner": {"type": "GreedyNearest", "version": "1", "kind": "operator",
                                       "timing": "online", "params": {}}}
    round1.pop("planner", None)  # the hand-deleted topology echo C2 review #7 describes
    with pytest.raises(Exception, match="policies_format=None, not 2: it was compiled before round 2"):
        assert_assignment_profiles_resolvable({"a0": {"profile": round1}})
    with pytest.raises(SystemExit) as stop:
        launcher._assert_policies_resolvable_or_exit({"a0": {"profile": round1}})
    assert stop.value.code == 2
    coll = {"a0": {"profile": {"strategy": "GreedyNearest"}}}
    check_policies_format(coll["a0"]["profile"])  # legacy: translated, no marker needed
    apply_policy_overrides(coll, {"planner": {"params": {"inner": {"params": {"dual_cycle_bonus_km": 1.0}}}}})
    assert coll["a0"]["profile"]["policies_format"] == 2


def test_planner_deployment_translates_when_no_solver():
    compiled = _compile(_spec(planner={"deployment": {
        "type": "GreedyNearest", "params": {"dual_cycle_radius_km": 0.9}}}))
    entry = compiled.recipe["policies"]["planner"]
    assert entry["type"] == "Partitioned"  # round 2: the leaf is wrapped (§3.6)
    assert entry["params"]["inner"] == {"type": "GreedyNearest", "params": {"dual_cycle_radius_km": 0.9}}
    # And with neither, the slot default (the defaults.py strategy) as the inner.
    default = _compile(_spec()).recipe["policies"]["planner"]
    assert (default["type"], default["params"]["inner"]["type"]) == ("Partitioned", "RandomAssignment")


def test_policies_block_wins_and_is_mirrored_into_strategy_and_deployment():
    """Verified to have teeth: mirroring the legacy ``deployment.type`` instead of
    ``policies.planner.type`` into ``strategy`` failed the first assertion.

    Preprocessor level only (shape, no registry): the full compile REFUSES this
    contradicting ``planner.deployment`` (review F1;
    ``test_policy_override_equals_compiled_edit.py``)."""
    compiled = _compile(_spec(
        planner={"deployment": {"type": "RandomAssignment", "params": {"dual_cycle_bonus_km": 9.0}}},
        policies={"planner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 1.5}}},
    ))
    prof = _profile(compiled)
    assert prof["strategy"] == "GreedyNearest"
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 1.5, "authored params beat deployment"
    assert prof["planner"]["deployment"] == {"type": "GreedyNearest", "params": prof["solver_params"]}
    assert compiled.recipe["solver"] == "GreedyNearest" and compiled.recipe["solverParams"] is None
    assert compiled.policies_provenance == {"planner": "authored"}


def test_solver_disagreeing_with_policies_dispatch_is_rejected():
    """Verified to have teeth: deleting the solver-vs-type check let this compile."""
    with pytest.raises(SpecValidationError) as exc:
        _compile(_spec(solver="RandomAssignment", policies={"planner": {"type": "GreedyNearest"}}))
    assert "solver='RandomAssignment' but policies.planner.type='GreedyNearest'" in str(exc.value)
    assert "legacy spelling" in str(exc.value)
    # An AGREEING solver is fine (a carried spec holds both spellings).
    _compile(_spec(solver="GreedyNearest", policies={"planner": {"type": "GreedyNearest"}}))


def test_solverparams_with_policies_block_is_rejected():
    """Verified to have teeth: deleting the solverParams check let this compile."""
    with pytest.raises(SpecValidationError, match="solverParams is the legacy spelling"):
        _compile(_spec(solverParams={"dual_cycle_bonus_km": 2.0},
                       policies={"planner": {"type": "GreedyNearest"}}))
    # Deviation (agreeing values tolerated, symmetric with `solver`): see preprocess.
    _compile(_spec(solverParams={"dual_cycle_bonus_km": 2.0},
                   policies={"planner": {"type": "GreedyNearest",
                                          "params": {"dual_cycle_bonus_km": 2.0}}}))


def _resolve_error(tmp_path, monkeypatch, policies):
    with pytest.raises(SpecValidationError) as exc:
        _bundle(tmp_path, monkeypatch, _spec(policies=policies))
    return str(exc.value)


def test_unknown_slot_rejected(tmp_path, monkeypatch):
    msg = _resolve_error(tmp_path, monkeypatch, {"dispach": {"type": "GreedyNearest"}})
    assert msg.startswith("Unknown policy slot 'dispach' in 'policies'. Known slots: planner.")


@pytest.mark.parametrize("slot", ["idle", "queue", "world.shedding", "world.duration"])
def test_removed_slot_rejected_as_unknown(tmp_path, monkeypatch, slot):
    """The four former planned slots are gone: naming one is the normal fail-loud refusal."""
    msg = _resolve_error(tmp_path, monkeypatch, {slot: {"type": "Stay"}})
    assert msg.startswith(f"Unknown policy slot {slot!r} in 'policies'. Known slots: planner.")


def test_market_component_is_not_a_slot(tmp_path, monkeypatch):
    """Round 2: offer/claim are PooledMarket components, no longer (legacy) slots."""
    msg = _resolve_error(tmp_path, monkeypatch, {"market.offer": {"type": "OfferAll"}})
    assert msg.startswith("Unknown policy slot 'market.offer' in 'policies'. Known slots: planner.")


def test_unknown_entry_key_rejected():
    with pytest.raises(SpecValidationError, match=r"policies.planner: unknown key\(s\) 'parms'"):
        _compile(_spec(policies={"planner": {"type": "GreedyNearest", "parms": {}}}))


def test_plan_timing_rejected_with_p4_message(tmp_path, monkeypatch):
    msg = _resolve_error(tmp_path, monkeypatch,
                         {"planner": {"type": "GreedyNearest", "timing": "plan"}})
    assert msg == ("policies.planner.timing='plan' is defined but not executable yet: the "
                   "held-plan dispatcher lands with P4. Use 'online'.")
    msg = _resolve_error(tmp_path, monkeypatch,
                         {"planner": {"type": "GreedyNearest", "timing": "weekly"}})
    assert msg == "policies.planner.timing='weekly' is not a planner timing. Allowed: online, plan."


def test_planner_timing_static_rejected_with_translation():
    """Verified to have teeth: removing the mode check let 'static' compile silently."""
    for mode in ("static", "hybrid"):
        with pytest.raises(SpecValidationError) as exc:
            _compile(_spec(planner={"timing": {"mode": mode}}))
        assert str(exc.value) == (
            f"planner.timing.mode={mode!r} has never had a runtime reader; dispatch timing "
            "is now policies.planner.timing ('online'; 'plan' lands with P4).")
    _compile(_spec(planner={"timing": {"mode": "online"}}))


def test_unknown_param_rejected_in_policies_block(tmp_path, monkeypatch):
    msg = _resolve_error(tmp_path, monkeypatch,
                         {"planner": {"type": "GreedyNearest", "params": {"bonus_km": 1}}})
    assert "unknown key 'bonus_km'" in msg and "Declared params: candidates" in msg


def test_undeclared_param_dropped_with_warning_in_legacy_translation(tmp_path, monkeypatch, caplog):
    """Random declares no dual-cycle params; legacy solver_params were never validated.

    Verified to have teeth: passing ``legacy_slots=()`` from spec_compile made this
    compile fail with "unknown key 'dual_cycle_bonus_km'".
    """
    with caplog.at_level(logging.WARNING):
        bundle = _bundle(tmp_path, monkeypatch, _spec(
            solver="RandomAssignment", solverParams={"dual_cycle_bonus_km": 7.0}))
    assert any("not declared by planner RandomAssignment" in r.getMessage() for r in caplog.records)
    # Round 2: the legacy solver is Partitioned's inner; its undeclared params are dropped there.
    assert bundle["recipe"]["policies"]["planner"]["params"] == {"inner": {"type": "RandomAssignment", "params": {}}}
    prof = _assignment_profile(bundle)
    assert prof["policies"]["planner"]["params"]["inner"]["params"] == {"candidates": "sampled"}
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 7.0, "accounting keeps the legacy value"


def test_bundle_profile_carries_resolved_policies_with_version_and_defaults(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path, monkeypatch, _spec(
        policies={"planner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0}}}))
    prof = _assignment_profile(bundle)
    assert prof["policies"] == {"planner": {
        "type": "GreedyNearest", "version": "1", "kind": "operator", "timing": "online",
        "params": {"candidates": "spatial", "dual_cycle_bonus_km": 0.0, "dual_cycle_radius_km": 0.5}}}
    assert prof["strategy"] == "GreedyNearest"
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 0.0, "accounting mirrors the policy"
    from apps.container_logistics.policies.dispatch.profile import resolve_dispatch_for_profile

    resolved = resolve_dispatch_for_profile(prof)
    assert resolved.source == "policies" and resolved.params.dual_cycle_bonus_km == 0.0


def test_stale_deployment_mirror_never_outlives_an_authored_param(tmp_path, monkeypatch):
    """A removed authored param never leaves an old mirrored value driving accounting.

    Under an authored block the recipe (= ``spec.json``) echoes no deployment mirror,
    so hand-editing only ``policies`` recompiles with the policy default in
    ``solver_params``. A spec that still carries a contradicting legacy
    ``planner.deployment`` (a first compile from a legacy ``spec.json``) is a LOUD
    compile error naming the fix (review F1); the editor/CLI save path drops the
    carried mirror next to a ``policies`` body (``drop_dispatch_dials_superseded_by``).
    Verified to have teeth: echoing the mirror (``deepcopy(planner_cfg)``) in
    ``Preprocessor._recipe_planner`` made the hand-edit recompile raise; disabling the
    params check in ``spec_compile._refuse_deployment_contradicting_policies`` let the
    legacy-mirror spec compile; removing the ``planner.deployment`` drop in
    ``drop_dispatch_dials_superseded_by`` made the editor-path compile raise.
    """
    from apps.container_logistics.scenario import frontend_scenario_spec as F

    first = _bundle(tmp_path, monkeypatch, _spec(
        policies={"planner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0}}}), "a")
    hand_edited = deepcopy(first["recipe"])
    hand_edited["policies"] = {"planner": {"type": "GreedyNearest"}}
    prof = _assignment_profile(_bundle(tmp_path, monkeypatch, hand_edited, "b"))
    assert prof["policies"]["planner"]["params"]["dual_cycle_bonus_km"] == 5.0
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 5.0

    legacy_first = _bundle(tmp_path, monkeypatch, _spec(
        solver="GreedyNearest", solverParams={"dual_cycle_bonus_km": 0}), "c")
    assert legacy_first["recipe"]["planner"]["deployment"]["params"]["dual_cycle_bonus_km"] == 0
    stale = deepcopy(legacy_first["recipe"])
    # Round 2: the legacy recipe's planner.topology='partitioned' agrees with Partitioned.
    stale["policies"] = {"planner": {"type": "Partitioned", "params": {"inner": {"type": "GreedyNearest"}}}}
    with pytest.raises(SpecValidationError, match="planner.deployment.params that disagree"):
        _bundle(tmp_path, monkeypatch, stale, "d")

    saved = F.assemble_spec({"name": "Policies Test", "slug": "policies_test",
                             "policies": {"planner": {"type": "GreedyNearest"}}},
                            DOMAIN, previous=deepcopy(legacy_first["recipe"]))
    prof = _assignment_profile(_bundle(tmp_path, monkeypatch, saved, "e"))
    assert prof["policies"]["planner"]["params"]["dual_cycle_bonus_km"] == 5.0
    assert prof["solver_params"]["dual_cycle_bonus_km"] == 5.0


def test_legacy_use_spatial_false_in_a_profile_is_folded_and_removed(tmp_path, monkeypatch):
    """A profile key arriving via a scenario_gen.py override must not reach the bundle
    next to a ``policies`` block (the runtime resolver refuses that pair).

    Verified to have teeth: replacing ``prof.pop`` with ``prof.get`` in
    ``_profile_policies`` left the key on the profile next to the ``policies`` block.
    """
    from apps.container_logistics.policies.dispatch.profile import resolve_dispatch_for_profile
    from apps.container_logistics.scenario import spec_compile as SC

    resolved = R.resolve_policies_block({"planner": {"type": "GreedyNearest"}})
    prof = {"strategy": "GreedyNearest", "use_spatial_matching": False}
    SC._patch_assignment_profiles({"a0": {"profile": prof}}, resolved, "legacy")
    assert "use_spatial_matching" not in prof
    assert prof["policies"]["planner"]["params"]["candidates"] == "sampled"
    assert resolve_dispatch_for_profile(prof).params.candidates == "sampled"
    authored = {"strategy": "GreedyNearest", "use_spatial_matching": False}
    with pytest.raises(SpecValidationError, match="candidates='sampled' instead"):
        SC._patch_assignment_profiles({"a0": {"profile": authored}}, resolved, "authored")


def test_recipe_echoes_authored_not_resolved(tmp_path, monkeypatch):
    authored = {"planner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0}}}
    bundle = _bundle(tmp_path, monkeypatch, _spec(policies=authored))
    assert bundle["recipe"]["policies"] == authored, "defaults/version must not leak into the recipe"
    assert bundle["settings"]["GENERATION_SPEC"]["policies"] == authored


def test_policies_block_never_perturbs_generated_agents(tmp_path, monkeypatch):
    """Invariant I1: trucks, orders and facilities are byte-equal with and without an
    equivalent ``policies`` block (the selection is carried data, not a sampling input),
    for the NESTED blocks of both legacy topologies (round 2): ``Partitioned{inner}`` and
    ``PooledMarket{market, inner}`` (the authored block replaces ``planner.topology`` /
    ``planner.market``, as the editor saves it).

    Verified to have teeth: adding ``+ (1 if raw.get("policies") else 0)`` to the
    master seed in ``Preprocessor.compile`` failed the agent comparison.
    """
    base = json.loads(SMOKE_SPEC.read_text())
    for topology in ("partitioned", "pooled"):
        spec = deepcopy(base)
        spec["planner"]["topology"] = topology
        legacy = _bundle(tmp_path, monkeypatch, spec, f"legacy_{topology}")
        with_block = deepcopy(spec)
        with_block["policies"] = deepcopy(legacy["recipe"]["policies"])
        assert with_block["policies"]["planner"]["params"]["inner"]["type"] == "GreedyNearest"
        with_block["solver"] = None
        for key in ("deployment", "topology", "market"):
            with_block["planner"].pop(key)
        authored = _bundle(tmp_path, monkeypatch, with_block, f"authored_{topology}")
        for role in ("truck", "order", "facility"):
            a = json.dumps(legacy["agents"][role], sort_keys=True)
            b = json.dumps(authored["agents"][role], sort_keys=True)
            assert a == b, f"[{topology}] {role} agents changed when the policies block was added"
        assert _assignment_profile(legacy)["policies"] == _assignment_profile(authored)["policies"]
        assert _assignment_profile(legacy)["planner"]["topology"] == topology


@pytest.mark.parametrize("path", sorted(glob.glob(str(REPO_ROOT / "scenarios" / "*" / "spec.json"))))
def test_every_shipped_spec_translates_to_partitioned_with_its_solver(path):
    """Shape-only: each shipped spec's dispatch translation is Partitioned around its old solver."""
    raw = json.loads(Path(path).read_text())
    planner_cfg = Preprocessor._normalize_planner(raw.get("planner"))
    expected = raw.get("solver") or (planner_cfg.get("deployment") or {}).get("type") or "RandomAssignment"
    authored, provenance = Preprocessor._normalize_policies(raw, planner_cfg)
    assert provenance["planner"] == ("authored" if "policies" in raw else "legacy")
    assert authored["planner"]["type"] == "Partitioned"
    assert authored["planner"]["params"]["inner"]["type"] == expected


def test_the_old_dispatch_slot_name_fails_loud_with_the_rename_hint(tmp_path, monkeypatch):
    """The slot was renamed ``dispatch`` -> ``planner`` with NO alias: the old key is an
    unknown slot, and the refusal says what it became."""
    msg = _resolve_error(tmp_path, monkeypatch, {"dispatch": {"type": "GreedyNearest"}})
    assert msg.startswith("Unknown policy slot 'dispatch' in 'policies'. Known slots: planner. "
                          "(The slot was renamed to 'planner'.)")


def test_the_old_dispatch_slot_name_is_refused_on_every_cli_path():
    from apps.container_logistics.policies.framework import (
        UnknownSlotError, get_policy_spec, merge_policy_overrides, parse_policy_cli_args,
        resolve_policies_block)
    from apps.container_logistics.policies.framework.errors import PolicyError

    hint = r"\(The slot was renamed to 'planner'\.\)"
    with pytest.raises(UnknownSlotError, match=hint):
        get_policy_spec("dispatch", "GreedyNearest")
    with pytest.raises(PolicyError, match=r"\(The slot 'dispatch' was renamed to 'planner'\.\)"):
        parse_policy_cli_args([], ["dispatch.inner=GreedyNearest"])
    with pytest.raises(UnknownSlotError, match=hint):
        merge_policy_overrides(resolve_policies_block({}),
                               parse_policy_cli_args(["dispatch=GreedyNearest"], []))


def test_legacy_planner_keys_and_policies_planner_do_not_collide(tmp_path, monkeypatch):
    """``planner.topology`` / ``planner.market`` (the legacy profile block, parent =
    the spec) and ``policies.planner`` (the slot, parent = ``policies``) share a word,
    not a key: both are read, from their own parents, in one spec."""
    spec = _spec(
        planner={"topology": "pooled", "market": {"max_rounds": 5}},
        policies={"planner": {"type": "PooledMarket",
                              "params": {"max_rounds": 5, "inner": {"type": "GreedyNearest"}}}})
    compiled = _compile(spec)
    assert compiled.policies_provenance == {"planner": "authored"}
    entry = compiled.recipe["policies"]["planner"]
    assert entry["type"] == "PooledMarket" and entry["params"]["inner"]["type"] == "GreedyNearest"
    prof = _assignment_profile(_bundle(tmp_path, monkeypatch, spec))
    assert prof["planner"]["topology"] == "pooled" and prof["planner"]["market"]["max_rounds"] == 5
    assert set(prof["policies"]) == {"planner"} and prof["policies"]["planner"]["type"] == "PooledMarket"
    assert "topology" not in prof["policies"]["planner"]
    assert prof["strategy"] == "GreedyNearest"
    # A contradiction between the two is still caught (message 7), so they are not merged.
    with pytest.raises(SpecValidationError, match=r"spec sets planner.topology='partitioned' but "
                                                  r"policies.planner.type='PooledMarket'"):
        _compile(_spec(planner={"topology": "partitioned"},
                       policies={"planner": {"type": "PooledMarket"}}))
