"""Spec / profile / override ``policies`` blocks -> validated :class:`ResolvedPolicy`.

Single responsibility: turn the user-facing ``policies`` block (``{slot: {type,
timing?, params?}}``) into registry-validated values, merge per-run overrides into
them, parse the CLI spelling of overrides, and build the run stamp
(``run_config.meta.policies``). Plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2.2.

Round 2 (``docs/plans/policy_slots_round2_whole_tick.md`` §3.2, §3.5): a selection is
a TREE. A ``PolicyRef`` param (e.g. ``Partitioned.inner``, ``PooledMarket.offer``)
holds ``{type, params?}`` and is resolved recursively by ``PolicyParams.from_mapping``;
:func:`merge_policy_overrides` merges nested params ref by ref (a changed ref type
resets that ref's params); a :class:`ResolvedPolicy` carries its ``label``
(``"PooledMarket/GreedyNearest"``: the type plus its ``inner`` chain) and ``leaf``.

Every failure is a :class:`PolicyError` subclass whose message names the offending
key and value, the allowed set and the fix. Nothing falls back silently: the one
tolerated lossy step is dropping undeclared params of a slot translated from the
LEGACY keys (``solver``/``solverParams``/``planner.deployment``), which were never
validated; it is logged at WARNING.

Leaf module (plan §2.7): imports only sibling framework modules, never
``policies.dispatch.*`` or ``apps.container_logistics.assignment.*``. Missing
dependencies are reported via ``PolicySpec.missing_requirements`` (``find_spec``), so
nothing heavy is imported.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Collection, Mapping, Sequence

from .components import ref_target
from .errors import (
    PolicyError,
    PolicyParamsError,
    PolicyTimingError,
    SlotNotImplementedError,
    UnknownSlotError,
)
from .params import PolicyParams, declared_params
from .registry import INNER_PARAM, PolicySpec, default_policy_name, get_policy_spec
from .slots import SLOTS

logger = logging.getLogger(__name__)

__all__ = [
    "ENTRY_KEYS",
    "INNER_PARAM",
    "selection_chain",
    "drop_undeclared_legacy_params",
    "ResolvedPolicy",
    "resolve_policies_block",
    "authored_after_resolution",
    "merge_policy_overrides",
    "merge_params",
    "parse_policy_cli_args",
    "policies_stamp",
]

#: The keys a ``policies.<slot>`` entry may carry (authored, override and CLI forms).
ENTRY_KEYS = ("type", "timing", "params")

# Why a declared-but-not-executable timing cannot run yet, per (slot, timing).
_TIMING_NOT_EXECUTABLE_REASON = {
    ("dispatch", "plan"): "the held-plan dispatcher lands with P4",
}


@dataclass(frozen=True)
class ResolvedPolicy:
    """One slot's validated selection: registered name and version, timing, params.

    ``params`` is the full validated dict (declared defaults filled in), in field order;
    a ref param is nested as ``{type, params}``. ``typed`` is the same value as the
    policy's ``Params`` instance (refs carry their versions). ``label`` is the name plus
    the ``inner`` chain (:func:`selection_chain`), ``leaf`` its last name;
    ``information_model``/``share_tag_format`` are the registration's. All four
    default to the plain-name / unset forms for a hand-built value.
    """

    slot: str
    name: str
    version: str
    kind: str
    timing: str | None
    params: dict[str, Any]
    label: str = ""
    leaf: str = ""
    information_model: str | None = None
    share_tag_format: str | None = None
    typed: PolicyParams | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", self.name)
        if not self.leaf:
            object.__setattr__(self, "leaf", self.label.rpartition("/")[2])

    def profile_params(self) -> dict[str, Any]:
        """``params`` with every ref also carrying its ``version`` (compiled/stamp form)."""
        return self.typed.to_profile_dict() if self.typed is not None else dict(self.params)

    def to_profile(self) -> dict[str, Any]:
        """The compiled-profile form ``{"type","version","kind","timing","params"}``.

        This is what ``profile.policies.<slot>`` carries in a bundle, and what
        ``dispatch/profile.py::resolve_dispatch_for_profile`` reads back. Nested refs
        are ``{type, version, params}``; the ``version`` is an echo, ignored on read.
        """
        return {
            "type": self.name,
            "version": self.version,
            "kind": self.kind,
            "timing": self.timing,
            "params": self.profile_params(),
        }


def _check_selectable_slot(slot: Any, where: str) -> None:
    """Raise unless ``slot`` is an implemented slot a ``policies`` block may select."""
    spec = SLOTS.get(slot) if isinstance(slot, str) else None
    if spec is None:
        raise UnknownSlotError(
            f"Unknown policy slot {slot!r} in {where!r}. Known slots: {', '.join(SLOTS)}."
        )
    if spec.status == "planned":
        raise SlotNotImplementedError(
            f"Policy slot {slot!r} is declared but not implemented yet (lands in "
            f"{spec.lands_in}). Remove '{where}.{slot}'."
        )


def selection_chain(slot: str, name: str, typed: PolicyParams | None) -> list[str]:
    """``[name, inner name, inner's inner name, ...]`` following ``PolicySpec.inner_param``.

    Only the declared descent param is followed (the composite contract,
    ``dispatch/composite.py``); ``typed`` is the resolved ``Params`` of ``name``
    (``None``: a leaf with nothing to follow).
    """
    chain = [name]
    spec = get_policy_spec(slot, name)
    while typed is not None and spec.inner_param is not None:
        inner = getattr(typed, spec.inner_param)
        chain.append(inner.type)
        spec, typed = get_policy_spec("dispatch", inner.type), inner.params
    return chain


def _check_entry_shape(entry: Any, label: str, *, require_type: bool) -> None:
    if not isinstance(entry, Mapping):
        raise PolicyError(
            f"{label} must be an object {{type, timing?, params?}} (got {entry!r})."
        )
    unknown = sorted(str(k) for k in entry if k not in ENTRY_KEYS)
    if unknown:
        raise PolicyError(
            f"{label}: unknown key(s) {', '.join(repr(k) for k in unknown)}. "
            f"Allowed keys: {', '.join(ENTRY_KEYS)}."
        )
    name = entry.get("type")
    if (require_type or name is not None) and (not isinstance(name, str) or not name.strip()):
        raise PolicyError(f"{label}.type must name a registered policy (got {name!r}).")
    params = entry.get("params")
    if params is not None and not isinstance(params, Mapping):
        raise PolicyParamsError(f"{label}.params must be an object (got {params!r}).")


def _resolve_timing(slot: str, timing: Any, label: str) -> str | None:
    spec = SLOTS[slot]
    if not spec.timings:
        if timing is not None:
            raise PolicyTimingError(
                f"{label}.timing={timing!r}: slot {slot!r} has no timings. Remove it."
            )
        return None
    if timing is None:
        return spec.executable_timings[0] if spec.executable_timings else spec.timings[0]
    if timing not in spec.timings:
        raise PolicyTimingError(
            f"{label}.timing={timing!r} is not a {slot} timing. Allowed: {', '.join(spec.timings)}."
        )
    if timing not in spec.executable_timings:
        reason = _TIMING_NOT_EXECUTABLE_REASON.get((slot, timing), "it lands in a later phase")
        allowed = ", ".join(repr(t) for t in spec.executable_timings)
        raise PolicyTimingError(
            f"{label}.timing={timing!r} is defined but not executable yet: {reason}. Use {allowed}."
        )
    return timing


def drop_undeclared_legacy_params(spec, params: Mapping[str, Any], label: str) -> dict[str, Any]:
    """The params of ``params`` that ``spec`` declares; the rest are dropped at WARNING.

    The ONE implementation of the legacy drop rule (review F4): used by the legacy
    profile translation (``dispatch/profile.py``) and by legacy-provenance slots in
    :func:`resolve_policies_block`. Legacy keys were never validated, and dropped
    keys still feed the framework's accounting via ``solver_params``. Applied to a
    TREE, a ref value ``{type, params}`` is filtered against its own target,
    recursively (``spec`` may be a ``PolicySpec`` or a component ``RefTarget``).
    """
    infos = {info.name: info for info in declared_params(spec.params_cls)}
    dropped = sorted(k for k in params if k not in infos)
    if dropped:
        logger.warning(
            "%s: legacy params %s are not declared by %s %s and are not passed to it "
            "(they still feed the framework's accounting via solver_params).",
            label, dropped, getattr(spec, "slot", None) or getattr(spec, "kind", ""), spec.name,
        )
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key not in infos:
            continue
        info = infos[key]
        if info.is_ref and isinstance(value, Mapping) and isinstance(value.get("type"), str) \
                and isinstance(value.get("params"), Mapping):
            target = ref_target(info.meta.kind, value["type"].strip(), where=f"{label}.params.{key}")
            value = {**value, "params": drop_undeclared_legacy_params(
                target, value["params"], f"{label}.params.{key}")}
        out[key] = value
    return out


def _resolve_entry(slot: str, entry: Mapping[str, Any], *, where: str, legacy: bool) -> ResolvedPolicy:
    label = f"{where}.{slot}"
    _check_entry_shape(entry, label, require_type=True)
    spec = get_policy_spec(slot, entry["type"].strip())
    spec.require_available()
    timing = _resolve_timing(slot, entry.get("timing"), label)
    raw_params = dict(entry.get("params") or {})
    if legacy:
        raw_params = drop_undeclared_legacy_params(spec, raw_params, label)
    where_params = f"{label}.params for {spec.name}"
    try:
        params = spec.params_cls.from_mapping(raw_params, where=where_params)
    except PolicyParamsError as exc:
        hint = _inner_key_hint(slot, spec, raw_params)
        if not hint:
            raise
        raise PolicyParamsError(f"{exc} {hint}") from exc
    return ResolvedPolicy(slot=slot, name=spec.name, version=spec.version, kind=spec.kind(),
                          timing=timing, params=params.to_dict(),
                          label="/".join(selection_chain(slot, spec.name, params)),
                          information_model=spec.information_model,
                          share_tag_format=spec.share_tag_format if slot == "dispatch" else None,
                          typed=params)


def _inner_key_hint(slot: str, spec: PolicySpec, raw: Mapping[str, Any]) -> str:
    """Message 5's hint: an unknown key that the ``inner`` solver declares.

    ``from_mapping`` checks unknown keys before any value, so when ``raw`` has an
    undeclared key the error being decorated IS the unknown-key error. Returns ``""``
    when no unknown key is an inner param (or the inner cannot be named).
    """
    inner_param = spec.inner_param
    declared = {info.name for info in declared_params(spec.params_cls)}
    unknown = sorted(k for k in raw if isinstance(k, str) and k not in declared)
    if inner_param is None or not unknown:
        return ""
    value = raw.get(inner_param)
    inner_name = value.get("type") if isinstance(value, Mapping) else None
    if not isinstance(inner_name, str):
        inner_name = getattr(spec.params_cls, "__dataclass_fields__")[inner_param].default.type
    try:
        inner_spec = get_policy_spec("dispatch", inner_name.strip())
    except PolicyError:
        return ""
    inner_declared = {info.name for info in declared_params(inner_spec.params_cls)}
    return " ".join(
        f"{k!r} is a param of the inner {inner_spec.name}: use {slot}.{inner_param}.{k}."
        for k in unknown if k in inner_declared
    )


def resolve_policies_block(
    block: Mapping[str, Any] | None,
    *,
    where: str = "policies",
    legacy_slots: Collection[str] = (),
) -> dict[str, ResolvedPolicy]:
    """Validate ``block`` against the registry; return one resolved policy per slot.

    Every implemented slot is present in the result, in ``SLOTS`` order; a slot the
    block does not name gets its default (``{}`` means "all defaults"; see
    :func:`default_policy_name`).
    ``legacy_slots`` names slots whose entry was translated from the legacy keys:
    their undeclared params are dropped with a WARNING instead of rejected.
    Raises ``PolicyError`` subclasses (unknown/planned slot, unknown entry key,
    unknown policy, timing, missing dependency, params).
    """
    if block is None:
        block = {}
    if not isinstance(block, Mapping):
        raise PolicyError(f"{where!r} must be an object of slot -> {{type, timing?, params?}} (got {block!r}).")
    for slot in block:
        _check_selectable_slot(slot, where)
    out: dict[str, ResolvedPolicy] = {}
    for slot, spec in SLOTS.items():
        if spec.status != "implemented":
            continue
        entry = block.get(slot)
        if entry is None:
            entry = {"type": default_policy_name(slot)}
        out[slot] = _resolve_entry(slot, entry, where=where, legacy=slot in legacy_slots)
    return out


def authored_after_resolution(
    block: Mapping[str, Any] | None,
    resolved: Mapping[str, ResolvedPolicy],
    legacy_slots: Collection[str] = (),
) -> dict[str, Any]:
    """The block to echo back as authored (recipe / ``spec.json``), NOT the resolved form.

    A copy of ``block`` with, for each slot in ``legacy_slots``, the params the resolved
    policies do not declare removed at every level of the selection tree (the WARNING
    was logged by :func:`resolve_policies_block`) and each type set to the registered
    spelling. Authored slots are returned exactly as written: defaults are not filled in.
    """
    out = json.loads(json.dumps(dict(block or {})))
    for slot in legacy_slots:
        entry = out.get(slot)
        if not isinstance(entry, dict) or slot not in resolved:
            continue
        entry["type"] = resolved[slot].name
        if isinstance(entry.get("params"), dict):
            entry["params"] = _keep_declared(entry["params"], resolved[slot].params)
    return out


def _keep_declared(authored: Mapping[str, Any], resolved: Mapping[str, Any]) -> dict[str, Any]:
    """``authored`` params minus the keys ``resolved`` lacks, recursing into ref values."""
    out: dict[str, Any] = {}
    for key, value in authored.items():
        if key not in resolved:
            continue
        target = resolved[key]
        if isinstance(value, Mapping) and isinstance(target, Mapping) and "type" in target:
            ref = dict(value)
            ref["type"] = target["type"]
            if isinstance(value.get("params"), Mapping):
                ref["params"] = _keep_declared(value["params"], target.get("params") or {})
            value = ref
        out[key] = value
    return out


def merge_policy_overrides(
    current: Mapping[str, ResolvedPolicy],
    overrides: Mapping[str, Any],
) -> dict[str, ResolvedPolicy]:
    """Apply per-run ``overrides`` (``{slot: {type?, timing?, params?}}``) to ``current``.

    Per slot: a CHANGED type resets params to the new policy's defaults plus the
    override params, except that a decomposition keeps the current per-scope solver
    as its ``inner`` unless the override names one (:func:`_carried_inner`); the same
    (or an omitted) type merges the override params over the
    current ones, NESTED (:func:`merge_params`): a ref param merges into the current
    ref's params unless the override changes the ref's type, which resets that ref's
    params. An omitted timing keeps the current timing. The result is re-validated in
    full; slots not named by ``overrides`` are carried unchanged. Raises
    ``PolicyError`` subclasses.
    """
    if not isinstance(overrides, Mapping):
        raise PolicyError(f"policy overrides must be an object of slot -> entry (got {overrides!r}).")
    out = dict(current)
    for slot, entry in overrides.items():
        _check_selectable_slot(slot, "policy overrides")
        label = f"policy overrides.{slot}"
        _check_entry_shape(entry, label, require_type=False)
        cur = current.get(slot)
        requested = entry.get("type")
        name = requested.strip() if isinstance(requested, str) else (
            cur.name if cur is not None else default_policy_name(slot)
        )
        spec = get_policy_spec(slot, name)
        same_type = cur is not None and spec.name == cur.name
        base = cur.params if same_type else _carried_inner(spec, cur, entry)
        params = merge_params(spec, base, dict(entry.get("params") or {}), path=f"{label}.params")
        timing = entry.get("timing") if entry.get("timing") is not None else (
            cur.timing if cur is not None else None
        )
        merged_entry: dict[str, Any] = {"type": name, "params": params}
        if timing is not None:
            merged_entry["timing"] = timing
        out[slot] = _resolve_entry(slot, merged_entry, where="policy overrides", legacy=False)
    return out


def _carried_inner(spec: PolicySpec, current: ResolvedPolicy | None, entry: Mapping[str, Any]) -> dict[str, Any]:
    """The params a CHANGED top type starts from: ``{}``, plus the current per-scope solver.

    When the new type delegates through ``inner_param`` and the override does not set
    it, the current selection's per-scope solver (its inner, or the current top itself
    when that is a leaf) is carried over with its params, so changing the
    decomposition (``--policy dispatch=PooledMarket``) never silently swaps the
    solver: the same rule ``ORSIM_PLANNER_TOPOLOGY`` follows (round-2 §3.6, review C2
    finding 2). Every other param resets to the new type's defaults.
    """
    descend = spec.inner_param
    if current is None or descend is None or descend in (entry.get("params") or {}):
        return {}
    current_spec = get_policy_spec(spec.slot, current.name)
    if current_spec.inner_param is not None:
        return {descend: current.params[current_spec.inner_param]}
    return {descend: {"type": current.name, "params": dict(current.params)}}


def merge_params(
    spec: Any, current: Mapping[str, Any], override: Mapping[str, Any], *, path: str
) -> dict[str, Any]:
    """``override`` merged over ``current`` for the params of ``spec``, ref by ref.

    ``spec`` is a ``PolicySpec`` or a component ``RefTarget`` (anything with
    ``params_cls``). A non-ref key is replaced. A ref key takes a mapping ``{type?,
    params?, version?}`` or a bare string (the CLI's ``dispatch.inner=X``, meaning
    ``{type: X}``): a type that differs from the current ref's resets the ref's params
    to the override's; otherwise the override's params merge into the current ref's,
    recursively. Keys the policy does not declare pass through for the resolver to
    refuse. Raises ``UnknownPolicyError`` for an unknown ref type (with its path).
    """
    infos = {info.name: info for info in declared_params(spec.params_cls)}
    out = dict(current)
    for key, value in override.items():
        info = infos.get(key)
        out[key] = (_merge_ref(info, current.get(key), value, path=f"{path}.{key}")
                    if info is not None and info.is_ref else value)
    return out


def _merge_ref(info: Any, current: Any, value: Any, *, path: str) -> Any:
    if isinstance(value, str):
        value = {"type": value}
    if not isinstance(value, Mapping):
        return value  # malformed: the resolver refuses it with message 4
    cur = current if isinstance(current, Mapping) else {}
    cur_type = cur.get("type") or getattr(info.default, "type", None)
    new_type = value.get("type") if value.get("type") is not None else cur_type
    if not isinstance(new_type, str):
        return dict(value)  # malformed type: the resolver names it
    target = ref_target(info.meta.kind, new_type.strip(), where=path, leaf_only=info.meta.leaf_only)
    same = isinstance(cur_type, str) and target.name == cur_type.strip()
    params = merge_params(target, (cur.get("params") or {}) if same else {},
                          dict(value.get("params") or {}), path=f"{path}.params")
    return {"type": target.name, "params": params}


def _cli_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def parse_policy_cli_args(policies: Sequence[str], params: Sequence[str]) -> dict[str, Any]:
    """Parse ``--policy slot=name`` and ``--policy-param slot.path=value`` into overrides.

    Returns ``{slot: {"type"?: name, "params"?: {...}}}`` for
    :func:`merge_policy_overrides`. The slot is the longest dotted prefix that names a
    known slot (slot names may contain dots, ``world.shedding``); every further
    non-final segment is a policy reference (``dispatch.inner.k=v`` ->
    ``{"dispatch": {"params": {"inner": {"params": {"k": v}}}}}``) and a value given
    to a reference itself sets its type (``dispatch.inner=RandomAssignment``). A value
    is ``json.loads(value)`` when that parses (``5`` -> int, ``true`` -> bool, ``"5"``
    -> str), else the raw string. Shape only: names and values are validated by the
    merge. Raises ``PolicyError`` for a malformed or contradictory argument (message 21
    for an unknown slot prefix or an empty path).
    """
    out: dict[str, dict[str, Any]] = {}
    for arg in policies or ():
        slot, sep, name = str(arg).partition("=")
        slot, name = slot.strip(), name.strip()
        if not sep or not slot or not name:
            raise PolicyError(f"--policy {arg!r} must be SLOT=NAME, e.g. dispatch=GreedyNearest.")
        entry = out.setdefault(slot, {})
        if entry.get("type") not in (None, name):
            raise PolicyError(
                f"--policy names slot {slot!r} twice ({entry['type']!r} and {name!r}); pass it once."
            )
        entry["type"] = name
    for arg in params or ():
        key_part, sep, value = str(arg).partition("=")
        slot, path = _split_param_path(key_part.strip(), arg, sep)
        bucket = out.setdefault(slot, {}).setdefault("params", {})
        for ref_name in path[:-1]:  # every non-final segment is a policy reference
            ref_value = bucket.setdefault(ref_name, {})
            if isinstance(ref_value, str):  # an earlier `slot.ref=Name` set the ref's type
                ref_value = bucket[ref_name] = {"type": ref_value}
            bucket = ref_value.setdefault("params", {})
        key, parsed = path[-1], _cli_value(value)
        existing = bucket.get(key)
        if isinstance(existing, dict) and isinstance(parsed, str) and "type" not in existing:
            existing["type"] = parsed  # `slot.ref=Name` after `slot.ref.k=v`: the ref's type
            continue
        if key in bucket:
            raise PolicyError(f"--policy-param sets {slot}.{'.'.join(path)} twice; pass it once.")
        bucket[key] = parsed
    return out


def _split_param_path(key_part: str, arg: str, sep: str) -> tuple[str, list[str]]:
    """``"dispatch.inner.k"`` -> ``("dispatch", ["inner", "k"])``: the slot is the longest known prefix.

    Slot names may contain dots (``world.shedding``). Raises ``PolicyError`` (message
    21) for a missing ``=``/path, or when no known slot is a prefix.
    """
    if not sep or not key_part:
        raise PolicyError(
            f"--policy-param {arg!r} must be SLOT.PATH=VALUE with a non-empty path, e.g. "
            "dispatch.inner.dual_cycle_bonus_km=0."
        )
    segments = key_part.split(".")
    for cut in range(len(segments) - 1, 0, -1):
        slot = ".".join(segments[:cut])
        if slot in SLOTS:
            return slot, segments[cut:]
    if key_part in SLOTS:
        raise PolicyError(
            f"--policy-param {arg!r} must be SLOT.PATH=VALUE with a non-empty path, e.g. "
            "dispatch.inner.dual_cycle_bonus_km=0."
        )
    known = ", ".join(name for name, spec in SLOTS.items() if spec.status == "implemented")
    raise PolicyError(
        f"--policy-param {arg!r}: no known slot is a prefix of {key_part!r}. Known slots: {known}."
    )


def policies_stamp(
    resolved: Mapping[str, ResolvedPolicy],
    *,
    candidate_settings: Mapping[str, Any],
    accounting_params: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    """The run-provenance stamp ``run_config.meta.policies``.

    ``{slot: {name, version, kind, timing, label, leaf, information_model, params,
    source}}``: ``label`` is the selection (``"PooledMarket/GreedyNearest"``), ``leaf``
    its innermost solver, ``params`` nested with each ref as ``{type, version,
    params}``. The dispatch entry also carries ``share_tag_format``,
    ``candidate_settings`` (the spatial knobs, plan §5) and
    ``accounting_params``: the effective ``profile.solver_params`` the framework's
    accounting (``benefit_km``, owner reserve, claims, arbitration) costs with
    (review F1). It equals the shared keys of ``params`` whenever the policy
    declares them; a cost-blind policy (Random) shows the dials it does not read.
    ``source`` is ``bundle``, ``legacy-profile`` or ``override``.
    """
    stamp: dict[str, Any] = {}
    for slot, r in resolved.items():
        entry = {"name": r.name, "version": r.version, "kind": r.kind, "timing": r.timing,
                 "label": r.label, "leaf": r.leaf, "information_model": r.information_model,
                 "params": r.profile_params(), "source": source}
        if slot == "dispatch":
            entry["share_tag_format"] = r.share_tag_format
            entry["candidate_settings"] = dict(candidate_settings)
            entry["accounting_params"] = dict(accounting_params)
        stamp[slot] = entry
    return stamp
