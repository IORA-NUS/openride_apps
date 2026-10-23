"""The legacy mirrors of a dispatch selection: written at compile, checked at run start.

Single responsibility: the legacy keys a compiled profile still carries next to its
``policies`` block (``strategy``, ``solver_params``, ``planner.deployment``,
``planner.topology``, ``planner.market``) -- :func:`mirror_dispatch_selection` writes
them from a selection (compile and per-run overrides, so the bundle bytes are what
they were) and :func:`check_mirrors` refuses a profile whose mirrors disagree with
its compiled selection (message 13). Round 2
``docs/plans/policy_slots_round2_whole_tick.md`` §3.6.

Moved verbatim from ``policies/dispatch/profile.py`` (35b5b92: ``_MARKET_KEYS`` line
94 and the "legacy mirrors" section, lines 494-638) into the compat quarantine;
``dispatch/profile.py`` imports :func:`check_mirrors` for the run-start check; its
re-exports of both helpers were deleted in WS8 (callers import them from here).

Import contract: ``compat.legacy`` and ``policies.framework`` at module top; the
selection-tree helpers of ``dispatch/profile.py`` (which imports this module) are
imported locally, so neither module cycles.
"""

from __future__ import annotations

from typing import Any, Mapping

from apps.container_logistics.policies.framework import (
    PolicyError,
    ResolvedPolicy,
    get_policy_spec,
    list_policies,
)
from apps.container_logistics.policies.dispatch.resolved import ResolvedDispatch

from .legacy import (
    MARKET_ROLES,
    PARTITIONED,
    POOLED_MARKET,
    legacy_market_params,
    legacy_topology,
    market_echo,
    topology_echo,
)

__all__ = ["check_mirrors", "mirror_dispatch_selection"]

_MARKET_KEYS = ("max_rounds", *MARKET_ROLES)


def _tree_helpers():
    """``(as_dispatch_node, _preorder)`` of ``dispatch/profile.py`` (local import: it imports this module)."""
    from apps.container_logistics.policies.dispatch.profile import _preorder, as_dispatch_node

    return as_dispatch_node, _preorder


# --- the legacy mirrors ---------------------------------------------------------------


def _first_declared(nodes: list[ResolvedDispatch], key: str) -> tuple[bool, Any]:
    """``(True, value)`` of ``key`` in the first node (pre-order) whose params declare it."""
    for node in nodes:
        if node.params is not None and key in type(node.params).__dataclass_fields__:
            return True, getattr(node.params, key)
    return False, None


def mirror_dispatch_selection(profile: dict[str, Any], resolved: ResolvedDispatch | ResolvedPolicy) -> None:
    """Write a selection's legacy mirrors into an assignment ``profile`` (in place).

    The ONE copy of the mirror rule (round-2 §3.6, F1): ``strategy`` becomes the
    innermost leaf; every key ALREADY PRESENT in ``solver_params`` and
    ``planner.deployment.params`` takes the value of the first node of the tree
    (pre-order) that declares it (normally the leaf; a cost-blind leaf leaves the
    accounting dials alone), and an existing ``planner.deployment.type`` becomes the
    leaf; ``planner.topology`` becomes ``legacy.topology_echo`` of the top (the key is
    removed when the echo is ``None``); ``planner.market`` becomes
    ``legacy.market_echo`` of the params when the top is ``PooledMarket`` (otherwise it
    is inert pooled-arm configuration and left as is). No other key is added.
    """
    as_dispatch_node, _preorder = _tree_helpers()
    node = as_dispatch_node(resolved)
    nodes = list(_preorder(node))
    profile["strategy"] = node.leaf
    planner = profile.get("planner")
    deployment = planner.get("deployment") if isinstance(planner, dict) else None
    deployment = deployment if isinstance(deployment, dict) else {}
    for target in (profile.get("solver_params"), deployment.get("params")):
        if isinstance(target, dict):
            for key in list(target):
                found, value = _first_declared(nodes, key)
                if found:
                    target[key] = value
    if "type" in deployment:
        deployment["type"] = node.leaf
    echo = topology_echo(node.name)
    if echo is None:
        if isinstance(planner, dict):
            planner.pop("topology", None)
        return
    planner = profile.setdefault("planner", {}) if not isinstance(planner, dict) else planner
    planner["topology"] = echo
    if node.name == POOLED_MARKET:
        planner["market"] = market_echo(node.params)


def check_mirrors(profile: Mapping[str, Any], resolved: ResolvedDispatch | ResolvedPolicy) -> None:
    """Refuse a profile whose legacy mirrors disagree with its selection (message 13).

    At run start the compiled ``policies`` block is authoritative and the mirrors are
    CHECKED, never read: a present ``strategy`` must be the innermost leaf; every key
    of ``solver_params`` and ``planner.deployment.params`` that a node of the tree
    declares must equal that node's value (the first such node, pre-order: the F1
    one-dial rule ``mirror_dispatch_selection`` writes, and ``solver_params`` is what
    the framework's accounting READS); a present ``planner.topology`` must be a legacy
    topology whose decomposition is the top (``Partitioned`` <-> partitioned,
    ``PooledMarket`` <-> pooled; any other top has none); a present ``planner.market``
    must agree with ``PooledMarket``'s params when that is the top (it is inert
    otherwise). Raises ``PolicyError``.
    """
    as_dispatch_node, _preorder = _tree_helpers()
    node = as_dispatch_node(resolved)
    _check_strategy_mirror(profile.get("strategy"), node)
    _check_accounting_mirrors(profile, node)
    planner = profile.get("planner") if isinstance(profile.get("planner"), Mapping) else {}
    if planner.get("topology") is not None:
        _check_topology_mirror(planner, node)
    if node.name == POOLED_MARKET and planner.get("market") is not None:
        _check_market_mirror(planner, node)


def _check_strategy_mirror(strategy: Any, node: ResolvedDispatch) -> None:
    if strategy is None or strategy == node.leaf:
        return
    # Advise --solver only with a name it accepts (a registered per-scope solver).
    leaves = {spec.name for spec in list_policies("planner") if not spec.is_composite}
    hint = (f"change a run's solver with --solver {strategy} (ORSIM_SOLVER is translated into "
            "exactly that). " if strategy in leaves else "")
    raise PolicyError(
        f"profile strategy={strategy!r} disagrees with its compiled policies.planner "
        f"({node.label}, whose innermost solver is {node.leaf}). The compiled policies "
        f"block is authoritative; {hint}Fix the bundle or recompile."
    )


def _same_value(a: Any, b: Any) -> bool:
    """Equal, and a bool never equals a number (``True == 1`` in Python; plan §2.6)."""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def _check_accounting_mirrors(profile: Mapping[str, Any], node: ResolvedDispatch) -> None:
    """Refuse an accounting mirror (``solver_params``, ``planner.deployment.params``) the tree contradicts."""
    _as_node, _preorder = _tree_helpers()
    nodes = list(_preorder(node))
    planner = profile.get("planner") if isinstance(profile.get("planner"), Mapping) else {}
    deployment = planner.get("deployment") if isinstance(planner.get("deployment"), Mapping) else {}
    for where, mirror in (("solver_params", profile.get("solver_params")),
                          ("planner.deployment.params", deployment.get("params"))):
        if not isinstance(mirror, Mapping):
            continue
        diffs = []
        for key in mirror:
            found, value = _first_declared(nodes, key)
            if found and not _same_value(mirror[key], value):
                diffs.append(f"{key}={mirror[key]!r} vs {value!r}")
        if diffs:
            raise PolicyError(
                f"profile {where} disagrees with its compiled policies.planner ({node.label}): "
                f"{', '.join(diffs)}. The compiled policies block is authoritative and the "
                f"framework's share accounting costs with solver_params, so the two must be "
                "equal (one dial). Fix the bundle or recompile."
            )


def _check_topology_mirror(planner: Mapping[str, Any], node: ResolvedDispatch) -> None:
    topology = legacy_topology(planner)
    if topology_echo(node.name) == topology:
        return
    wanted = PARTITIONED if topology == "partitioned" else POOLED_MARKET
    raise PolicyError(
        f"profile planner.topology={planner['topology']!r} disagrees with its compiled "
        f"policies.planner ({node.name}). The compiled policies block is authoritative; "
        f"change a run's decomposition with --policy planner={wanted} "
        "(ORSIM_PLANNER_TOPOLOGY is translated into exactly that). Fix the bundle or recompile."
    )


def _check_market_mirror(planner: Mapping[str, Any], node: ResolvedDispatch) -> None:
    spec = get_policy_spec("planner", node.name)
    compiled = node.params.to_dict()
    authored = spec.params_cls.from_mapping(
        {**legacy_market_params(planner), spec.inner_param: compiled[spec.inner_param]},
        where=f"profile planner.market for {node.name}",
    ).to_dict()
    diffs = [f"{k}={authored[k]!r} vs {compiled[k]!r}" for k in _MARKET_KEYS if authored[k] != compiled[k]]
    if diffs:
        raise PolicyError(
            f"profile planner.market disagrees with its compiled policies.planner "
            f"({node.label}): {', '.join(diffs)}. The compiled policies block is "
            "authoritative; change a run's market with --policy-param planner.<key>=<value> "
            "(ORSIM_OFFER_POLICY, ORSIM_CLAIM_POLICY, ORSIM_ARBITRATION_RULE and "
            "ORSIM_MARKET_MAX_ROUNDS are translated into exactly that). Fix the bundle or recompile."
        )

