"""The dashboard editForm exposes ``policySlots`` and a raw ``solver`` mirror.

What is locked (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2.4, §1.2 item 1):
- ``editForm.policySlots`` is the recipe's ``policies`` block, or, for a spec
  compiled before policy slots, the compiler's legacy translation (undeclared params
  dropped), so the editor hydrates the same selection the bundle runs.
- ``editForm.solver`` mirrors ``policySlots.dispatch.type`` as RAW text: an
  unregistered name is shown as written, never coerced to the default.
- ``editForm.policies`` (datagen role policies) is unchanged.

Why: the field name ``policies`` already means datagen roles in the form; and a
coerced solver would hide a broken spec behind a plausible default.
"""

from __future__ import annotations

import json
from pathlib import Path

from apps.container_logistics.scenario import frontend_scenario_spec as F

DOMAIN = "container-logistics-sim"
REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SPEC = REPO_ROOT / "scenarios" / "collab_smoke_12t" / "spec.json"
ENTRY = {"name": "x", "slug": "x", "simulationDays": 1,
         "agents": {"truck": 6, "order": 12, "facility": 30}}


def _form(generation_spec):
    return F.scenario_edit_form_from_detail(ENTRY, generation_spec)


def test_editform_has_policy_slots_from_the_recipe():
    block = {"dispatch": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 0}}}
    form = _form({"policies": block, "solver": "GreedyNearest"})
    assert form["policySlots"] == block
    assert form["solver"] == "GreedyNearest"
    assert set(form["policies"]) == {"truck", "order", "facility"}, "datagen roles unchanged"


def test_editform_translates_a_legacy_spec():
    """Verified to have teeth: returning ``gs.get("policies") or {}`` (no translation)
    left the legacy form with no dispatch slot at all."""
    # Round 2 (C3 compile switch): the legacy solver is Partitioned's inner (nested slots).
    form = _form({"solver": "RandomAssignment", "solverParams": {"dual_cycle_bonus_km": 2.0}})
    assert form["policySlots"] == {"dispatch": {"type": "Partitioned", "timing": "online",
                                                "params": {"inner": {"type": "RandomAssignment", "params": {}}}}}
    form = _form({"planner": {"deployment": {"type": "GreedyNearest",
                                             "params": {"dual_cycle_radius_km": 0.8}}}})
    assert form["policySlots"]["dispatch"]["params"]["inner"]["params"] == {"dual_cycle_radius_km": 0.8}
    assert form["solver"] == "GreedyNearest", "the editForm solver is the innermost per-scope solver"


def test_editform_solver_mirror_is_raw_text_not_coerced():
    """Verified to have teeth: restoring ``normalize_solver(...)`` for the mirror
    turned the typo into ``RandomAssignment``."""
    form = _form({"solver": "GreedyNeerest"})
    assert form["solver"] == "GreedyNeerest"
    # Round 2 (C4): the display fallback is nested too (the editor reads policySlots nested).
    assert form["policySlots"] == {
        "dispatch": {"type": "Partitioned", "params": {"inner": {"type": "GreedyNeerest", "params": {}}}}}


def test_editform_of_a_compiled_scenario_carries_policy_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("ORSIM_SCENARIOS_DIR", str(tmp_path / "root"))
    spec = json.loads(SMOKE_SPEC.read_text())
    spec["policies"] = {"dispatch": {"type": "Partitioned", "params": {"inner": {
        "type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 1.25}}}}}
    spec["solver"] = None
    # A body speaking policies must not also carry the legacy planner.deployment it
    # supersedes: a contradicting one is a compile error (review F1).
    spec["planner"].pop("deployment", None)
    datahub = str(tmp_path / "datahub")
    F.generate_scenario(datahub, DOMAIN, spec)
    detail = F.get_scenario_detail(datahub, DOMAIN, spec["slug"])
    form = detail["editForm"]
    assert form["policySlots"] == spec["policies"]
    assert form["solver"] == "GreedyNearest"
