"""A profile with no ``policies`` block (an old bundle): its legacy keys -> resolved policies.

Single responsibility: the legacy-PROFILE translation the framework's one profile
resolver (``dispatch/profile.resolve_profile_policies``) falls back to when a profile
carries no compiled ``policies`` block. The legacy keys ``strategy``,
``solver_params``, ``use_spatial_matching`` and ``planner.topology`` /
``planner.market`` become ONE ``planner`` entry:

- :func:`use_spatial_matching` reads the legacy flag strictly (absent means true);
- :func:`legacy_dispatch_from_profile` translates the LEAF keys into an entry;
- :func:`refuse_legacy_flag_next_to_block` refuses a legacy ``use_spatial_matching``
  other than ``true`` next to a compiled block (moved verbatim from
  ``dispatch/profile.resolve_profile_policies``, final review M8: the framework reads no
  legacy key);
- :func:`resolve_legacy_profile` wraps that leaf in the profile's decomposition
  (``compat.legacy.wrap_legacy_leaf``), resolves it, and names the legacy keys in any
  error (``_LEGACY_SOURCE_NOTE``).

Why compat: the translation names solvers and their topologies (it maps old spellings
onto ``Partitioned`` / ``PooledMarket``), which the framework may not (plan
``plan_contract_final.md`` §0 "Leftovers", §7.12). Moved verbatim from
``dispatch/profile.py`` (b70c2ec: ``_declares``, ``use_spatial_matching``,
``legacy_dispatch_from_profile``, ``_LEGACY_SOURCE_NOTE`` and the no-block branch of
``resolve_profile_policies``) in WS8; behaviour, messages and the drop-rule WARNING are
unchanged.

Imports ``policies.framework`` and the leaf ``compat.legacy`` only; never
``apps.container_logistics.assignment.*`` and never ``policies.dispatch`` (so
``dispatch/profile.py`` can import it at module top without a cycle).
"""

from __future__ import annotations

from typing import Any, Mapping

from apps.container_logistics.policies.framework import (
    PolicyError,
    ResolvedPolicy,
    get_policy_spec,
    resolve_policies_block,
)
from apps.container_logistics.policies.framework.resolve import drop_undeclared_legacy_params

from .legacy import LEGACY_LEAF_DEFAULT, wrap_legacy_leaf

__all__ = ["legacy_dispatch_from_profile", "refuse_legacy_flag_next_to_block", "resolve_legacy_profile",
           "use_spatial_matching"]

_LEGACY_SOURCE_NOTE = (
    "(This profile has no 'policies' block: its selection is translated from the legacy keys "
    "strategy, solver_params, use_spatial_matching and planner.topology/planner.market; "
    "the fix goes there, or recompile the scenario.)"
)


def _declares(spec: Any, key: str) -> bool:
    return key in spec.params_cls.__dataclass_fields__


def use_spatial_matching(prof: Mapping[str, Any]) -> bool:
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


def refuse_legacy_flag_next_to_block(prof: Mapping[str, Any]) -> None:
    """Raise ``PolicyError`` when ``prof`` (which carries a ``policies`` block) sets ``use_spatial_matching`` != true.

    The flag is superseded by the block's own ``candidates`` param; ``true`` (or absent)
    is the compiled default and is tolerated. Message unchanged from
    ``dispatch/profile.py``.
    """
    if "use_spatial_matching" in prof and prof["use_spatial_matching"] is not True:
        raise PolicyError(
            "assignment profile sets use_spatial_matching="
            f"{prof['use_spatial_matching']!r} next to a 'policies' block; it is superseded "
            "by policies.planner.params.candidates. Remove use_spatial_matching."
        )


def legacy_dispatch_from_profile(prof: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the legacy LEAF keys into a ``{"type", "timing", "params"}`` entry.

    ``type`` is ``strategy`` or the legacy leaf default (``legacy.LEGACY_LEAF_DEFAULT``).
    ``params`` keeps only the ``solver_params`` keys the policy's ``Params`` declares,
    via the framework's one drop rule (``framework.resolve.drop_undeclared_legacy_params``,
    logged at WARNING): legacy keys were never validated, and ``solver_params`` also
    feeds the framework's accounting, so a cost-blind policy legitimately ignores the
    dual-cycle keys. ``use_spatial_matching: false`` becomes ``candidates="sampled"``
    where declared. This is the per-scope solver only; :func:`resolve_legacy_profile`
    wraps it in the profile's decomposition. Raises ``UnknownPolicyError`` for an
    unregistered ``strategy``.
    """
    name = prof.get("strategy") or LEGACY_LEAF_DEFAULT
    spec = get_policy_spec("planner", name)
    params = drop_undeclared_legacy_params(
        spec, prof.get("solver_params") or {}, "profile.strategy/solver_params"
    )
    if not use_spatial_matching(prof) and _declares(spec, "candidates"):
        params["candidates"] = "sampled"
    return {"type": name, "timing": "online", "params": params}


def resolve_legacy_profile(prof: Mapping[str, Any]) -> dict[str, ResolvedPolicy]:
    """``{slot: ResolvedPolicy}`` of a profile with NO ``policies`` block (the legacy keys).

    The leaf (:func:`legacy_dispatch_from_profile`) wrapped in ``Partitioned`` or
    ``PooledMarket`` per ``planner.topology``/``planner.market``
    (``legacy.wrap_legacy_leaf``; loud on an unknown topology, component or
    ``max_rounds``), resolved with the framework's own resolver. Any ``PolicyError``
    is re-raised as its own class with :data:`_LEGACY_SOURCE_NOTE` appended, because
    the paths in it are the translation's, not the keys the author wrote.
    """
    try:
        entry = wrap_legacy_leaf(legacy_dispatch_from_profile(prof), prof.get("planner"))
        return resolve_policies_block({"planner": entry}, where="profile.policies",
                                      legacy_slots=("planner",))
    except PolicyError as exc:
        # The paths above are the translation's; name the keys the author wrote.
        raise type(exc)(f"{exc} {_LEGACY_SOURCE_NOTE}") from exc
