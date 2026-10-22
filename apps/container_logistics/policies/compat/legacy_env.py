"""The legacy dispatch environment variables, translated into one dispatch override entry.

Single responsibility: the pre-v2 run-time spellings of a dispatch selection --
``ORSIM_PLANNER_TOPOLOGY`` (``partitioned`` / ``pooled`` / ``two-stage``) and the
pooled-market variables ``ORSIM_OFFER_POLICY`` / ``ORSIM_CLAIM_POLICY`` /
``ORSIM_ARBITRATION_RULE`` / ``ORSIM_MARKET_MAX_ROUNDS`` -- read
(:func:`read_legacy_dispatch_env`), checked (:func:`legacy_env_request`) and folded into
the ONE per-profile override entry ``scenario_overrides.run_dispatch_overrides`` merges
(:func:`apply_legacy_env`). Round 2 ``docs/plans/policy_slots_round2_whole_tick.md``
§3.6, §5 C4. ``scenario_overrides`` hands the mapping through without reading it.

Moved verbatim from ``scenario/scenario_overrides.py`` (35b5b92): ``MARKET_ENV``
(lines 153-160), :func:`legacy_topology_name` (242-261), ``_topology_top`` (263-270),
``_market_request`` (272-298), ``_refuse_market_conflict`` (386-399), the topology and
market branches of ``_dispatch_entry`` (326-366, now :func:`apply_legacy_env`) with
their helpers ``_over`` / ``_with_type`` (369-383), and the compatibility shims
:func:`known_planner_topologies` (614-626), :func:`apply_planner_topology_override`
(628-645) and :func:`apply_market_override` (647-672). Every message text is
unchanged. ``scenario_overrides`` re-exports the public names until the shims are
removed.

Import contract: stdlib plus ``compat.legacy`` and ``policies.framework`` (imported
inside the functions, as in the original); ``scenario_overrides`` (whose
``run_dispatch_overrides`` the two shims call) and ``datagen.preprocess`` are imported
locally, so this module never cycles with them.
"""

import re

__all__ = [
    "MARKET_ENV",
    "LEGACY_DISPATCH_ENV",
    "LegacyEnvRequest",
    "read_legacy_dispatch_env",
    "legacy_env_request",
    "apply_legacy_env",
    "legacy_topology_name",
    "known_planner_topologies",
    "apply_planner_topology_override",
    "apply_market_override",
]


#: The ``PooledMarket`` params each legacy market variable sets, in the message order.
MARKET_ENV = (
    ("offer", "ORSIM_OFFER_POLICY"),
    ("claim", "ORSIM_CLAIM_POLICY"),
    ("arbitration", "ORSIM_ARBITRATION_RULE"),
    ("max_rounds", "ORSIM_MARKET_MAX_ROUNDS"),
)

#: ``{request key: environment variable}`` of every legacy dispatch variable, in the
#: order the sim main read them (35b5b92 ``run_container_logistics_simulation.py:98-102``).
LEGACY_DISPATCH_ENV = (("topology", "ORSIM_PLANNER_TOPOLOGY"), *MARKET_ENV)


def read_legacy_dispatch_env(environ):
    """``{topology, offer, claim, arbitration, max_rounds}`` from ``environ``; blank is ``None``.

    Exactly the sim main's former read (``os.environ.get(name, "").strip() or None``
    per variable). The values are unchecked strings; :func:`legacy_env_request` checks
    them.
    """
    return {key: environ.get(name, "").strip() or None for key, name in LEGACY_DISPATCH_ENV}


class LegacyEnvRequest:
    """A checked legacy env request: the decomposition it selects and the market params it sets.

    ``topology`` is the raw ``ORSIM_PLANNER_TOPOLOGY`` value (for messages),
    ``want_top`` its decomposition (``Partitioned`` / ``PooledMarket``) or ``None``,
    ``market`` the ``{param: value}`` the market variables set. Built only by
    :func:`legacy_env_request`.
    """

    __slots__ = ("topology", "want_top", "market")

    def __init__(self, topology, want_top, market):
        self.topology = topology
        self.want_top = want_top
        self.market = market

    def __bool__(self):
        return self.want_top is not None or bool(self.market)

    def segments(self):
        """The run-name segments this request appends (the canonical topology, as before), in order."""
        canonical = legacy_topology_name(self.topology)
        return [canonical] if canonical is not None else []


def legacy_env_request(legacy_env):
    """Check a :func:`read_legacy_dispatch_env` mapping (or ``None``) into a :class:`LegacyEnvRequest`.

    The topology is checked first, then the market variables, in the order
    ``run_dispatch_overrides`` always checked them. Raises ``PolicyError`` for an
    unknown topology or a ``max_rounds`` that is not an integer in 1..100.
    """
    values = dict(legacy_env or {})
    topology = values.get("topology")
    want_top = _topology_top(topology)
    market = _market_request(offer=values.get("offer"), claim=values.get("claim"),
                             arbitration=values.get("arbitration"), max_rounds=values.get("max_rounds"))
    return LegacyEnvRequest(topology, want_top, market)


def legacy_topology_name(topology):
    """The canonical spelling (``partitioned``/``pooled``) of a topology request, ``None`` if unset.

    Raises ``PolicyError`` for an unknown value (the env converter is loud, round-2 §3.6).
    """
    from apps.container_logistics.policies.compat import legacy
    from apps.container_logistics.policies.framework import PolicyError

    if topology is None or (isinstance(topology, str) and not topology.strip()):
        return None
    name = topology.strip() if isinstance(topology, str) else topology
    canonical = legacy.LEGACY_TOPOLOGIES.get(name) if isinstance(name, str) else None
    if canonical is None:
        raise PolicyError(
            f"ORSIM_PLANNER_TOPOLOGY={topology!r} is not a legacy topology "
            f"({', '.join(legacy.LEGACY_TOPOLOGIES)}). The decomposition is now the dispatch "
            f"policy: --policy planner={legacy.PARTITIONED} or --policy planner={legacy.POOLED_MARKET}."
        )
    return canonical


def _topology_top(topology):
    from apps.container_logistics.policies.compat import legacy

    canonical = legacy_topology_name(topology)
    if canonical is None:
        return None
    return legacy.POOLED_MARKET if canonical == "pooled" else legacy.PARTITIONED


def _market_request(**values):
    """``{param: value}`` of the market variables that were given; ``max_rounds`` checked as an int in 1..100."""
    from apps.container_logistics.policies.compat import legacy
    from apps.container_logistics.policies.framework import PolicyError

    out = {}
    for key, env in MARKET_ENV:
        value = values.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if key != "max_rounds":
            if not isinstance(value, str):
                raise PolicyError(f"{env}={value!r} must name a {key} component.")
            out[key] = value.strip()
            continue
        rounds = value
        if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+\s*", value):
            rounds = int(value)
        low, high = legacy.LEGACY_MAX_ROUNDS_RANGE
        if isinstance(rounds, bool) or not isinstance(rounds, int) or not low <= rounds <= high:
            raise PolicyError(
                f"{env}={value!r} must be an integer in {low}..{high}. It is the pooled "
                "auction's safety backstop; the auction ends on convergence."
            )
        out[key] = rounds
    return out


def apply_legacy_env(entry, explicit, current, planner, request):
    """Fold ``request`` into the dispatch override ``entry`` of one profile (in place).

    The topology and market branches of ``scenario_overrides._dispatch_entry``
    (35b5b92 lines 333-363), unchanged: ``entry`` is the deep copy of the explicit
    ``planner`` override, ``explicit`` that override, ``current`` the profile's resolved
    ``{slot: ResolvedPolicy}``, ``planner`` the profile's legacy ``planner`` block (a
    new ``PooledMarket`` takes its market from it, strictly). Message 10 when the
    override names another top; message 9 when a market variable meets a non-market
    top. Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.compat import legacy
    from apps.container_logistics.policies.framework import PolicyError, merge_policy_overrides

    topology, want_top, market = request.topology, request.want_top, request.market
    named_top = entry.get("type").strip() if isinstance(entry.get("type"), str) else None
    top = named_top or current["planner"].name
    if want_top is not None:
        if named_top is not None and named_top != want_top:
            raise PolicyError(
                f"ORSIM_PLANNER_TOPOLOGY={topology!r} selects {want_top} but ORSIM_POLICY_OVERRIDES "
                f"sets planner.type={entry['type']!r}; pass one of them."
            )
        if top != want_top:
            entry["type"] = top = want_top
            if want_top == legacy.POOLED_MARKET:
                params = entry.setdefault("params", {})
                for key, value in legacy.legacy_market_params(planner).items():
                    params[key] = _over(value, params.get(key))
    if market:
        if top != legacy.POOLED_MARKET:
            key, env = next((k, e) for k, e in MARKET_ENV if k in market)
            label = (merge_policy_overrides(current, {"planner": entry})["planner"].label
                     if entry else current["planner"].label)
            raise PolicyError(
                f"{env}={market[key]!r} configures the PooledMarket dispatch, but this run's dispatch "
                f"is {label}. Add ORSIM_PLANNER_TOPOLOGY=pooled, or select --policy "
                f"planner=PooledMarket --policy-param planner.{key}={market[key]}."
            )
        params = entry.setdefault("params", {})
        stated = explicit.get("params") if isinstance(explicit.get("params"), dict) else {}
        for key, value in market.items():
            _refuse_market_conflict(key, value, stated.get(key))
            if key == "max_rounds":
                params[key] = value
            else:
                params[key] = _with_type(params.get(key), value)


def _over(base, stated):
    """A legacy market value under what ``overrides`` stated for the same key (the statement wins)."""
    if stated is None:
        return base
    if isinstance(base, dict) and isinstance(stated, dict) and "type" not in stated:
        return {"type": base["type"], "params": {**base.get("params", {}), **(stated.get("params") or {})}}
    return stated


def _with_type(value, name):
    """A component ref set to ``name``: the same type keeps its params, another type starts from none."""
    if isinstance(value, str):
        value = {"type": value}
    if isinstance(value, dict) and value.get("type") in (None, name):
        return {**value, "type": name}
    return {"type": name}


def _refuse_market_conflict(key, value, stated):
    from apps.container_logistics.policies.framework import PolicyError

    env = dict(MARKET_ENV)[key]
    have = stated.get("type") if isinstance(stated, dict) else stated
    if have is None or (key != "max_rounds" and isinstance(have, str) and have.strip() == value) \
            or (key == "max_rounds" and not isinstance(have, bool) and have == value):
        return
    path = f"planner.{key}" if key == "max_rounds" else f"planner.{key}.type"
    raise PolicyError(
        f"{env}={value!r} conflicts with {path}={have!r} from ORSIM_POLICY_OVERRIDES "
        "(--policy-param/--policy-overrides); pass one of them."
    )


# --- compatibility shims (no run-path caller; kept for tests and external callers) ---


def known_planner_topologies():
    """Planner topologies accepted by ``apply_planner_topology_override`` — sourced
    from the same constant ``Preprocessor._normalize_planner`` validates against, so
    CLI/override validation and the compiler can never drift (plan §6.11/§7).

    Compatibility shim (verification review MINOR-11): no run path calls it (the
    topology request is validated by :func:`legacy_topology_name`). Kept for tests
    and external callers.
    """
    from apps.container_logistics.datagen.preprocess import PLANNER_TOPOLOGIES

    return tuple(PLANNER_TOPOLOGIES)


def apply_planner_topology_override(assignment_collection, topology):
    """Per-run planner-topology override: ``ORSIM_PLANNER_TOPOLOGY``.

    A LOUD wrapper of ``scenario_overrides.run_dispatch_overrides`` (``legacy_env=
    {"topology": topology}``): the dispatch top becomes ``PooledMarket`` (``pooled``;
    ``two-stage`` is its deprecated alias) or ``Partitioned``, keeping the current
    per-scope solver; the legacy ``planner.topology`` / ``planner.market`` keys are
    rewritten as mirrors. Mutates the loaded behaviors only; the on-disk bundle is never
    touched. Returns the canonical topology, or ``None`` for a falsy value (nothing
    changed). Raises ``PolicyError`` for an unknown topology (it used to be ignored with
    a warning).

    Compatibility shim (verification review MINOR-11): no run path calls it; the sim
    main and the control-plane dry run go through ``run_dispatch_overrides``.
    Kept for tests and external callers.
    """
    from apps.container_logistics.scenario.scenario_overrides import run_dispatch_overrides

    run_dispatch_overrides(assignment_collection, legacy_env={"topology": topology})
    return legacy_topology_name(topology)


def apply_market_override(
    assignment_collection, *, offer=None, claim=None, arbitration=None, max_rounds=None
):
    """Per-run pooled-market overrides: ``ORSIM_OFFER_POLICY`` / ``_CLAIM_`` / ``_ARBITRATION_`` / ``MAX_ROUNDS``.

    A LOUD wrapper of ``scenario_overrides.run_dispatch_overrides``: each given
    component name becomes that ``PooledMarket`` param (a changed type resets its
    params) and ``max_rounds`` (an int, or an integer string, in 1..100) the round
    backstop; the ``planner.market`` mirror is rewritten in the legacy shape. Returns
    the applied patch ``{role: name, max_rounds: int}``, or ``None`` when nothing was
    requested. Raises ``UnknownPolicyError`` for an unknown component, ``PolicyError``
    for a ``max_rounds`` outside 1..100 or when the dispatch is not ``PooledMarket``
    (message 9). All used to be ignored or deferred to the runtime's fail-soft
    registries.

    Compatibility shim (verification review MINOR-11): no run path calls it; the sim
    main and the control-plane dry run go through ``run_dispatch_overrides``.
    Kept for tests and external callers.
    """
    from apps.container_logistics.scenario.scenario_overrides import run_dispatch_overrides

    patch = _market_request(offer=offer, claim=claim, arbitration=arbitration, max_rounds=max_rounds)
    if not patch:
        return None
    run_dispatch_overrides(assignment_collection, legacy_env=patch)
    return patch
