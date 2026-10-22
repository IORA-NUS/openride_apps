"""How ``policies`` survives a save, and how it supersedes the legacy dispatch keys.

What is locked (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2.4, §7 risk 5):
- A body that SUPPLIES ``policies`` must not inherit the saved ``solver`` /
  ``solverParams`` back next to it (they are its superseded spellings).
- A body that does not speak ``policies`` inherits it (CARRY_IF_ABSENT), and an
  explicit ``null`` is "not spoken" (UNSUPPLIED).
- The supersede rule is ASYMMETRIC: a stale legacy client sending only ``solver``
  over a carried ``policies`` is not reconciled; the compile refuses it loudly.
- ``edit_scenario``'s shallow merge applies the same rule, end to end.

Why: the dashboard now sends ``policies`` and no ``solver``; without the supersede a
dashboard save over a legacy spec would carry ``solver`` into a disagreement error,
and without the asymmetry a stale client's choice would be silently ignored.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError
from apps.container_logistics.scenario import frontend_scenario_spec as F

DOMAIN = "container-logistics-sim"
REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SPEC = REPO_ROOT / "scenarios" / "collab_smoke_12t" / "spec.json"

GREEDY_0 = {"planner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0}}}


def _legacy_previous():
    return {"name": "prev", "slug": "prev", "solver": "GreedyNearest",
            "solverParams": {"dual_cycle_bonus_km": 3.0},
            "agents": {"truck": {"count": 6}, "order": {"count": 12}, "facility": {"count": 30}},
            "earlyOrderCount": 0}


def test_body_with_policies_does_not_carry_legacy_solver():
    """Verified to have teeth: removing the ``drop_dispatch_dials_superseded_by`` call in
    ``assemble_spec`` carried the legacy solver/solverParams next to ``policies``."""
    out = F.assemble_spec({"name": "n", "slug": "prev", "policies": GREEDY_0}, DOMAIN,
                          previous=_legacy_previous())
    assert out["policies"] == GREEDY_0
    assert out["solver"] is None and out["solverParams"] is None
    Preprocessor.compile(out, domain=DOMAIN)


def test_supersede_helper_is_a_no_op_without_policies():
    prev = _legacy_previous()
    assert F.drop_dispatch_dials_superseded_by({"name": "n"}, prev) is prev
    assert F.drop_dispatch_dials_superseded_by({"policies": None}, prev) is prev
    assert F.drop_dispatch_dials_superseded_by({"policies": {}}, None) is None
    assert "solver" not in F.drop_dispatch_dials_superseded_by({"policies": {}}, prev)


def test_body_without_policies_carries_policies():
    prev = {**_legacy_previous(), "solver": None, "solverParams": None, "policies": GREEDY_0}
    out = F.assemble_spec({"name": "n", "slug": "prev"}, DOMAIN, previous=prev)
    assert out["policies"] == GREEDY_0


def test_stale_legacy_client_gets_a_loud_disagreement():
    """Verified to have teeth: making the helper also drop ``policies`` when the body
    sends ``solver`` let the stale choice silently replace the carried policy."""
    prev = {**_legacy_previous(), "solver": "GreedyNearest", "solverParams": None,
            "policies": GREEDY_0}
    out = F.assemble_spec({"name": "n", "slug": "prev", "solver": "RandomAssignment"},
                          DOMAIN, previous=prev)
    assert out["policies"] == GREEDY_0 and out["solver"] == "RandomAssignment"
    with pytest.raises(SpecValidationError, match="solver='RandomAssignment' but policies.planner"):
        Preprocessor.compile(out, domain=DOMAIN)


def test_policies_null_is_unsupplied():
    assert F.SPEC_KEY_NULL_POLICY["policies"] is F.NullPolicy.UNSUPPLIED
    assert F.SPEC_KEYS["policies"] is F.Carry.CARRY_IF_ABSENT
    assert list(F.SPEC_KEYS).index("policies") == list(F.SPEC_KEYS).index("solverParams") + 1
    prev = {**_legacy_previous(), "policies": GREEDY_0}
    out = F.assemble_spec({"name": "n", "slug": "prev", "policies": None}, DOMAIN, previous=prev)
    assert out["policies"] == GREEDY_0, "an explicit null wiped the policy selection"
    assert out["solver"] == "GreedyNearest", "a null policies must not supersede the legacy keys"


def test_edit_with_policies_patch_over_legacy_spec_compiles(tmp_path, monkeypatch):
    """A dashboard save (policies, no solver) over a legacy spec.json, end to end.

    Verified to have teeth: removing the base-side supersede call in
    ``edit_scenario`` failed this test (the base's legacy keys rode through the
    shallow merge next to the patch's ``policies``).
    """
    root = tmp_path / "root"
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    spec = json.loads(SMOKE_SPEC.read_text())
    spec["solverParams"] = {"dual_cycle_bonus_km": 3.0}
    F.generate_scenario(str(tmp_path / "datahub"), DOMAIN, spec)
    target = Path(F.scenario_dir(str(tmp_path / "datahub"), DOMAIN, spec["slug"]))
    # Simulate a pre-policy-slots spec.json on disk (legacy keys, no policies).
    legacy = json.loads((target / "spec.json").read_text())
    legacy.pop("policies", None)
    legacy["solverParams"] = {"dual_cycle_bonus_km": 3.0}
    (target / "spec.json").write_text(json.dumps(legacy))

    F.edit_scenario(str(tmp_path / "datahub"), DOMAIN, spec["slug"],
                    {"policies": deepcopy(GREEDY_0)})
    saved = json.loads((target / "spec.json").read_text())
    assert saved["policies"] == GREEDY_0
    assert saved["solver"] == "GreedyNearest" and saved["solverParams"] is None
    bundle = json.loads((target / "scenario.json").read_text())
    prof = next(iter(bundle["agents"]["assignment"].values()))["profile"]
    assert prof["policies"]["planner"]["params"]["dual_cycle_bonus_km"] == 0.0


def test_body_with_policies_drops_carried_topology_and_market(tmp_path, monkeypatch):
    """Round 2 (§3.6, §5 C4): next to a ``policies`` body the carried ``planner.topology``
    and ``planner.market`` (the legacy spellings of the decomposition) are dropped like
    ``planner.deployment``; the rest of the carried planner stays. So a dashboard save of
    a PooledMarket (or central) selection over a legacy ``partitioned`` spec compiles
    instead of tripping message 7, end to end through ``edit_scenario``.

    Verified to have teeth: removing ``"topology"`` from
    ``frontend_scenario_spec._PLANNER_DISPATCH_SPELLINGS`` failed the helper assertion
    and the edit raised message 7; removing ``"market"`` failed the helper assertion.
    """
    prev = {**_legacy_previous(), "planner": {
        "topology": "partitioned", "timing": {"mode": "online"}, "rebate_aware": False,
        "deployment": {"type": "GreedyNearest", "params": {}},
        "market": {"offer": {"type": "OfferSpare", "params": {}}, "max_rounds": 3}}}
    pooled = {"planner": {"type": "PooledMarket", "params": {"inner": {"type": "GreedyNearest"}}}}
    out = F.drop_dispatch_dials_superseded_by({"policies": pooled}, prev)
    assert out["planner"] == {"timing": {"mode": "online"}, "rebate_aware": False}
    assert prev["planner"]["topology"] == "partitioned", "the carried spec is never mutated"
    spec = F.assemble_spec({"name": "n", "slug": "prev", "policies": pooled}, DOMAIN, previous=prev)
    assert "topology" not in spec["planner"] and "market" not in spec["planner"]
    Preprocessor.compile(spec, domain=DOMAIN)

    root = tmp_path / "root"
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(root))
    smoke = json.loads(SMOKE_SPEC.read_text())  # legacy: planner.topology=partitioned + market
    F.generate_scenario(str(tmp_path / "datahub"), DOMAIN, smoke)
    F.edit_scenario(str(tmp_path / "datahub"), DOMAIN, smoke["slug"], {"policies": deepcopy(pooled)})
    target = Path(F.scenario_dir(str(tmp_path / "datahub"), DOMAIN, smoke["slug"]))
    bundle = json.loads((target / "scenario.json").read_text())
    prof = next(iter(bundle["agents"]["assignment"].values()))["profile"]
    assert prof["policies"]["planner"]["type"] == "PooledMarket"
    assert prof["planner"]["topology"] == "pooled", "the compiled mirror follows the selection"
