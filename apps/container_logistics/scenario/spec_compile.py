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
from apps.container_logistics.policies.dispatch import legacy as _legacy
from apps.container_logistics.policies.dispatch.profile import as_dispatch_node, mirror_dispatch_selection
from apps.container_logistics.policies.framework import (
    PolicyError,
    ResolvedPolicy,
    authored_after_resolution,
    get_policy_spec,
    resolve_policies_block,
)

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


def _resolve_compiled_policies(compiled) -> dict[str, ResolvedPolicy]:
    """Resolve the recipe's ``policies`` block against the registry (fail loud).

    Replaces ``recipe["policies"]`` with :func:`authored_after_resolution` (legacy
    translations lose the params their policies do not declare, at every level of
    the tree). For an AUTHORED dispatch, refuses a ``planner.deployment`` that
    contradicts its innermost solver (:func:`_refuse_deployment_contradicting_policies`),
    then writes the selection's legacy mirrors into the profile (``strategy`` = the
    leaf, ``solver_params``/``planner.deployment``, ``planner.topology``,
    ``planner.market``) via ``dispatch/profile.py::mirror_dispatch_selection`` -- the
    same helper the per-run override path uses -- so the framework's accounting (F6)
    reads exactly what the policy runs with and the run-start mirror check passes. A
    legacy translation was built FROM those keys, so its mirrors already agree.
    Raises ``SpecValidationError`` (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2.3,
    round-2 §3.6).
    """
    legacy = [s for s, p in compiled.policies_provenance.items() if p == "legacy"]
    block = compiled.recipe.get("policies")
    try:
        resolved = resolve_policies_block(block, legacy_slots=legacy)
    except PolicyError as exc:
        raise SpecValidationError(str(exc)) from exc
    compiled.recipe["policies"] = authored_after_resolution(block, resolved, legacy)
    if compiled.policies_provenance.get("planner") == "authored":
        node = as_dispatch_node(resolved["planner"])
        _refuse_deployment_contradicting_policies(compiled.authored_deployment, _innermost_with_path(node))
        # The recipe echoes an authored deployment verbatim (preprocess
        # ``_recipe_planner``), which the check above proved agrees with the leaf.
        mirror_dispatch_selection(compiled.spec.assignment_settings["profile"], node)
    return resolved


def _innermost_with_path(node: Any) -> tuple[Any, str]:
    """``(innermost node, its spec path)``: ``policies.planner`` plus ``.params.<inner>`` per level."""
    path = "policies.planner"
    while node.inner is not None:
        path += f".params.{get_policy_spec('planner', node.name).inner_param}"
        node = node.inner
    return node, path


def _same_value(a, b) -> bool:
    """Equal, and a bool never equals a number (``True == 1`` in Python; plan §2.6)."""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def _refuse_deployment_contradicting_policies(deployment: dict, leaf: tuple[Any, str]) -> None:
    """Raise ``SpecValidationError`` if ``planner.deployment`` contradicts the innermost solver.

    ``planner.deployment`` is the legacy spelling (and the compiled mirror) of the
    per-scope solver: ``leaf`` is ``(node, path)``, the innermost ``ResolvedDispatch``
    of ``policies.planner`` and its spec path (:func:`_innermost_with_path`). Next to an authored block it is tolerated only where it
    AGREES, symmetric with the ``solver``/``solverParams`` rule (deviation 17), so a
    recompiled recipe -- whose deployment mirror carries the solver's resolved
    params -- still compiles:

    - ``type`` must name the same registered policy;
    - a param the solver DECLARES must equal its resolved value (authored or
      default); otherwise the mirror would silently be overwritten;
    - a param the solver does NOT declare (Random has no dual-cycle params) is an
      accounting-only dial: the solver never reads it, so there is nothing for it
      to disagree with, and it reaches ``solver_params`` exactly as before.
    """
    if not deployment:
        return
    leaf, where = leaf
    leaf_params = leaf.params.to_dict() if leaf.params is not None else {}
    dtype = deployment.get("type")
    if dtype not in (None, ""):
        try:
            canonical = get_policy_spec("planner", str(dtype).strip()).name
        except PolicyError:
            canonical = None
        if canonical != leaf.name:
            raise SpecValidationError(
                f"spec sets planner.deployment.type={dtype!r} but {where}.type="
                f"{leaf.name!r}. planner.deployment is the legacy spelling of {where} (the "
                "innermost per-scope solver); remove planner.deployment (or make its type agree)."
            )
    params = deployment.get("params") if isinstance(deployment.get("params"), dict) else {}
    conflicts = sorted(k for k, v in params.items() if k in leaf_params and not _same_value(v, leaf_params[k]))
    if conflicts:
        detail = ", ".join(
            f"{k}={params[k]!r} vs {where}.params.{k}={leaf_params[k]!r}" for k in conflicts
        )
        raise SpecValidationError(
            f"spec sets planner.deployment.params that disagree with policies.planner "
            f"({detail}). planner.deployment.params is the legacy spelling of {where}.params "
            "and would otherwise be silently overwritten: move the values into "
            f"{where}.params and remove planner.deployment."
        )


def _profile_policies(prof: dict, resolved: dict[str, ResolvedPolicy], provenance: str) -> dict:
    """The ``profile.policies`` block for one assignment profile; consumes ``use_spatial_matching``.

    The legacy key cannot sit next to a ``policies`` block (the runtime resolver refuses
    it). Under legacy provenance ``false`` is folded into ``candidates="sampled"``
    where the policy declares ``candidates`` (the legacy translation's rule); under an
    authored block anything but ``true`` is refused. Raises ``SpecValidationError``.
    """
    block = {slot: r.to_profile() for slot, r in resolved.items()}
    if "use_spatial_matching" not in prof:
        return block
    value = prof.pop("use_spatial_matching")
    if not isinstance(value, bool):
        raise SpecValidationError(
            f"assignment profile use_spatial_matching must be true/false (got {value!r})."
        )
    if value is True:
        return block
    if provenance != "legacy":
        raise SpecValidationError(
            "assignment profile sets use_spatial_matching=false next to policies.planner; "
            "it is superseded. Set policies.planner.params.candidates='sampled' instead "
            "and remove use_spatial_matching."
        )
    # The per-scope solver is the one that declares ``candidates`` (round-2: the
    # translated top is a decomposition, whose own params carry only the ``inner``).
    node, spec = block["planner"], get_policy_spec("planner", block["planner"]["type"])
    while spec.inner_param is not None and isinstance(node["params"].get(spec.inner_param), dict):
        node = node["params"][spec.inner_param]
        spec = get_policy_spec("planner", node["type"])
    if "candidates" in node["params"]:
        node["params"]["candidates"] = "sampled"
    return block


def _patch_assignment_profiles(assignment: dict, resolved: dict[str, ResolvedPolicy], provenance: str) -> None:
    """Write ``profile.policies`` onto every generated assignment behavior (in place).

    Behaviors may share one profile dict (the builder hands out the same object), so
    each profile is patched once, and gets the round-2 format marker
    (``legacy.POLICIES_FORMAT_KEY``). Raises ``SpecValidationError`` if the
    ``profile.strategy`` mirror is not the resolved selection's innermost solver.
    """
    seen: set[int] = set()
    for agent_id, behavior in (assignment or {}).items():
        prof = behavior.setdefault("profile", {}) if isinstance(behavior, dict) else None
        if prof is None or id(prof) in seen:
            continue
        seen.add(id(prof))
        if prof.get("strategy") != resolved["planner"].leaf:
            raise SpecValidationError(
                f"assignment agent {agent_id!r}: profile.strategy={prof.get('strategy')!r} "
                f"disagrees with the innermost solver {resolved['planner'].leaf!r} of the "
                f"resolved policies.planner {resolved['planner'].label!r} (a scenario_gen.py "
                "override must not set strategy; set policies.planner)."
            )
        prof["policies"] = _profile_policies(prof, resolved, provenance)
        prof[_legacy.POLICIES_FORMAT_KEY] = _legacy.POLICIES_FORMAT


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
    resolved_policies = _resolve_compiled_policies(compiled)
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
    _patch_assignment_profiles(
        result.assignment, resolved_policies, compiled.policies_provenance.get("planner", "legacy")
    )

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
