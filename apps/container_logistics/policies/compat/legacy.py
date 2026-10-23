"""Legacy dispatch selection: the pre-v2 keys translated into a dispatch selection tree.

Single responsibility: the deterministic translation of the legacy spellings
(``solver``/``strategy``, ``solverParams``/``solver_params``, ``planner.deployment``,
``planner.topology``, ``planner.market``) into ``policies.planner``, and the reverse
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
- :func:`check_authored_planner` refuses an authored ``planner.topology`` /
  ``planner.market`` that contradicts an authored ``policies.planner`` (messages 7, 8).
- :data:`POLICIES_FORMAT` / :func:`check_policies_format`: the bundle-format marker a
  round-2 compile (and per-run override) writes next to ``profile.policies``, so a
  round-1 flat block (whose ``{type: GreedyNearest}`` meant the per-scope solver, and
  now means a CENTRAL one) is refused at run start instead of silently changing arm.

The LEAF entry handed to :func:`wrap_legacy_leaf` is already translated by the caller
(``compat/legacy_profile.legacy_dispatch_from_profile`` for a bundle profile); its name and
params are validated by the resolver, not here. Component names, by contrast, are
validated here, because ``planner.market`` is read only when the topology is pooled.

Leaf module (plan §2.7, round-2 §4): stdlib plus ``policies.framework`` only (the
component registries are reached through ``framework.components``, imported inside
the function that needs them); never imports ``apps.container_logistics.assignment.*``.

Moved verbatim from ``policies/dispatch/legacy.py`` (35b5b92, lines 1-374) into the
compat quarantine; the old path's re-export shim was deleted in WS8.
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
    "check_authored_planner",
    "POLICIES_FORMAT",
    "POLICIES_FORMAT_KEY",
    "check_policies_format",
]

#: ``profile[POLICIES_FORMAT_KEY]``: the format of the ``profile.policies`` block next to
#: it. 2 = round 2 (a dispatch selection is a tree; a flat leaf top is central). Absent
#: next to ``policies``: a round-1 compile (refused, :func:`check_policies_format`).
POLICIES_FORMAT_KEY = "policies_format"
POLICIES_FORMAT = 2

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
    _check_planner(planner, where)
    topology = planner.get("topology") if planner is not None else None
    if topology is None:
        return "partitioned"
    canonical = LEGACY_TOPOLOGIES.get(topology) if isinstance(topology, str) else None
    if canonical is None:
        raise PolicyError(
            f"{where} planner.topology={topology!r} is not a legacy topology "
            f"({', '.join(LEGACY_TOPOLOGIES)}). The decomposition is now the planner policy: "
            f"policies.planner.type = {PARTITIONED} or {POOLED_MARKET}."
        )
    return canonical


def _check_planner(planner: Any, where: str) -> None:
    """A legacy ``planner`` is absent (``None``) or an object; anything else is loud (never "partitioned")."""
    if planner is not None and not isinstance(planner, Mapping):
        raise PolicyParamsError(
            f"{where} planner must be an object {{topology?, market?, ...}} (got {planner!r}). "
            "Fix the profile or recompile the scenario."
        )


def legacy_market_params(planner: Any) -> dict[str, Any]:
    """``planner.market`` as ``PooledMarket`` params (``inner`` excluded), strictly.

    Returns ``{"max_rounds": int, "offer": {type, params}, "claim": ..., "arbitration":
    ...}``; an absent role is its component kind's default with ``{}`` params, an
    absent (or ``null``) ``max_rounds`` is 20. Params are returned as authored minus the keys the
    component does not declare (dropped at WARNING); defaults are NOT filled in (the
    resolver does that). Raises ``UnknownPolicyError`` for an unknown component name
    and ``PolicyParamsError`` for a malformed block (a non-object ``planner``, a padded
    component name) or a ``max_rounds`` that is not an int in 1..100.
    """
    _check_planner(planner, "assignment profile")
    market = planner.get("market") if planner is not None else None
    if market is None:
        market = {}
    if not isinstance(market, Mapping):
        raise PolicyParamsError(
            f"planner.market must be an object {{offer?, claim?, arbitration?, max_rounds?}} "
            f"(got {market!r})."
        )
    # ``null`` means "the default", as an absent key does (and as preprocess reads it).
    rounds = market.get("max_rounds")
    out: dict[str, Any] = {"max_rounds": _max_rounds(LEGACY_MAX_ROUNDS_DEFAULT if rounds is None else rounds)}
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
    if name != name.strip():
        raise PolicyParamsError(
            f"{where}.type {name!r} has leading/trailing whitespace; write {name.strip()!r} "
            "(component names are matched exactly)."
        )
    target = ref_target(kind, name, where=where)
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


def check_authored_planner(planner: Any, dispatch: Mapping[str, Any]) -> None:
    """Refuse a legacy ``planner`` spelling that contradicts an authored ``policies.planner``.

    ``planner`` is the RAW authored block (never the compile-normalised one, which
    fills a default topology); ``dispatch`` the authored ``policies.planner`` entry.
    Round-2 §3.6:

    - an authored ``planner.topology`` (a non-empty value) must equal the echo of the
      top type (``Partitioned`` <-> ``partitioned``, ``PooledMarket`` <-> ``pooled`` or
      ``two-stage``); next to any other top every value is an error (message 7);
    - an authored ``planner.market`` next to ``PooledMarket`` must resolve to the same
      ``offer``/``claim``/``arbitration``/``max_rounds`` as its params, defaults filled
      on both sides (message 8); next to any other top it is inert pooled-arm
      configuration (read only by ``ORSIM_PLANNER_TOPOLOGY``) and is not read.

    Params that do not resolve on the ``policies`` side are left for the resolver to
    name. Raises ``PolicyError`` (and the errors of :func:`legacy_market_params` for a
    malformed authored ``planner.market`` next to ``PooledMarket``).
    """
    if not isinstance(planner, Mapping) or not isinstance(dispatch, Mapping):
        return
    top = dispatch.get("type")
    top = top.strip() if isinstance(top, str) else top
    topology = planner.get("topology")
    if topology not in (None, ""):
        canonical = LEGACY_TOPOLOGIES.get(topology) if isinstance(topology, str) else None
        if canonical is None or _TOP_FOR_TOPOLOGY[canonical] != top:
            raise PolicyError(
                f"spec sets planner.topology={topology!r} but policies.planner.type={top!r}. "
                "planner.topology is the legacy spelling of the dispatch decomposition (pooled = "
                f"{POOLED_MARKET}, partitioned = {PARTITIONED}); remove planner.topology."
            )
    if top != POOLED_MARKET or planner.get("market") is None:
        return
    disagree = _market_disagreements(planner, dispatch.get("params"))
    if disagree:
        raise PolicyError(
            "spec sets planner.market that disagrees with policies.planner PooledMarket params "
            f"({', '.join(disagree)}). planner.market is the legacy spelling of the PooledMarket "
            "params; move the values into policies.planner.params and remove planner.market."
        )


def _market_disagreements(planner: Mapping[str, Any], authored_params: Any) -> list[str]:
    """``["<k>=<planner.market value> vs <policies value>", ...]`` over the resolved market params."""
    # Local import: the registry discovers the policy library (agent-side modules).
    from apps.container_logistics.policies.framework.registry import get_policy_spec

    params_cls = get_policy_spec("planner", POOLED_MARKET).params_cls
    authored = dict(authored_params) if isinstance(authored_params, Mapping) else {}
    try:
        ours = params_cls.from_mapping(authored, where="policies.planner.params").to_dict()
    except PolicyError:
        return []  # the resolver names the bad policies value with its own path
    legacy = legacy_market_params(planner)
    if "inner" in authored:
        legacy["inner"] = authored["inner"]
    theirs = params_cls.from_mapping(legacy, where="planner.market").to_dict()
    out: list[str] = []
    for key in (*MARKET_ROLES, "max_rounds"):
        _diff(key, theirs.get(key), ours.get(key), out)
    return out


def _diff(path: str, a: Any, b: Any, out: list[str]) -> None:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        for key in list(a) + [k for k in b if k not in a]:
            _diff(f"{path}.{key}", a.get(key), b.get(key), out)
    elif isinstance(a, bool) != isinstance(b, bool) or a != b:
        out.append(f"{path}={a!r} vs {b!r}")


def check_policies_format(profile: Mapping[str, Any]) -> None:
    """Refuse a ``profile.policies`` block that is not the round-2 format.

    A profile without ``policies`` is a legacy profile (translated, always accepted).
    Next to ``policies`` the marker must be exactly the int :data:`POLICIES_FORMAT`
    (not a bool, not a float such as ``2.0``, not a string). An absent marker is a
    round-1 bundle; any other value is an unknown format. Raises ``PolicyError``
    naming the fix (recompile).
    """
    if not isinstance(profile, Mapping) or profile.get("policies") is None:
        return
    marker = profile.get(POLICIES_FORMAT_KEY)
    from apps.container_logistics.policies.framework.slots import RENAMED_SLOTS

    block = profile["policies"]
    renamed = [old for old in RENAMED_SLOTS if isinstance(block, Mapping) and old in block]
    hint = "".join(f" (The slot {old!r} was renamed to {RENAMED_SLOTS[old]!r}.)" for old in renamed)
    if type(marker) is int and marker == POLICIES_FORMAT:
        if renamed:
            raise PolicyError(
                f"profile.policies names slot {renamed[0]!r}: the bundle was compiled before the "
                f"slot rename.{hint} Recompile the scenario (openride scenario compile <slug>); "
                "never hand-patch the bundle."
            )
        return
    if marker is None:
        raise PolicyError(
            f"profile.policies has {POLICIES_FORMAT_KEY}={marker!r}, not {POLICIES_FORMAT}: it was "
            "compiled before round 2, when a flat policies.dispatch {type: X} meant the per-scope "
            "solver X under planner.topology; it now means a CENTRAL X. Recompile the scenario "
            "(openride scenario compile <slug>); never hand-patch the bundle." + hint
        )
    raise PolicyError(
        f"profile.policies has {POLICIES_FORMAT_KEY}={marker!r}: unknown format. This build reads "
        f"exactly the integer {POLICIES_FORMAT} (compile never writes a bool, a float or a string). "
        "Recompile the scenario with this build (openride scenario compile <slug>); never "
        "hand-patch the bundle."
    )
