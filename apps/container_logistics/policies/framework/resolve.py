"""Spec / profile / override ``policies`` blocks -> validated :class:`ResolvedPolicy`.

Single responsibility: turn the user-facing ``policies`` block (``{slot: {type,
timing?, params?}}``) into registry-validated values, merge per-run overrides into
them, parse the CLI spelling of overrides, and build the run stamp
(``run_config.meta.policies``). Plan ``docs/plans/policy_slots_p0_p3.md`` §4 P2.2.

Every failure is a :class:`PolicyError` subclass whose message names the offending
key and value, the allowed set and the fix. Nothing falls back silently: the one
tolerated lossy step is dropping undeclared params of a slot translated from the
LEGACY keys (``solver``/``solverParams``/``planner.deployment``), which were never
validated; it is logged at WARNING.

Leaf module (plan §2.7): imports only sibling framework modules, never
``apps.container_logistics.assignment.*``. Missing dependencies are reported via
``PolicySpec.missing_requirements`` (``find_spec``), so nothing heavy is imported.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Collection, Mapping, Sequence

from .errors import (
    PolicyDependencyError,
    PolicyError,
    PolicyTimingError,
    SlotNotImplementedError,
    UnknownSlotError,
)
from .params import declared_params
from .registry import default_policy_name, get_policy_spec
from .slots import SLOTS

logger = logging.getLogger(__name__)

__all__ = [
    "ENTRY_KEYS",
    "ResolvedPolicy",
    "resolve_policies_block",
    "authored_after_resolution",
    "merge_policy_overrides",
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

    ``params`` is the full validated dict (declared defaults filled in), in field order.
    """

    slot: str
    name: str
    version: str
    kind: str
    timing: str | None
    params: dict[str, Any]

    def to_profile(self) -> dict[str, Any]:
        """The compiled-profile form ``{"type","version","kind","timing","params"}``.

        This is what ``profile.policies.<slot>`` carries in a bundle, and what
        ``dispatch/profile.py::resolve_dispatch_for_profile`` reads back.
        """
        return {
            "type": self.name,
            "version": self.version,
            "kind": self.kind,
            "timing": self.timing,
            "params": dict(self.params),
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
    if spec.status == "legacy":
        raise SlotNotImplementedError(
            f"'{where}.{slot}' is not selectable here yet; configure it via "
            f"{spec.configured_via} (lands in P5)."
        )


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
        raise PolicyError(f"{label}.params must be an object (got {params!r}).")


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


def _legacy_params(spec, params: Mapping[str, Any], label: str) -> dict[str, Any]:
    """Drop the params ``spec`` does not declare (legacy keys were never validated)."""
    declared = {info.name for info in declared_params(spec.params_cls)}
    dropped = sorted(k for k in params if k not in declared)
    if dropped:
        logger.warning(
            "%s: legacy params %s are not declared by %s %s and are not passed to it "
            "(they still feed the framework's accounting via solver_params).",
            label, dropped, spec.slot, spec.name,
        )
    return {k: v for k, v in params.items() if k in declared}


def _resolve_entry(slot: str, entry: Mapping[str, Any], *, where: str, legacy: bool) -> ResolvedPolicy:
    label = f"{where}.{slot}"
    _check_entry_shape(entry, label, require_type=True)
    spec = get_policy_spec(slot, entry["type"].strip())
    missing = spec.missing_requirements()
    if missing:
        raise PolicyDependencyError(
            f"{slot.capitalize()} policy {spec.name!r} requires module(s) not importable in "
            f"this environment: {', '.join(missing)}. Install them or pick another policy."
        )
    timing = _resolve_timing(slot, entry.get("timing"), label)
    raw_params = dict(entry.get("params") or {})
    if legacy:
        raw_params = _legacy_params(spec, raw_params, label)
    params = spec.params_cls.from_mapping(raw_params, where=f"{label}.params for {spec.name}")
    return ResolvedPolicy(slot=slot, name=spec.name, version=spec.version, kind=spec.kind(),
                          timing=timing, params=params.to_dict())


def resolve_policies_block(
    block: Mapping[str, Any] | None,
    *,
    where: str = "policies",
    legacy_slots: Collection[str] = (),
) -> dict[str, ResolvedPolicy]:
    """Validate ``block`` against the registry; return one resolved policy per slot.

    Every implemented slot is present in the result, in ``SLOTS`` order; a slot the
    block does not name gets its declared default (``{}`` means "all defaults").
    ``legacy_slots`` names slots whose entry was translated from the legacy keys:
    their undeclared params are dropped with a WARNING instead of rejected.
    Raises ``PolicyError`` subclasses (unknown/planned/legacy slot, unknown entry key,
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
    policy does not declare removed (the WARNING was logged by
    :func:`resolve_policies_block`) and the type set to the registered spelling.
    Authored slots are returned exactly as written: defaults are not filled in.
    """
    out = json.loads(json.dumps(dict(block or {})))
    for slot in legacy_slots:
        entry = out.get(slot)
        if not isinstance(entry, dict) or slot not in resolved:
            continue
        declared = set(resolved[slot].params)
        entry["type"] = resolved[slot].name
        if isinstance(entry.get("params"), dict):
            entry["params"] = {k: v for k, v in entry["params"].items() if k in declared}
    return out


def merge_policy_overrides(
    current: Mapping[str, ResolvedPolicy],
    overrides: Mapping[str, Any],
) -> dict[str, ResolvedPolicy]:
    """Apply per-run ``overrides`` (``{slot: {type?, timing?, params?}}``) to ``current``.

    Per slot: a CHANGED type resets params to the new policy's defaults plus the
    override params; the same (or an omitted) type merges the override params over the
    current ones. An omitted timing keeps the current timing. The result is
    re-validated in full; slots not named by ``overrides`` are carried unchanged.
    Raises ``PolicyError`` subclasses.
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
        same_type = cur is not None and get_policy_spec(slot, name).name == cur.name
        params = {**(cur.params if same_type else {}), **dict(entry.get("params") or {})}
        timing = entry.get("timing") if entry.get("timing") is not None else (
            cur.timing if cur is not None else None
        )
        merged_entry: dict[str, Any] = {"type": name, "params": params}
        if timing is not None:
            merged_entry["timing"] = timing
        out[slot] = _resolve_entry(slot, merged_entry, where="policy overrides", legacy=False)
    return out


def _cli_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def parse_policy_cli_args(policies: Sequence[str], params: Sequence[str]) -> dict[str, Any]:
    """Parse ``--policy slot=name`` and ``--policy-param slot.key=value`` into overrides.

    Returns ``{slot: {"type"?: name, "params"?: {key: value}}}`` for
    :func:`merge_policy_overrides`. A value is ``json.loads(value)`` when that parses
    (``5`` -> int, ``true`` -> bool, ``"5"`` -> str), else the raw string. Slot names
    may contain dots (``market.offer``), so a param spec splits on the LAST ``.``
    before the ``=``. Shape only: names and values are validated by the merge.
    Raises ``PolicyError`` for a malformed or contradictory argument.
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
        slot, dot, key = key_part.strip().rpartition(".")
        if not sep or not dot or not slot or not key:
            raise PolicyError(
                f"--policy-param {arg!r} must be SLOT.KEY=VALUE, e.g. dispatch.dual_cycle_bonus_km=0."
            )
        bucket = out.setdefault(slot, {}).setdefault("params", {})
        if key in bucket:
            raise PolicyError(f"--policy-param sets {slot}.{key} twice; pass it once.")
        bucket[key] = _cli_value(value)
    return out


def policies_stamp(
    resolved: Mapping[str, ResolvedPolicy],
    *,
    candidate_settings: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    """The run-provenance stamp ``run_config.meta.policies``.

    ``{slot: {name, version, kind, timing, params, source}}``; the dispatch entry also
    carries ``candidate_settings`` (the spatial knobs, plan §5). ``source`` is
    ``bundle``, ``legacy-profile`` or ``override``.
    """
    stamp: dict[str, Any] = {}
    for slot, r in resolved.items():
        entry = {"name": r.name, "version": r.version, "kind": r.kind, "timing": r.timing,
                 "params": dict(r.params), "source": source}
        if slot == "dispatch":
            entry["candidate_settings"] = dict(candidate_settings)
        stamp[slot] = entry
    return stamp
