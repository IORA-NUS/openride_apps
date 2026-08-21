"""Per-facility rules — the pure resolver (plan ``docs/facility_rules_plan.md`` §3–§7).

One targeted rule list, ``facilityRules``, replaces the bespoke per-key
``overrides.facility.rebate_by_code`` mechanism. A rule is::

    { "match": { <one matcher key>: <value> }, "set": { <allow-listed key>: <value>, ... } }

and there are exactly **two layers**:

* ``overrides.facility`` — the scenario-wide blanket merge. **Rank 0.**
* ``facilityRules``      — targeted. ``code`` is **rank 10**, ``name`` is **rank 30**.

Ranks are gapped integers so a future matcher (``name_prefix`` is the reserved
candidate, rank 20) slots in without renumbering and without changing what any
existing rank means.

Three properties this module is built around, each of which has a mutation proof
in the plan's §14.2 rather than a comment asserting it:

1. **Resolution is per settable KEY, not per rule** (§4.2). A ``name`` rule that
   sets only ``gate_count`` must not strip the ``rebate`` a ``code`` rule gave the
   same facility. Per-rule resolution is the natural mis-reading of
   "most specific wins" and it fails silently, producing a mis-target from rules
   that are each individually correct.
2. **Presence, not truthiness** (§4.2). ``{"rebate": null}`` at rank 10 beats a
   schedule at rank 0 — ``k in rule["set"]`` is the test, never ``rule["set"].get(k)``.
   This is the precedence lesson carried over verbatim from the deleted
   ``resolve_facility_rebate``; the blanket merge's ``if v is not None`` idiom must
   **not** be reused here.
3. **Zero RNG** (§12.1). Resolution runs after site sampling and draws nothing, so
   compiling ``W`` and ``W⊕R`` perturbs nothing outside the keys the rules set.
   Datagen is seeded per role, so a single stray ``rng.random()`` here would shift
   every downstream sample — which is what makes ``test_rules_do_not_perturb_...``
   sharp.

**Mechanism-inert, value-live.** The *mechanism* perturbs nothing. The *values* are
world physics: ``gate_count`` changes gate capacity, therefore queueing, therefore
every KPI. Runs before and after a gate-count change are **not comparable** (§12.2).

Purity: stdlib only, plus :mod:`apps.container_logistics.rebate` for schedule
parsing (``datagen/preprocess.py`` already depends on it). No import of
``datagen.catalog``, no scenario/runtime/Eve/MQTT edge, no module globals read.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from apps.container_logistics.rebate import RebateSpecError, parse_rebate_schedule


class FacilityRulesError(ValueError):
    """An authored ``facilityRules`` block that cannot be compiled.

    A distinct type rather than ``preprocess.SpecValidationError`` so this module
    keeps its no-back-edge purity; ``preprocess`` catches and re-raises, exactly as
    it already does for :class:`RebateSpecError`.
    """


#: Matcher key -> specificity rank. **Gapped on purpose** (§3.2): rank 20 is
#: reserved for a future ``name_prefix``/glob matcher. The table must stay a
#: TOTAL order — two matchers sharing a rank silently re-introduce the
#: order-dependence §4.3 exists to forbid, so ``test_matcher_ranks_are_unique``
#: pins ``len(set(values)) == len(RANKS)``.
MATCHER_RANKS: dict[str, int] = {
    "name": 30,
    "code": 10,
}

#: The rank of ``overrides.facility`` (and, below it, the datagen default).
BLANKET_RANK = 0


@dataclass(frozen=True)
class _KeySpec:
    """One allow-listed settable key: whether ``null`` is a legal value, and how to
    validate/normalise a non-null one."""

    nullable: bool
    #: ``(value, where) -> normalised value``; raises :class:`FacilityRulesError`.
    coerce: Any


def _reject_bool(value: Any, where: str) -> None:
    """``isinstance(True, int)`` is ``True`` in Python, so ``"gate_count": true``
    would otherwise compile to one gate. House style rejects ``bool`` before every
    numeric check (``preprocess._strict_bool``, ``rebate._is_real_number``)."""
    if isinstance(value, bool):
        raise FacilityRulesError(
            f"{where}: must be a positive integer, got a boolean ({value!r}). "
            f"Booleans are integers in Python, so this would silently compile to "
            f"{int(value)}."
        )


def _coerce_positive_int(value: Any, where: str) -> int:
    _reject_bool(value, where)
    if not isinstance(value, int):
        raise FacilityRulesError(
            f"{where}: must be a positive integer, got {type(value).__name__} ({value!r})."
        )
    if value < 1:
        raise FacilityRulesError(
            f"{where}: must be a positive integer (>= 1), got {value!r}."
        )
    return int(value)


def _coerce_gate_count(value: Any, where: str) -> int:
    # FacilityQueueController asserts >= 1 at statemachine/facility_queue_sm.py; catching
    # it here beats an agent-boot crash 500 agents into a run.
    return _coerce_positive_int(value, where)


def _coerce_service_time(value: Any, where: str) -> int:
    return _coerce_positive_int(value, where)


def _coerce_rebate(value: Any, where: str) -> dict:
    """Delegate schedule validation to ``parse_rebate_schedule`` **verbatim** — the
    rule list changes *where* a schedule attaches, not what a valid schedule is."""
    try:
        return parse_rebate_schedule(value, where=where).as_block()
    except RebateSpecError as exc:
        raise FacilityRulesError(str(exc)) from exc


#: The allow-list. **Exactly three keys, each with a named live consumer** (§6.1).
#: "Any profile key" would let a rule set ``facility_type`` and break the code<->type
#: invariant the trip matrix and order-leg snapping depend on.
SETTABLE_KEYS: dict[str, _KeySpec] = {
    # facility/manager.py -> FacilityQueueController(gate_count=...); one
    # GateStateMachine per gate; published by facility_snapshot_publisher.
    "gate_count": _KeySpec(nullable=False, coerce=_coerce_gate_count),
    # TWO independent causal paths, not one (F5) — both must be held in mind when
    # reasoning about what a service_time rule does:
    #   (a) FACILITY side, governing actual gate occupancy:
    #       facility/service_time.py (behavior, then profile) -> facility/manager.py
    #       and facility/app.py; published by facility_snapshot_publisher.
    #   (b) ORDER side, a pre-gate ESTIMATE flattened onto every matched order as
    #       profile.pickup_service_time / dropoff_service_time, read by
    #       assignment/app.py's publish payload, by haul_trip_duration.
    #       gate_service_seconds() feeding the MIN_HAUL_TRIP_SECONDS floor (four call
    #       sites), and as the initial trip-stats seed in truck/trip_manager.py —
    #       which the facility's authoritative value later overwrites.
    # The floor in (b) partially ABSORBS a service_time reduction, so the two paths
    # do not simply add. Measured in tests/test_order_service_time_path.py.
    "service_time": _KeySpec(nullable=False, coerce=_coerce_service_time),
    # rebate.py RebateBook.from_facility_docs; assignment/app.py projection;
    # analytics/manager.py settlement; scenario_manager provenance.
    "rebate": _KeySpec(nullable=True, coerce=_coerce_rebate),
}

#: Facility-profile keys that are authored today and read by **nothing**
#: (plan V13; ``docs/solver_boundary_audit.md`` P6/P7/P11). Allow-listing one would
#: manufacture the P11 defect the audit calls *worse than having no flag* — so they
#: are rejected, and the rejection says why rather than reading as an oversight.
DEAD_PROFILE_KEYS: dict[str, str] = {
    "fifo_queue_policy": (
        "a facility profile key that nothing reads (solver_boundary_audit P7/P11); "
        "setting it would change nothing. If queue discipline should be per-facility, "
        "implement the QUEUE_DISCIPLINE registry first — then the key becomes live and "
        "joins the allow-list on merit"
    ),
    "max_queue_size": (
        "a facility profile key that nothing reads (solver_boundary_audit P7/P11); "
        "setting it would change nothing"
    ),
    "status": (
        "a facility profile key that nothing reads — the facility opens "
        "unconditionally at facility/app.py; setting it would change nothing"
    ),
    "operating_hours": (
        "a facility profile key that nothing reads; setting it would change nothing"
    ),
    "operating_days": (
        "a facility profile key that nothing reads; setting it would change nothing"
    ),
}

_LOC_REJECTION = (
    "generation samples only real on-land postal addresses BY CONSTRUCTION (the "
    "2026-06-17 facilities-in-the-sea fix); hand-set coordinates reintroduce that "
    "bug and move a site out from under the facilityRulesWorld digest"
)

#: Keys rejected for a reason OTHER than being dead — stated so the next person does
#: not re-litigate them from the generic allow-list message (§6.2).
_EXPLAINED_REJECTIONS: dict[str, str] = {
    "facility_type": (
        "it is 1:1 derivable from 'code' and the builders' own literal wins anyway, so "
        "it is already authored-and-ignored today; making it settable would break the "
        "code<->type invariant that the trip matrix and order-leg snapping depend on"
    ),
    "name": (
        "it is the addressing key — a rule renaming its own target is self-referential "
        "and invalidates the facilityRulesWorld digest, which is computed over names"
    ),
    "location": _LOC_REJECTION,
    "lat": _LOC_REJECTION,
    "lon": _LOC_REJECTION,
    "footprint": _LOC_REJECTION,
    "publish_facility_stream_kafka": (
        "it is observation, not world; a rule that silently stops streaming a subset of "
        "facilities is indistinguishable from the recurring 'things vanish from the live "
        "map' bug class. It is the first v2 candidate, gated on a named use case"
    ),
    "persist_facility_snapshots": (
        "it is observation, not world; see publish_facility_stream_kafka"
    ),
}


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #

def _matcher_of(rule: dict) -> tuple[str, Any]:
    """The rule's single ``(matcher_key, value)``. Validation guarantees exactly one."""
    match = rule["match"]
    key = next(iter(match))
    return key, match[key]


def rule_rank(rule: dict) -> int:
    return MATCHER_RANKS[_matcher_of(rule)[0]]


def rule_matches(rule: dict, site: dict) -> bool:
    """Does ``rule`` address ``site``? Exact equality on one generated field."""
    key, wanted = _matcher_of(rule)
    if key == "code":
        return str(site.get("code") or "") == str(wanted)
    if key == "name":
        return str(site.get("name") or "") == str(wanted)
    # Unreachable: validation rejects unknown matchers with an Available: [...] list.
    raise FacilityRulesError(f"unknown matcher {key!r}")


def describe_rule(index: int, rule: dict) -> str:
    key, value = _matcher_of(rule)
    return f"rules[{index}] match.{key}={value!r}"


def code_index(metadata: dict) -> dict[str, str]:
    """``{label -> code, prefix -> code}`` from a ``LOCATION_TYPE_METADATA`` mapping.

    A COMPILED facility profile carries ``facility_type`` (the label) and ``name``
    (``<prefix>_###``) but **not** ``code`` — and adding ``code`` to the profile is
    not an option, because it would change every compiled bundle and break the
    migration's equivalence proof. So the run-time provenance stamp, which must
    attribute a value to the ``code`` rule that produced it, inverts the same
    metadata the generator used rather than hardcoding a second mapping.
    """
    index: dict[str, str] = {}
    for code, entry in (metadata or {}).items():
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        prefix = entry.get("prefix")
        if label:
            index[str(label)] = str(code)
        if prefix:
            index[str(prefix)] = str(code)
    return index


def code_for_compiled_facility(profile: dict, index: dict) -> Optional[str]:
    """Recover a compiled facility's location code. ``None`` when unrecoverable —
    never a guess, because a wrong code silently mis-attributes a rule."""
    label = (profile or {}).get("facility_type")
    if label and str(label) in index:
        return index[str(label)]
    name = str((profile or {}).get("name") or "")
    if "_" in name:
        prefix = name.rsplit("_", 1)[0]
        if prefix in index:
            return index[prefix]
    return None


# --------------------------------------------------------------------------- #
# the site fingerprint (§7)
# --------------------------------------------------------------------------- #

def site_digest(sites: Iterable[dict]) -> str:
    """A stable digest of the *generated site list* — an OUTCOME, not the inputs.

    Facility identity depends on ``num_facilities``, the trip matrix (via the
    demand-derived per-code allocation), the facility policy type, the code
    registry, the address-book CSV and a module constant — but **not** on the
    scenario seed. A guard that enumerated those causes would miss one the next
    time the generator changes, so the guard is defined over what came out (§0.4).

    * **Generation order, deliberately not sorted** — order *is* part of the
      binding (``facility_000`` <- ``sites[0]``).
    * **Coordinates rounded to 6 dp** so the digest cannot drift on float repr.
    """
    payload = [
        [
            str(s.get("name")),
            str(s.get("code")),
            round(float(s.get("lat")), 6),
            round(float(s.get("lon")), 6),
        ]
        for s in sites
    ]
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return "blake2b16:" + hashlib.blake2b(raw, digest_size=8).hexdigest()


def code_counts(sites: Iterable[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in sites:
        code = str(s.get("code") or "")
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def world_snapshot(sites: list[dict], *, seed: Any = None, recorded_at: str = "") -> dict:
    """The ``facilityRulesWorld`` block for the CURRENT sites (see §7.1)."""
    snap: dict[str, Any] = {
        "site_digest": site_digest(sites),
        "facility_count": len(sites),
        "code_counts": code_counts(sites),
        # RECORDED AND DELIBERATELY NOT COMPARED (§7.1/§18.1): facility placement is
        # seeded from a module constant, never from the spec seed, so a --reseed
        # provably cannot move a facility. Kept for the human reading the block; a
        # recorded-but-unchecked field otherwise reads like a check, hence this note.
        "seed_at_baseline": seed,
        "_seed_note": (
            "recorded for context only and NOT compared: facility placement does not "
            "read the scenario seed, so --reseed cannot move a facility"
        ),
    }
    if recorded_at:
        snap["recorded_at"] = recorded_at
    return snap


def _name_matchers(rules: Any) -> list[str]:
    """Rules that address a facility BY NAME, for the staleness message.

    Deliberately tolerant of a malformed rule list: this runs *before* schema
    validation so the fingerprint gets the first word (§7.3) — a world that moved
    is the more useful thing to be told, and a bad rule shape is reported a moment
    later by :func:`validate_facility_rules` regardless.
    """
    out = []
    if not isinstance(rules, list):
        return out
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            continue
        match = r.get("match")
        if isinstance(match, dict) and "name" in match:
            out.append(f"    rules[{i}] match.name={match['name']!r}")
    return out


def validate_world_baseline(
    recorded: Any, sites: list[dict], rules: Any, *, slug: str = "?"
) -> None:
    """Refuse a rule list authored against a *different* facility world (§7.3).

    This is the **load-bearing** half of the staleness guard. The zero-match rule
    (§4.5) catches a renamed or deleted facility; it structurally cannot catch the
    expensive case, because the name space is dense and positional: regenerate with
    8 ports instead of 6 and all six original names still exist, every rule still
    matches, nothing errors — while the shared RNG stream means several of them now
    denote different physical sites.
    """
    # --- the world-orphan lock (R2-1, outcome-defined) ----------------------------
    # A baseline is only ever written FOR a rule list — `rules-baseline` refuses a
    # rules-less scenario outright — so "a recorded world with no rules" is an
    # IMPOSSIBLE AUTHORED STATE and can only mean the rules were lost between
    # authoring and here.
    #
    # This is the one guard here that is defined over the OUTCOME rather than over an
    # enumerated door. Every other protection against losing a rule names a mechanism
    # (register in this table, echo in that literal, carry through this path) and every
    # review finding landed on a door that was not on the list. This fires on the
    # resulting STATE, so it catches a null wipe, a hand edit, a save path nobody has
    # written yet, and doors not yet enumerated.
    #
    # It also closes a gap in the guard below: `validate_world_baseline` used to return
    # early on `if not rules`, so the fingerprint itself was silent on a wipe.
    if not rules:
        if recorded is not None:
            raise FacilityRulesError(
                f"spec {slug!r}: facilityRulesWorld is present but facilityRules is "
                f"empty.\n"
                f"A world baseline is only ever recorded FOR a rule list, so this state "
                f"means the rules were lost between authoring and here — most likely a "
                f"save payload carrying \"facilityRules\": null.\n"
                f"  recorded world: {(recorded or {}).get('facility_count')} facilities "
                f"{(recorded or {}).get('code_counts')} "
                f"site_digest={(recorded or {}).get('site_digest')}\n"
                f"Restore the rules, or — if this scenario deliberately has none — "
                f"delete facilityRulesWorld as well. Compiling without the rules would "
                f"silently revert every per-facility value to the blanket layer."
            )
        return
    if recorded is None:
        raise FacilityRulesError(
            f"spec {slug!r}: facilityRules are authored but facilityRulesWorld is "
            f"missing. Facility names and counts are GENERATED, so rules are only "
            f"meaningful against the world they were written for, and that world must "
            f"be recorded explicitly.\n"
            f"    openride scenario rules-baseline {slug}\n"
            f"This is not auto-populated on first compile on purpose: doing so would "
            f"silently bless whatever world happened to exist."
        )
    if not isinstance(recorded, dict):
        raise FacilityRulesError(
            f"spec {slug!r}: facilityRulesWorld must be an object "
            f"(got {type(recorded).__name__})."
        )

    now_digest = site_digest(sites)
    rec_digest = str(recorded.get("site_digest") or "")
    if rec_digest == now_digest:
        return

    named = _name_matchers(rules)
    named_block = (
        "\n  Rules addressing a facility by name:\n" + "\n".join(named) if named else ""
    )
    raise FacilityRulesError(
        f"spec {slug!r}: facilityRules were authored against a different facility "
        f"world and can no longer be trusted to target the facilities you meant.\n\n"
        f"  recorded : {recorded.get('facility_count')} facilities  "
        f"{recorded.get('code_counts')}   site_digest={rec_digest}\n"
        f"  now      : {len(sites)} facilities  {code_counts(sites)}   "
        f"site_digest={now_digest}\n\n"
        f"  Facility names are GENERATED and positional. A name that still exists is "
        f"not necessarily the same physical site: every code is sampled from ONE "
        f"shared RNG stream in code order, so changing ANY code's count moves the "
        f"later ones too.{named_block}\n\n"
        f"  If these rules are still what you want against the NEW world, re-baseline "
        f"explicitly:\n"
        f"      openride scenario rules-baseline {slug}\n"
        f"  Review the rules first. That command records the new world; it does NOT "
        f"check that your rules still mean what you intended."
    )


# --------------------------------------------------------------------------- #
# validation (§5)
# --------------------------------------------------------------------------- #

_RULE_KEYS = ("match", "set")


def _reject_set_key(key: str, where: str) -> None:
    allowed = sorted(SETTABLE_KEYS)
    if key in DEAD_PROFILE_KEYS:
        raise FacilityRulesError(
            f"{where}: {key!r} is {DEAD_PROFILE_KEYS[key]}. "
            f"Settable keys: {allowed}."
        )
    if key in _EXPLAINED_REJECTIONS:
        raise FacilityRulesError(
            f"{where}: {key!r} is not settable because {_EXPLAINED_REJECTIONS[key]}. "
            f"Settable keys: {allowed}."
        )
    raise FacilityRulesError(
        f"{where}: {key!r} is not a settable facility key. Available: {allowed}."
    )


def _name_hint(sites: list[dict], wanted: str) -> str:
    """For a zero-match ``name`` rule, show the names that DO exist nearby."""
    prefix = wanted.rsplit("_", 1)[0] if "_" in wanted else wanted
    same = sorted(
        str(s.get("name"))
        for s in sites
        if str(s.get("name") or "").rsplit("_", 1)[0] == prefix
    )
    if not same:
        prefixes = sorted(
            {str(s.get("name") or "").rsplit("_", 1)[0] for s in sites if s.get("name")}
        )
        return f"No facility name uses the {prefix!r} prefix. Prefixes in use: {prefixes}."
    if len(same) <= 4:
        listed = ", ".join(repr(n) for n in same)
    else:
        listed = f"{same[0]} … {same[-1]}"
    return f"This scenario has {len(same)} facilities with the {prefix!r} prefix: {listed}."


def validate_facility_rules(
    rules: Any, sites: list[dict], codes: Iterable[str], *, slug: str = "?"
) -> list[dict]:
    """Validate an authored ``facilityRules`` block against the generated sites.

    Returns the rule list unchanged (normalised only in that ``None`` becomes
    ``[]``). Raises :class:`FacilityRulesError` on anything ambiguous, before any
    agent is generated.
    """
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise FacilityRulesError(
            f"spec {slug!r}: facilityRules must be a list of "
            f"{{'match': ..., 'set': ...}} objects (got {type(rules).__name__})."
        )
    code_list = sorted({str(c) for c in codes})

    for idx, rule in enumerate(rules):
        where = f"spec {slug!r}: facilityRules[{idx}]"
        if not isinstance(rule, dict):
            raise FacilityRulesError(
                f"{where}: must be an object with 'match' and 'set' "
                f"(got {type(rule).__name__})."
            )
        extra = sorted(set(rule) - set(_RULE_KEYS))
        if extra:
            raise FacilityRulesError(
                f"{where}: unknown key(s) {extra}. A rule carries exactly "
                f"{list(_RULE_KEYS)} — anything else is authoring noise that reads "
                f"like configuration."
            )
        for required in _RULE_KEYS:
            if required not in rule:
                raise FacilityRulesError(f"{where}: missing required key {required!r}.")

        # --- match ---------------------------------------------------------
        match = rule["match"]
        if not isinstance(match, dict):
            raise FacilityRulesError(
                f"{where}.match: must be an object (got {type(match).__name__})."
            )
        if len(match) != 1:
            raise FacilityRulesError(
                f"{where}.match: must carry EXACTLY ONE matcher key, got {sorted(match)}. "
                f"Combining matchers is either redundant or contradictory, and "
                f"supporting it would force specificity to be a lattice instead of a "
                f"total order over {sorted(MATCHER_RANKS)}."
            )
        mkey = next(iter(match))
        if mkey not in MATCHER_RANKS:
            raise FacilityRulesError(
                f"{where}.match: unknown matcher {mkey!r}. "
                f"Available: {sorted(MATCHER_RANKS)}."
            )
        mval = match[mkey]
        if not isinstance(mval, str) or not mval.strip():
            raise FacilityRulesError(
                f"{where}.match.{mkey}: must be a non-empty string (got {mval!r})."
            )
        if mkey == "code" and mval not in code_list:
            raise FacilityRulesError(
                f"{where}.match.code: unknown code {mval!r}. Available: {code_list}."
            )

        # --- set -----------------------------------------------------------
        setter = rule["set"]
        if not isinstance(setter, dict):
            raise FacilityRulesError(
                f"{where}.set: must be an object (got {type(setter).__name__})."
            )
        if not setter:
            raise FacilityRulesError(
                f"{where}.set: must set at least one key. A rule that sets nothing is "
                f"authoring noise that reads like configuration."
            )
        for skey, sval in setter.items():
            if skey not in SETTABLE_KEYS:
                _reject_set_key(str(skey), f"{where}.set")
            spec_ = SETTABLE_KEYS[skey]
            if sval is None:
                if not spec_.nullable:
                    raise FacilityRulesError(
                        f"{where}.set.{skey}: null is not a legal value — there is no "
                        f"'no {skey}'. Remove the key or give it a value."
                    )
                continue
            spec_.coerce(sval, f"{where}.set.{skey}")

        # --- does it bite? (§4.5) ------------------------------------------
        matched = [s for s in sites if rule_matches(rule, s)]
        if not matched:
            hint = (
                _name_hint(sites, mval)
                if mkey == "name"
                else f"No facility carries code {mval!r}."
            )
            raise FacilityRulesError(
                f"{where} matches no facility — match.{mkey}={mval!r}.\n"
                f"{hint}\n"
                f"Facility names are GENERATED from num_facilities, the trip matrix and "
                f"the address book; they are not stable across a change to any of "
                f"those. Available codes: {code_list}."
            )

    _reject_equal_rank_conflicts(rules, sites, slug=slug)
    return list(rules)


def _reject_equal_rank_conflicts(rules: list[dict], sites: list[dict], *, slug: str) -> None:
    """Two rules of the SAME rank setting the SAME key on the SAME facility are a
    compile error — **including when the values are identical** (§4.4).

    Rejecting equal values too follows the precedent already set for duplicate
    ``hour`` entries in a rebate schedule: a "harmless" duplicate is how a later
    edit to one of the two becomes a silent divergence. Disjoint keys at the same
    rank are *not* a conflict — composing rules by concern is intended (§2.2).
    """
    # (rank, key) -> {rule_index: set(matched facility names)}
    seen: dict[tuple[int, str], list[tuple[int, set[str]]]] = {}
    for idx, rule in enumerate(rules):
        rank = rule_rank(rule)
        names = {str(s.get("name")) for s in sites if rule_matches(rule, s)}
        for key in rule["set"]:
            seen.setdefault((rank, key), []).append((idx, names))

    for (rank, key), entries in sorted(seen.items()):
        if len(entries) < 2:
            continue
        for pos, (i, names_i) in enumerate(entries):
            for j, names_j in entries[pos + 1:]:
                overlap = names_i & names_j
                if not overlap:
                    continue
                example = sorted(overlap)[0]
                matcher_i = _matcher_of(rules[i])[0]
                raise FacilityRulesError(
                    f"spec {slug!r}: facilityRules conflict — two rules of equal "
                    f"specificity (match.{matcher_i}, rank {rank}) both set {key!r} on "
                    f"{len(overlap)} facilities, including {example!r}.\n"
                    f"    {describe_rule(i, rules[i])}  set.{key}="
                    f"{rules[i]['set'][key]!r}\n"
                    f"    {describe_rule(j, rules[j])}  set.{key}="
                    f"{rules[j]['set'][key]!r}\n"
                    f"Rules of equal specificity are resolved by neither order nor "
                    f"value: merge them into one rule, or address the facilities you "
                    f"actually mean by name."
                )


# --------------------------------------------------------------------------- #
# resolution (§4.2)
# --------------------------------------------------------------------------- #

@dataclass
class Resolution:
    """The per-facility outcome, parallel to the ``sites`` list it was built from."""

    #: ``sites[i]`` -> ``{key: effective value}``. ``rebate`` may be ``None``
    #: ("this facility gets no schedule"), in which case the caller must OMIT
    #: ``profile['rebate']`` rather than stamp a null.
    per_site: list[dict] = field(default_factory=list)
    #: ``sites[i]`` -> ``{key: human source string}`` — for the provenance stamp.
    sources: list[dict] = field(default_factory=list)
    #: One entry per authored rule: how many facilities it addressed and which keys.
    rules_applied: list[dict] = field(default_factory=list)


def _blanket_value(key: str, site: dict, blanket: dict) -> tuple[Any, str]:
    """The rank-0 value for ``key`` and its source label.

    ``overrides.facility`` skips ``None`` (the blanket merge's own idiom), then the
    datagen default already stamped on the site.
    """
    if key == "rebate":
        # A schedule ALREADY on the site is the rank-0 value — symmetric with
        # gate_count/service_time, which the blanket loop stamps onto the site
        # before this runs. Nothing else ever writes it: catalog.facility_sites
        # does not, and "facilities" is an _UNSAFE_OVERRIDE_KEY so no authored
        # payload can smuggle one in.
        existing = site.get("rebate")
        if existing is not None:
            return existing, "overrides.facility"
        raw = blanket.get("rebate")
        if raw is None:
            return None, "datagen default (no schedule)"
        return _coerce_rebate(raw, "overrides.facility.rebate"), "overrides.facility"
    if blanket.get(key) is not None:
        return blanket[key], "overrides.facility"
    return site.get(key), "datagen default"


def resolve_facility_rules(
    rules: Optional[list[dict]],
    sites: list[dict],
    blanket: Optional[dict] = None,
    *,
    slug: str = "?",
    warn: bool = True,
) -> Resolution:
    """Resolve every allow-listed key for every site. **Per key, not per rule.**

    ``blanket`` is ``settings['facility']['profile']`` — the rank-0 layer.
    Draws zero RNG; the only work is dict lookups and (for ``rebate``) the shared
    schedule parse.
    """
    rules = list(rules or [])
    blanket = blanket or {}

    per_site: list[dict] = []
    sources: list[dict] = []
    # rule index -> matched facility count, and whether it changed anything
    matched_counts = [0] * len(rules)
    effective_counts = [0] * len(rules)

    # Pre-compute rank once; the table is a total order so max() is unambiguous.
    ranks = [rule_rank(r) for r in rules]

    for site in sites:
        matching = [i for i, r in enumerate(rules) if rule_matches(r, site)]
        for i in matching:
            matched_counts[i] += 1

        values: dict[str, Any] = {}
        srcs: dict[str, str] = {}
        for key, spec_ in SETTABLE_KEYS.items():
            base_value, base_source = _blanket_value(key, site, blanket)

            # PRESENCE, not truthiness: `{"rebate": null}` is a candidate.
            candidates = [i for i in matching if key in rules[i]["set"]]
            if not candidates:
                values[key] = base_value
                srcs[key] = base_source
                continue

            best = max(ranks[i] for i in candidates)
            winners = [i for i in candidates if ranks[i] == best]
            if len(winners) > 1:  # pragma: no cover - validation refuses this earlier
                raise FacilityRulesError(
                    f"spec {slug!r}: facilityRules conflict on {site.get('name')!r} for "
                    f"{key!r} at rank {best} "
                    f"({', '.join(describe_rule(i, rules[i]) for i in winners)})."
                )
            win = winners[0]
            raw = rules[win]["set"][key]
            value = None if raw is None else spec_.coerce(raw, f"facilityRules[{win}].set.{key}")
            values[key] = value
            srcs[key] = describe_rule(win, rules[win])
            if value != base_value:
                effective_counts[win] += 1

        per_site.append(values)
        sources.append(srcs)

    rules_applied = [
        {
            "index": i,
            "match": dict(r["match"]),
            "set_keys": sorted(r["set"]),
            "matched": matched_counts[i],
        }
        for i, r in enumerate(rules)
    ]

    if warn:
        for i, r in enumerate(rules):
            if matched_counts[i] and not effective_counts[i]:
                # A no-op rule is how someone believes a lever is engaged when it is
                # not — the P11 shape. Not fatal: re-stating a default is legal.
                logging.warning(
                    "spec %r: facilityRules[%d] (%s) matches %d facilities but changes "
                    "no effective value — it sets %s to what they already resolve to. "
                    "If a lever was intended, it is NOT engaged.",
                    slug, i, describe_rule(i, r), matched_counts[i], sorted(r["set"]),
                )

    return Resolution(per_site=per_site, sources=sources, rules_applied=rules_applied)


def apply_resolution(sites: list[dict], resolution: Resolution) -> None:
    """Stamp the resolved values onto the sites, in place.

    Stamping the **site** (rather than the profile) is what removes the two-builder
    divergence class instead of mitigating it: both builders already read
    ``gate_count``/``service_time`` off the site, and now read ``rebate`` there too.
    It also gives the V11 property for free — the facility agent emits ``gate_count``
    both at the top level (which the runtime schema validates) and inside
    ``profile`` (which ``facility/manager.py`` actually reads). Patching only one of
    those passes the schema and silently changes nothing, or silently changes
    queueing without the bundle saying so.
    """
    if len(sites) != len(resolution.per_site):  # pragma: no cover - programming error
        raise FacilityRulesError(
            f"resolution covers {len(resolution.per_site)} sites but {len(sites)} were given"
        )
    for site, values in zip(sites, resolution.per_site):
        for key, value in values.items():
            if key == "rebate":
                if value is None:
                    site.pop("rebate", None)
                else:
                    site["rebate"] = value
            else:
                site[key] = value
