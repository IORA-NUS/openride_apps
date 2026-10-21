"""Legacy dispatch selection: the pre-v2 keys translated into a dispatch selection tree.

Single responsibility: the deterministic translation of the legacy spellings
(``solver``/``strategy``, ``solverParams``/``solver_params``, ``planner.deployment``,
``planner.topology``, ``planner.market``) into ``policies.dispatch``, and the reverse
echoes compile writes back into those keys. Plan
``docs/plans/policy_slots_round2_whole_tick.md`` §3.6, §4.

- :func:`legacy_topology` reads ``planner.topology`` STRICTLY (the runtime used to
  fail soft to ``partitioned``; an unknown value is now message 6).
- :func:`legacy_market_params` reads ``planner.market`` STRICTLY: component names are
  checked against the live component registries, ``max_rounds`` must be an int in
  1..100, undeclared component params are dropped at WARNING (the one legacy
  tolerance rule).
- :func:`wrap_legacy_leaf` wraps a leaf entry in ``Partitioned`` or ``PooledMarket``.
- :func:`topology_echo` / :func:`market_echo` are the compile mirrors.

The LEAF entry handed to :func:`wrap_legacy_leaf` is already translated by the caller
(``dispatch/profile.legacy_dispatch_from_profile`` for a bundle profile); its name and
params are validated by the resolver, not here. Component names, by contrast, are
validated here, because ``planner.market`` is read only when the topology is pooled.

Leaf module (plan §2.7, round-2 §4): stdlib plus ``policies.framework`` only (the
component registries are reached through ``framework.components``, imported inside
the function that needs them); never imports ``apps.container_logistics.assignment.*``.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any, Mapping

from apps.container_logistics.policies.framework.errors import PolicyError, PolicyParamsError

logger = logging.getLogger(__name__)

__all__ = [
    "LEGACY_LEAF_DEFAULT",
    "PARTITIONED",
    "POOLED_MARKET",
    "LEGACY_TOPOLOGIES",
    "LEGACY_MAX_ROUNDS_DEFAULT",
    "LEGACY_MAX_ROUNDS_RANGE",
    "MARKET_ROLES",
    "legacy_topology",
    "legacy_market_params",
    "wrap_legacy_leaf",
    "topology_echo",
    "market_echo",
]

#: The per-scope solver a legacy selection with no ``solver``/``strategy`` runs: the
#: pre-v2 default. It is the default ``inner`` of the slot default ``Partitioned``.
LEGACY_LEAF_DEFAULT = "RandomAssignment"

#: The two built-in decompositions the legacy topologies translate into.
PARTITIONED = "Partitioned"
POOLED_MARKET = "PooledMarket"

#: Every legacy ``planner.topology`` value -> its canonical topology. ``two-stage`` is
#: the deprecated alias of ``pooled``.
LEGACY_TOPOLOGIES: Mapping[str, str] = MappingProxyType(
    {"partitioned": "partitioned", "pooled": "pooled", "two-stage": "pooled"}
)
_TOP_FOR_TOPOLOGY = MappingProxyType({"partitioned": PARTITIONED, "pooled": POOLED_MARKET})
_ECHO_FOR_TOP = MappingProxyType({top: topo for topo, top in _TOP_FOR_TOPOLOGY.items()})

#: ``planner.market.max_rounds`` when absent, and its accepted range. Equal to
#: ``assignment.pooled_planner.DEFAULT_MAX_ROUNDS`` / ``MAX_ROUNDS_CEILING`` (a test
#: pins it; this leaf cannot import them).
LEGACY_MAX_ROUNDS_DEFAULT = 20
LEGACY_MAX_ROUNDS_RANGE = (1, 100)

#: ``planner.market`` role -> component kind, in the legacy block's order.
MARKET_ROLES: Mapping[str, str] = MappingProxyType({
    "offer": "market.offer",
    "claim": "market.claim",
    "arbitration": "market.arbitration",
})


def legacy_topology(planner: Any, *, where: str = "assignment profile") -> str:
    """The canonical topology (``partitioned`` or ``pooled``) of a legacy ``planner``.

    A missing planner, or a missing/``None`` ``topology``, is ``partitioned`` (the
    pre-v2 default); ``two-stage`` is ``pooled``. ``where`` prefixes the message.
    Raises ``PolicyError`` (message 6) for any other value.
    """
    topology = planner.get("topology") if isinstance(planner, Mapping) else None
    if topology is None:
        return "partitioned"
    canonical = LEGACY_TOPOLOGIES.get(topology) if isinstance(topology, str) else None
    if canonical is None:
        raise PolicyError(
            f"{where} planner.topology={topology!r} is not a legacy topology "
            f"({', '.join(LEGACY_TOPOLOGIES)}). The decomposition is now the dispatch policy: "
            f"policies.dispatch.type = {PARTITIONED} or {POOLED_MARKET}."
        )
    return canonical


def legacy_market_params(planner: Any) -> dict[str, Any]:
    """``planner.market`` as ``PooledMarket`` params (``inner`` excluded), strictly.

    Returns ``{"max_rounds": int, "offer": {type, params}, "claim": ..., "arbitration":
    ...}``; an absent role is its component kind's default with ``{}`` params, an
    absent ``max_rounds`` is 20. Params are returned as authored minus the keys the
    component does not declare (dropped at WARNING); defaults are NOT filled in (the
    resolver does that). Raises ``UnknownPolicyError`` for an unknown component name
    and ``PolicyParamsError`` for a malformed block or a ``max_rounds`` that is not an
    int in 1..100.
    """
    market = planner.get("market") if isinstance(planner, Mapping) else None
    if market is None:
        market = {}
    if not isinstance(market, Mapping):
        raise PolicyParamsError(
            f"planner.market must be an object {{offer?, claim?, arbitration?, max_rounds?}} "
            f"(got {market!r})."
        )
    out: dict[str, Any] = {"max_rounds": _max_rounds(market.get("max_rounds", LEGACY_MAX_ROUNDS_DEFAULT))}
    for role, kind in MARKET_ROLES.items():
        out[role] = _market_role(role, kind, market.get(role))
    return out


def _max_rounds(value: Any) -> int:
    low, high = LEGACY_MAX_ROUNDS_RANGE
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise PolicyParamsError(
            f"planner.market.max_rounds must be an integer in {low}..{high} (got {value!r}). "
            "It is the pooled auction's safety backstop; the auction ends on convergence."
        )
    return value


def _market_role(role: str, kind: str, raw: Any) -> dict[str, Any]:
    """One ``planner.market`` role as ``{type, params}``: name checked, params filtered."""
    # Local import: framework.components reaches the live component registries.
    from apps.container_logistics.policies.framework.components import COMPONENT_KINDS, ref_target
    from apps.container_logistics.policies.framework.params import REF_ENTRY_KEYS, declared_params

    where = f"planner.market.{role}"
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise PolicyParamsError(f"{where} must be an object {{type, params?}} (got {raw!r}).")
    unknown = sorted(str(k) for k in raw if k not in REF_ENTRY_KEYS)
    if unknown:
        raise PolicyParamsError(
            f"{where}: unknown key(s) {', '.join(repr(k) for k in unknown)}. "
            "Allowed keys: type, params (and version, which is ignored)."
        )
    name = raw.get("type")
    if name in (None, ""):  # the pre-v2 reader took a falsy type as "the default"
        name = COMPONENT_KINDS[kind].default
    if not isinstance(name, str):
        raise PolicyParamsError(f"{where}.type must name a {kind} component (got {name!r}).")
    params = raw.get("params")
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise PolicyParamsError(f"{where}.params must be an object (got {params!r}).")
    target = ref_target(kind, name.strip(), where=where)
    declared = {info.name for info in declared_params(target.params_cls)}
    dropped = sorted(str(k) for k in params if k not in declared)
    if dropped:
        logger.warning(
            "%s: legacy params %s are not declared by %s component %s and are not passed to it.",
            where, dropped, kind, target.name,
        )
    return {"type": target.name, "params": {k: v for k, v in params.items() if k in declared}}


def wrap_legacy_leaf(leaf_entry: Mapping[str, Any], planner: Any) -> dict[str, Any]:
    """Wrap a translated leaf entry in the decomposition its legacy ``planner`` names.

    ``leaf_entry`` is ``{type, timing?, params?}`` (the per-scope solver). Returns
    ``{type: Partitioned, timing?, params: {inner: {type, params}}}`` for a partitioned
    (or absent) topology, and ``{type: PooledMarket, timing?, params: {max_rounds,
    offer, claim, arbitration, inner}}`` for ``pooled``/``two-stage``, the market read
    by :func:`legacy_market_params`. The timing belongs to the slot, so it moves to the
    top entry. ``planner.market`` next to a partitioned topology is inert and not read.
    Raises ``PolicyError`` (unknown topology) and the errors of
    :func:`legacy_market_params`.
    """
    topology = legacy_topology(planner)
    inner = {"type": leaf_entry["type"], "params": dict(leaf_entry.get("params") or {})}
    if topology == "pooled":
        params: dict[str, Any] = {**legacy_market_params(planner), "inner": inner}
    else:
        params = {"inner": inner}
    entry: dict[str, Any] = {"type": _TOP_FOR_TOPOLOGY[topology]}
    if leaf_entry.get("timing") is not None:
        entry["timing"] = leaf_entry["timing"]
    entry["params"] = params
    return entry


def topology_echo(type_name: str) -> str | None:
    """The legacy ``planner.topology`` spelling of a top-level dispatch type, or ``None``.

    ``Partitioned`` -> ``partitioned``, ``PooledMarket`` -> ``pooled``; any other top (a
    central leaf, a third-party composite) has no legacy spelling.
    """
    return _ECHO_FOR_TOP.get(type_name)


def market_echo(params: Any) -> dict[str, Any]:
    """The legacy ``planner.market`` shape of resolved ``PooledMarket`` params.

    ``params`` is the typed ``Params`` or its ``to_dict()`` form. Returns ``{offer,
    claim, arbitration: {type, params}, max_rounds}`` (the order of
    ``pooled_planner.resolve_market_config``). ``None`` params (an unset nullable,
    e.g. ``OfferSpare.keep_below_km``) are omitted, so the legacy component reader
    keeps its own default, and the echo translates back to the same params.
    """
    values = params.to_dict() if hasattr(params, "to_dict") else dict(params)
    out: dict[str, Any] = {}
    for role in MARKET_ROLES:
        ref_value = values[role]
        out[role] = {
            "type": ref_value["type"],
            "params": {k: v for k, v in (ref_value.get("params") or {}).items() if v is not None},
        }
    out["max_rounds"] = values["max_rounds"]
    return out
