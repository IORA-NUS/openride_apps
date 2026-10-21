"""Assignment behavior profile -> the dispatch policy that runs, and its params.

Single responsibility: map an assignment agent's behavior ``profile`` onto the
dispatch slot (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P0.7 and P1.6):

- :func:`resolve_profile_policies` is THE profile resolver (agent, run-start check,
  run stamp and per-run overrides all use it): the compiled ``policies`` block when
  present, else the legacy keys (``strategy``, ``solver_params``,
  ``use_spatial_matching``) via :func:`legacy_dispatch_from_profile`.
- :func:`resolve_dispatch_for_profile` is its dispatch entry as a ``ResolvedDispatch``.
- :func:`mirror_dispatch_params` is the one-dial mirror into ``solver_params``.
- :func:`build_dispatch_policy` instantiates the resolved policy.
- :func:`resolve_for_instance` resolves for an already-built policy object (the
  ``AssignmentApp._solver`` the tests inject) and refuses a mismatch.
- :func:`assert_assignment_profiles_resolvable` is the run-start check
  ``run_container_logistics_simulation.main`` runs before any agent spawns.

Everything raises ``PolicyError`` subclasses; nothing falls back silently.

Import contract (plan §2.7): imports only leaf ``policies`` modules at module top,
never ``apps.container_logistics.assignment.*``, so ``assignment.app`` can import it
without a cycle. Framework side of the boundary.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from apps.container_logistics.policies.framework import (
    PolicyError,
    PolicySpec,
    ResolvedPolicy,
    get_policy_spec,
    resolve_policies_block,
    spec_for_class,
)
from apps.container_logistics.policies.framework.resolve import (
    ENTRY_KEYS,
    drop_undeclared_legacy_params,
)

from .legacy import LEGACY_LEAF_DEFAULT
from .resolved import ResolvedDispatch, candidate_kind_for

logger = logging.getLogger(__name__)

__all__ = [
    "COMPILED_ENTRY_KEYS",
    "assert_assignment_profiles_resolvable",
    "build_dispatch_policy",
    "legacy_dispatch_from_profile",
    "mirror_dispatch_params",
    "resolve_dispatch_for_profile",
    "resolve_for_instance",
    "resolve_profile_policies",
]

#: The keys of a compiled ``profile.policies.<slot>`` entry (``ResolvedPolicy.to_profile``).
COMPILED_ENTRY_KEYS = ("type", "version", "kind", "timing", "params")


def assert_assignment_profiles_resolvable(collection: Mapping[str, Any]) -> None:
    """Raise ``PolicyError`` unless every assignment agent's dispatch resolves and runs.

    ``collection`` is the assignment behavior collection, ``{agent_id: behavior}``.
    Each profile goes through :func:`resolve_dispatch_for_profile` (a missing
    ``strategy`` means the slot default, as ``get_solver`` reads it); the shared
    framework resolver also refuses a timing that is not executable. The raised
    error is the original class (e.g. ``UnknownPolicyError``) with the agent id
    prefixed; a behavior that is not a mapping raises plain ``PolicyError``.
    """
    for agent_id, behavior in (collection or {}).items():
        if not isinstance(behavior, Mapping):
            raise PolicyError(
                f"assignment agent {agent_id!r}: behavior must be a mapping with a "
                f"'profile' (got {type(behavior).__name__})."
            )
        try:
            resolve_dispatch_for_profile(behavior.get("profile") or {})
        except PolicyError as exc:
            raise type(exc)(f"assignment agent {agent_id!r}: {exc}") from exc


def _declares(spec: PolicySpec, key: str) -> bool:
    return key in spec.params_cls.__dataclass_fields__


def _use_spatial_matching(prof: Mapping[str, Any]) -> bool:
    """The legacy ``use_spatial_matching`` flag, strictly boolean (absent means true).

    The pre-v2 code read it with ``bool(...)``; a non-boolean here is a malformed
    profile, so it raises instead of silently picking an arm (plan §2.6).
    """
    value = prof.get("use_spatial_matching", True)
    if not isinstance(value, bool):
        raise PolicyError(
            f"assignment profile use_spatial_matching must be true or false (got {value!r}, "
            f"a {type(value).__name__})."
        )
    return value


def legacy_dispatch_from_profile(prof: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the legacy dispatch keys into a ``{"type", "timing", "params"}`` entry.

    ``type`` is ``strategy`` or the legacy leaf default (``legacy.LEGACY_LEAF_DEFAULT``). ``params`` keeps only the
    ``solver_params`` keys the policy's ``Params`` declares, via the framework's one
    drop rule (``framework.resolve.drop_undeclared_legacy_params``, logged at
    WARNING): legacy keys were never validated, and ``solver_params`` also feeds the
    framework's accounting, so a cost-blind policy legitimately ignores the
    dual-cycle keys. ``use_spatial_matching: false`` becomes ``candidates="sampled"``
    where declared. Raises ``UnknownPolicyError`` for an unregistered ``strategy``.
    """
    name = prof.get("strategy") or LEGACY_LEAF_DEFAULT
    spec = get_policy_spec("dispatch", name)
    params = drop_undeclared_legacy_params(
        spec, prof.get("solver_params") or {}, "profile.strategy/solver_params"
    )
    if not _use_spatial_matching(prof) and _declares(spec, "candidates"):
        params["candidates"] = "sampled"
    return {"type": name, "timing": "online", "params": params}


def resolve_profile_policies(prof: Mapping[str, Any]) -> tuple[dict[str, ResolvedPolicy], str]:
    """THE resolver of an assignment profile's policies: ``({slot: ResolvedPolicy}, source)``.

    Shared by the agent (:func:`resolve_dispatch_for_profile`), the run-start check
    and the run stamp / per-run override path
    (``scenario_overrides.current_policies_for_profile``), so they cannot disagree
    (review F4). A compiled ``profile.policies`` block is resolved with the same
    strictness as compile: an unknown slot or an entry key outside
    :data:`COMPILED_ENTRY_KEYS` raises (nothing is silently stripped). Its ``version``
    and ``kind`` are explicitly IGNORED: they are a compile-time echo, the run
    stamp re-derives both from the registry, so the record is always what ran. Next
    to a block, ``use_spatial_matching`` other than ``true`` is refused (superseded
    by ``policies.dispatch.params.candidates``). Without a block the legacy keys
    are translated (:func:`legacy_dispatch_from_profile`). ``source`` is
    ``"bundle"`` or ``"legacy-profile"``. Raises ``PolicyError`` subclasses.
    """
    block = prof.get("policies")
    if block is None:
        entry = legacy_dispatch_from_profile(prof)
        resolved = resolve_policies_block({"dispatch": entry}, where="profile.policies",
                                          legacy_slots=("dispatch",))
        return resolved, "legacy-profile"
    if not isinstance(block, Mapping) or not isinstance(block.get("dispatch"), Mapping):
        raise PolicyError(
            f"assignment profile 'policies' must be an object with a 'dispatch' entry "
            f"(got {block!r}). Recompile the scenario."
        )
    if "use_spatial_matching" in prof and prof["use_spatial_matching"] is not True:
        raise PolicyError(
            "assignment profile sets use_spatial_matching="
            f"{prof['use_spatial_matching']!r} next to a 'policies' block; it is superseded "
            "by policies.dispatch.params.candidates. Remove use_spatial_matching."
        )
    authored = {slot: _authored_from_compiled(slot, entry) for slot, entry in block.items()}
    return resolve_policies_block(authored, where="profile.policies"), "bundle"


def _authored_from_compiled(slot: Any, entry: Any) -> Any:
    """``entry`` minus the ignored ``version``/``kind`` echo; refuses any other unknown key.

    A non-mapping entry is passed through for ``resolve_policies_block`` to refuse
    with its standard message (after its slot check, so an unknown slot names itself).
    """
    if not isinstance(entry, Mapping):
        return entry
    unknown = sorted(str(k) for k in entry if k not in COMPILED_ENTRY_KEYS)
    if unknown:
        raise PolicyError(
            f"profile.policies.{slot}: unknown key(s) {', '.join(repr(k) for k in unknown)}. "
            f"A compiled entry carries exactly {', '.join(COMPILED_ENTRY_KEYS)}. "
            "Fix the bundle or recompile the scenario."
        )
    return {k: v for k, v in entry.items() if k in ENTRY_KEYS}


def resolve_dispatch_for_profile(prof: Mapping[str, Any]) -> ResolvedDispatch:
    """Resolve the dispatch slot of an assignment profile (validated, never coerced).

    :func:`resolve_profile_policies`' dispatch entry as a :class:`ResolvedDispatch`
    (``source`` ``"policies"`` for a compiled block, ``"legacy-profile"`` otherwise).
    A non-executable timing is refused by the framework resolver. Raises
    ``PolicyError``.
    """
    resolved, source = resolve_profile_policies(prof)
    dispatch = resolved["dispatch"]
    spec = get_policy_spec("dispatch", dispatch.name)
    # Re-validating the already-validated dict is idempotent; it yields the typed
    # ``Params`` object the runner and policies read.
    params = spec.params_cls.from_mapping(dispatch.params, where=f"profile.policies.dispatch.params for {spec.name}")
    return ResolvedDispatch(
        name=spec.name,
        version=spec.version,
        timing=dispatch.timing,
        params=params,
        candidate_kind=candidate_kind_for(params),
        source="policies" if source == "bundle" else "legacy-profile",
    )


def mirror_dispatch_params(profile: Mapping[str, Any], dispatch_params: Mapping[str, Any]) -> None:
    """Re-mirror resolved dispatch params into a profile's accounting copies (in place).

    The ONE copy of the one-dial mirror (plan deviation 19, review F1), used by
    compile and by the per-run override path: every key ALREADY PRESENT in
    ``profile["solver_params"]`` and ``profile["planner"]["deployment"]["params"]``
    is set to ``dispatch_params[key]`` when the policy declares it. Keys the policy
    does not declare (Random has no dual-cycle params) keep their value, so a
    cost-blind policy leaves the accounting dials alone. No key is added: the
    accounting reads only what the profile already carries.
    """
    planner = profile.get("planner") if isinstance(profile.get("planner"), Mapping) else {}
    deployment = planner.get("deployment") if isinstance(planner.get("deployment"), Mapping) else {}
    for target in (profile.get("solver_params"), deployment.get("params")):
        if isinstance(target, dict):
            target.update({k: v for k, v in dispatch_params.items() if k in target})


def build_dispatch_policy(resolved: ResolvedDispatch) -> Any:
    """Instantiate the resolved policy with its validated params as a plain dict."""
    spec = get_policy_spec("dispatch", resolved.name)
    return spec.cls(params=resolved.params.to_dict())


def resolve_for_instance(policy: Any, prof: Mapping[str, Any]) -> ResolvedDispatch:
    """Resolve dispatch for an already-built ``policy`` under profile ``prof``.

    A registered class must be the one the profile resolves to (a mismatch is a
    wiring bug and raises). An unregistered class (a hand-injected test solver) gets
    ``params=None`` and the legacy candidate rule: spatial iff
    ``use_spatial_matching`` and it has ``solve_pairs``. An unregistered subclass of
    a registered policy raises: its inherited ``decide`` would read params it lacks.
    """
    spec = spec_for_class(type(policy))
    if spec is not None:
        resolved = resolve_dispatch_for_profile(prof)
        if resolved.name != spec.name:
            raise PolicyError(
                f"AssignmentApp._solver is a {spec.name} but the profile resolves dispatch "
                f"to {resolved.name}."
            )
        return resolved
    _refuse_unregistered_subclass(type(policy))
    spatial = _use_spatial_matching(prof) and hasattr(policy, "solve_pairs")
    return ResolvedDispatch(
        name=type(policy).__name__,
        version="unregistered",
        timing="online",
        params=None,
        candidate_kind="spatial" if spatial else "sampled",
        source="unregistered",
    )


def _refuse_unregistered_subclass(cls: type) -> None:
    for ancestor in cls.__mro__[1:]:
        spec = spec_for_class(ancestor)
        if spec is not None:
            raise PolicyError(
                f"{cls.__qualname__} subclasses the registered dispatch policy {spec.name} "
                "but is not registered itself; decorate it with @policy(slot='dispatch', ...)."
            )
