"""Compile a ``spec.json`` into a runnable ``scenario.json`` via the new pipeline.

This is the cut-over (plan §10 step 6): ``spec.json`` → ``Preprocessor`` →
``ScenarioGenerator`` → bundle, with **no** ``scenario_config`` global mutation and
no ``scenario_datagen`` relay. Early-order staggering (a generation post-step) runs
here. The runtime still loads only ``scenario.json``.
"""

from __future__ import annotations

from typing import Any, Optional

from apps.container_logistics.datagen.demand import recommended_early_order_count
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.overrides import (
    load_scenario_overrides,
    override_sha256,
)
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

from .scenario_bundle import compile_bundle, write_bundle


def _assert_generation_consistent(result, compiled, has_gen_override: bool) -> None:
    """Fail loudly if the *generated* agents don't match the *spec* — so a corrupt or
    stale generation (e.g. every truck/order collapsing to the default 'haulier', or a
    count that doesn't match) is never persisted as a runnable bundle.

    This guards the exact failure seen once when a scenario was compiled by an
    inconsistent intermediate code state: spec.json said 3 hauliers, but the bundle put
    all agents on the default 'haulier'. Cheap set/len checks; raises before write.

    Skipped when a Tier-2 ``scenario_gen.py`` override is present — that trusted host file
    may legitimately change counts/hauliers (customize_spec / post_generate), so the
    default-path invariants don't apply.
    """
    if has_gen_override:
        return
    spec = compiled.spec
    # 1) counts match what the Preprocessor resolved.
    for role, expected in (("truck", spec.num_trucks), ("order", spec.num_orders)):
        got = len(getattr(result, role) or {})
        if got != expected:
            raise SpecValidationError(
                f"generation inconsistency: expected {expected} {role} agents, generated {got} "
                f"— refusing to persist a mismatched bundle."
            )
    # 2) every agent's haulier is one the spec configured (catches the 'haulier' collapse).
    configured = {h.get("id") for h in (compiled.recipe.get("hauliers") or []) if h.get("id")}
    if configured:
        for role in ("truck", "order"):
            got_ids = set()
            for a in (getattr(result, role) or {}).values():
                hid = a.get("haulier_id") or (a.get("profile") or {}).get("haulier_id")
                if hid is not None:
                    got_ids.add(hid)
            stray = got_ids - configured
            if stray:
                raise SpecValidationError(
                    f"generation inconsistency: {role} agents reference haulier(s) {sorted(stray)} "
                    f"absent from the spec's hauliers {sorted(configured)} — refusing to persist a "
                    f"corrupt bundle (recompile the scenario)."
                )


def _stagger_early_orders(order_collection: dict, orsim_settings: dict, early_n: int, truck_n: int) -> None:
    """Place a small warm-up batch into sim hour 0 (behavior-preserving)."""
    order_n = len(order_collection or {})
    if truck_n > 0 and order_n > 0:
        early_n = min(early_n, recommended_early_order_count(truck_n, order_n))
    if early_n <= 0 or not order_collection:
        return
    interval = int(orsim_settings.get("STEP_INTERVAL", 240))
    steps_per_hour = max(1, 3600 // interval)
    sim_end = int(orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0))
    window = min(steps_per_hour, sim_end + 1)
    ordered_ids = sorted(
        order_collection.keys(),
        key=lambda k: int(order_collection[k].get("request_time_step", 0)),
    )
    for i, agent_id in enumerate(ordered_ids[: min(early_n, len(ordered_ids))]):
        step = min(int(i * window / max(1, early_n)), sim_end)
        order_collection[agent_id]["request_time_step"] = step


def compile_spec_to_bundle(
    spec_json: dict[str, Any],
    *,
    domain: str,
    scenario_dir: str,
    slug: str,
    name: Optional[str] = None,
    source: str = "spec",
    reference_time: str = "2020-01-01 08:00:00",
) -> dict[str, Any]:
    """Preprocess + generate + persist the bundle into ``scenario_dir``. Returns the bundle."""
    # A scenario may DECLARE its own epoch (spec key ``referenceTime``). Without this
    # the caller's default always won, so no scenario could sit on a midnight hour axis
    # and plan §18.3 step 3 was unreachable. Declared beats default; nothing global is
    # flipped (that would be the P8 shape).
    declared_ref = spec_json.get("referenceTime") if isinstance(spec_json, dict) else None
    if isinstance(declared_ref, str) and declared_ref.strip():
        reference_time = declared_ref.strip()

    compiled = Preprocessor.compile(
        spec_json, domain=domain, scenario_dir=scenario_dir, reference_time=reference_time
    )
    spec = compiled.spec
    # Optional per-scenario Tier-2 override (scenario_gen.py in the folder). Absent
    # => pure shared-engine generation. Kept for back-compat (customize_spec /
    # customize_catalog / post_generate still fire).
    overrides = load_scenario_overrides(scenario_dir)
    result = ScenarioGenerator(
        spec,
        catalog=Preprocessor.catalog(),
        overrides=overrides,
    ).generate()

    _assert_generation_consistent(result, compiled, has_gen_override=overrides is not None)

    orsim_settings = dict(compiled.orsim_settings)
    orsim_settings["BEHAVIOR_REVISION"] = spec.behavior_revision
    orsim_settings["GENERATION_SPEC"] = compiled.recipe

    _stagger_early_orders(
        result.order,
        orsim_settings,
        int(spec.order_settings.get("early_order_count", 0)),
        len(result.truck or {}),
    )

    collections = {
        "truck": result.truck,
        "order": result.order,
        "facility": result.facility,
        "assignment": result.assignment,
        "analytics": result.analytics,
        "order_lifecycle": getattr(result, "order_lifecycle", {}) or {},
    }
    bundle = compile_bundle(
        collections,
        orsim_settings,
        compiled.recipe,
        domain=domain,
        slug=slug,
        name=name or compiled.recipe.get("name") or slug,
        source=source,
        behavior_revision=spec.behavior_revision,
        generator_override_sha256=override_sha256(scenario_dir),
    )
    write_bundle(scenario_dir, bundle)
    return bundle
