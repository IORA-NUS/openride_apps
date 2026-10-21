"""Assignment behavior profile -> the dispatch policy that runs, and its params.

Single responsibility: map an assignment agent's behavior ``profile`` onto the
dispatch slot (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P0.7 and P1.6):

- :func:`resolve_dispatch_for_profile` reads the compiled ``policies.dispatch`` block
  when present, else translates the legacy keys (``strategy``, ``solver_params``,
  ``use_spatial_matching``) via :func:`legacy_dispatch_from_profile`.
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
    SLOTS,
    PolicyError,
    PolicySpec,
    PolicyTimingError,
    get_policy_spec,
    spec_for_class,
)

from .resolved import ResolvedDispatch

logger = logging.getLogger(__name__)

__all__ = [
    "assert_assignment_profiles_resolvable",
    "build_dispatch_policy",
    "legacy_dispatch_from_profile",
    "resolve_dispatch_for_profile",
    "resolve_for_instance",
]

# A policy without a ``candidates`` param gets no prebuilt spatial pairs.
_KIND_WITHOUT_CANDIDATES_PARAM = "sampled"


def assert_assignment_profiles_resolvable(collection: Mapping[str, Any]) -> None:
    """Raise ``PolicyError`` unless every assignment agent's dispatch resolves and runs.

    ``collection`` is the assignment behavior collection, ``{agent_id: behavior}``.
    Each profile goes through :func:`resolve_dispatch_for_profile` (a missing
    ``strategy`` means the slot default, as ``get_solver`` reads it), and its timing
    must be executable. The raised error is the original class (e.g.
    ``UnknownPolicyError``) with the agent id prefixed; a behavior that is not a
    mapping raises plain ``PolicyError``.
    """
    executable = SLOTS["dispatch"].executable_timings
    for agent_id, behavior in (collection or {}).items():
        if not isinstance(behavior, Mapping):
            raise PolicyError(
                f"assignment agent {agent_id!r}: behavior must be a mapping with a "
                f"'profile' (got {type(behavior).__name__})."
            )
        try:
            resolved = resolve_dispatch_for_profile(behavior.get("profile") or {})
            if resolved.timing not in executable:
                raise PolicyTimingError(
                    f"dispatch timing {resolved.timing!r} is not executable yet; "
                    f"executable timings: {list(executable)}."
                )
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

    ``type`` is ``strategy`` or the slot default. ``params`` keeps only the
    ``solver_params`` keys the policy's ``Params`` declares: legacy keys were never
    validated, and ``solver_params`` also feeds the framework's accounting, so a
    cost-blind policy legitimately ignores the dual-cycle keys (dropped at DEBUG).
    ``use_spatial_matching: false`` becomes ``candidates="sampled"`` where declared.
    Raises ``UnknownPolicyError`` for an unregistered ``strategy``.
    """
    name = prof.get("strategy") or SLOTS["dispatch"].default
    spec = get_policy_spec("dispatch", name)
    raw = prof.get("solver_params") or {}
    params = {k: v for k, v in raw.items() if _declares(spec, k)}
    dropped = sorted(k for k in raw if not _declares(spec, k))
    if dropped:
        logger.debug("legacy solver_params keys %s are not %s params; not passed to it", dropped, name)
    if not _use_spatial_matching(prof) and _declares(spec, "candidates"):
        params["candidates"] = "sampled"
    return {"type": name, "timing": "online", "params": params}


def resolve_dispatch_for_profile(prof: Mapping[str, Any]) -> ResolvedDispatch:
    """Resolve the dispatch slot of an assignment profile (validated, never coerced).

    A compiled ``policies.dispatch`` block wins; with it, a ``use_spatial_matching:
    false`` key is refused because ``policies.dispatch.params.candidates`` superseded
    it. Without the block the legacy keys are translated. Raises ``PolicyError``.
    """
    block = prof.get("policies")
    if block is not None:
        return _resolve_policies_block(prof, block)
    entry = legacy_dispatch_from_profile(prof)
    return _resolve_entry(entry, source="legacy-profile", where="profile.strategy/solver_params")


def _resolve_policies_block(prof: Mapping[str, Any], block: Any) -> ResolvedDispatch:
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
    return _resolve_entry(block["dispatch"], source="policies", where="profile.policies.dispatch")


def _resolve_entry(entry: Mapping[str, Any], *, source: str, where: str) -> ResolvedDispatch:
    name = entry.get("type")
    if not isinstance(name, str) or not name:
        raise PolicyError(f"{where}: 'type' must name a dispatch policy (got {name!r}).")
    spec = get_policy_spec("dispatch", name)
    params = spec.params_cls.from_mapping(entry.get("params"), where=f"{where}.params for {name}")
    candidate_kind = getattr(params, "candidates", _KIND_WITHOUT_CANDIDATES_PARAM)
    return ResolvedDispatch(
        name=spec.name,
        version=spec.version,
        timing=_checked_timing(entry.get("timing", "online"), where),
        params=params,
        candidate_kind=candidate_kind,
        source=source,
    )


def _checked_timing(timing: Any, where: str) -> str:
    """A declared dispatch timing; executability is checked where it runs (P4 adds 'plan')."""
    allowed = SLOTS["dispatch"].timings
    if not isinstance(timing, str) or timing not in allowed:
        raise PolicyTimingError(
            f"{where}.timing={timing!r} is not a dispatch timing. Allowed: {', '.join(allowed)}."
        )
    return timing


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
