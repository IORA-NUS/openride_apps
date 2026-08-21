"""Frontend scenario generation spec, slug helpers, and config overrides."""

from __future__ import annotations

import json
import os
import re
import shutil
from contextlib import contextmanager
from copy import deepcopy
from enum import Enum, auto
from datetime import datetime, timezone
from typing import Any

from . import scenario_config
from ..assignment.solver import DEFAULT_SOLVER, SOLVER_REGISTRY
from .order_demand import (
    LEGACY_EARLY_ORDER_COUNT,
    hourly_weights_from_spec,
    parse_order_demand_curve,
    recommended_early_order_count,
)

# Selectable assignment strategies, sourced from the solver registry so the
# frontend/scenario layer can never drift from what the sim can actually run.
KNOWN_SOLVERS = frozenset(SOLVER_REGISTRY)


def normalize_solver(raw: Any) -> str:
    """Coerce a requested solver name to a known one, else the default."""
    name = str(raw or "").strip()
    return name if name in KNOWN_SOLVERS else DEFAULT_SOLVER


def parse_trip_matrix(raw: Any) -> dict[str, Any]:
    """Normalize a frontend-supplied trip matrix, falling back to the config-layer
    observed default (``scenario_config.DEFAULT_TRIP_MATRIX``) when the input is
    empty / all-diagonal.

    The underlying datagen ``parse_trip_matrix`` is strict (raises when no matrix
    is provided) — this user-facing proxy supplies the default so the editor stays
    forgiving. Imported lazily because location_sampler pulls in datagen.
    """
    from .location_sampler import parse_trip_matrix as _impl
    from .location_sampler import restrict_trip_matrix as _restrict

    codes = scenario_config._location_catalog().codes()
    try:
        return _restrict(_impl(raw), codes)
    except ValueError:
        return _restrict(_impl(scenario_config.DEFAULT_TRIP_MATRIX), codes)

BEHAVIOR_FILES = (
    "truck_behavior.json",
    "order_behavior.json",
    "facility_behavior.json",
    "assignment_behavior.json",
    "analytics_behavior.json",
    "orsim_settings.json",
)

META_FILENAME = "scenario_meta.json"
MAX_TRUCKS = 1_000_000
MAX_SIMULATION_DAYS = 7
MIN_TRUCKS = 1
ORDERS_PER_TRUCK_PER_DAY = scenario_config.ORDERS_PER_TRUCK_PER_DAY
DEFAULT_FACILITY_COUNT = scenario_config.NUM_FACILITIES
MAX_FACILITY_COUNT = 1_000_000
PLATFORM_DEFAULT_SCENARIO_SLUG = "default_container_logistics_7d"
PROTECTED_SCENARIO_SLUGS = frozenset(
    {
        PLATFORM_DEFAULT_SCENARIO_SLUG,
        "default_container_logistics",
    }
)
PREVIEW_SAMPLE_LIMIT = 20

RESERVED_SLUGS = frozenset(
    {
        "default",
        "con",
        "prn",
        "aux",
        "nul",
        "com1",
        "lpt1",
    }
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def slugify(name: str, *, fallback: str = "scenario") -> str:
    """Convert display name to a filesystem-safe slug."""
    lowered = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", lowered)
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        slug = fallback
    return slug[:64]


def sanitize_run_name(name: str | None) -> str | None:
    if name is None:
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(name)).strip()
    if not cleaned:
        return None
    return cleaned[:120]


def validate_slug(slug: str) -> str | None:
    """Return error message if slug is invalid, else None."""
    if not slug or not isinstance(slug, str):
        return "Slug is required"
    slug = slug.strip()
    if slug in RESERVED_SLUGS:
        return f"Slug {slug!r} is reserved"
    if ".." in slug or "/" in slug or "\\" in slug:
        return "Slug must not contain path separators"
    if not _SLUG_RE.match(slug):
        return "Slug must be 1–64 chars: lowercase letters, digits, underscore, hyphen"
    return None


SCENARIO_FOLDER_SEGMENT = "scenarios"


def container_logistics_scenarios_root() -> str:
    """Scenarios live in a **global folder outside the domain package**:
    ``openride_apps/scenarios/`` (plan §14.1). Sources (spec.json, scenario_gen.py,
    inputs/) are git-tracked there; compiled artifacts (scenario.json, index.json,
    _index.json, _registry.json) are gitignored — see .gitignore.

    Overridable via ``ORSIM_SCENARIOS_DIR`` (used by tests for isolation so they
    never write into the real source tree)."""
    override = os.environ.get("ORSIM_SCENARIOS_DIR", "").strip()
    if override:
        return override
    # this file: openride_apps/apps/container_logistics/scenario/frontend_scenario_spec.py
    # parents:   scenario -> container_logistics -> apps -> openride_apps (repo root)
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    return os.path.join(repo_root, SCENARIO_FOLDER_SEGMENT)


def scenario_root(datahub_dir: str = "", domain: str = "") -> str:
    """The container_logistics scenarios root — anchored, not derived from ``datahub_dir``.

    The anchoring is deliberate (plan §14.1): scenarios live in one global folder, not
    per-datahub. What was NOT deliberate is that this signature accepted a path and
    silently discarded it, so a caller that passed a temporary directory expecting
    isolation got the REAL ``scenarios/`` tree and wrote into it. That is how a probe
    ended up writing into the source tree (review R2-15).

    So the parameter stays — removing it would break every call site — but a non-empty
    ``datahub_dir`` that the resolved root does **not** live under is now an error
    rather than a silent no-op. A caller passing the repo root (the normal case) or a
    tmp root together with ``ORSIM_SCENARIOS_DIR`` (the test-isolation case) is
    consistent and passes; a caller passing an unrelated path is told, loudly, that its
    isolation is not real.
    """
    root = container_logistics_scenarios_root()
    # The rule is "is this caller plausibly talking about THIS checkout?", NOT "does
    # datahub_dir contain the scenarios root". The latter looks tempting and is wrong:
    # the production caller passes `<repo>/datahub`, which of course does not contain
    # `<repo>/scenarios`, so it rejects the normal path. (It did — this exact mistake
    # broke `list-scenarios` and a 500-truck verification run before the test below
    # existed. The suite missed it because every test used a synthetic path.)
    #
    # A caller inside this checkout is legitimately using the anchored root. A caller
    # passing an unrelated absolute path — a tmp dir, which is what a probe expecting
    # isolation passes — is not, and is told so instead of silently getting the real
    # tree. Tests that genuinely want isolation set ORSIM_SCENARIOS_DIR, which is
    # honoured and exempt.
    if (
        datahub_dir
        and str(datahub_dir).strip()
        and not os.environ.get("ORSIM_SCENARIOS_DIR", "").strip()
    ):
        base = os.path.abspath(str(datahub_dir).strip())
        repo_root = os.path.dirname(os.path.abspath(root))
        inside_checkout = base == repo_root or base.startswith(repo_root + os.sep)
        if not inside_checkout:
            raise ValueError(
                f"scenario_root: datahub_dir={datahub_dir!r} is outside this "
                f"checkout, but container_logistics scenarios are anchored to "
                f"{root!r} and are NOT derived from datahub_dir. This path would have "
                f"been silently ignored and you would have read/written the REAL "
                f"scenarios tree — which is how a probe once wrote into the source "
                f"tree. For isolation set ORSIM_SCENARIOS_DIR; for the anchored root "
                f"pass no datahub_dir."
            )
    return root


# Back-compat alias (older callers / tests may import the previous name).
scenario_dataset_root = scenario_root


def scenario_dir(datahub_dir: str, domain: str, slug: str) -> str:
    root = scenario_root(datahub_dir, domain)
    target = os.path.abspath(os.path.join(root, slug))
    if not target.startswith(os.path.abspath(root) + os.sep) and target != os.path.abspath(root):
        raise ValueError(f"Invalid scenario slug: {slug!r}")
    return target


def behaviors_complete(scenario_path: str) -> bool:
    """Legacy six-file completeness check (un-migrated scenarios)."""
    return all(os.path.isfile(os.path.join(scenario_path, fname)) for fname in BEHAVIOR_FILES)


def bundle_complete(scenario_path: str) -> bool:
    """True when a valid self-contained ``scenario.json`` bundle is present."""
    from .scenario_bundle import read_bundle

    bundle = read_bundle(scenario_path)
    return bool(bundle and isinstance(bundle.get("agents"), dict) and isinstance(bundle.get("settings"), dict))


def scenario_complete(scenario_path: str) -> bool:
    """A scenario is runnable if it has a bundle OR the legacy six files."""
    return bundle_complete(scenario_path) or behaviors_complete(scenario_path)


def scenario_meta_view(scenario_path: str) -> dict[str, Any] | None:
    """Meta-shaped view sourced from the bundle (preferred) or legacy scenario_meta.json.

    Agent counts are taken from the recipe (per-day, editor round-trip), falling back
    to the bundle's realized totals.
    """
    from .scenario_bundle import read_bundle

    bundle = read_bundle(scenario_path)
    if not bundle:
        return read_meta(scenario_path)
    return scenario_meta_from_bundle(bundle)


def scenario_meta_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """The pure half of :func:`scenario_meta_view`, for callers that already parsed the bundle.

    The run-load path holds the parsed bundle in memory; re-reading the file just to derive
    this small meta view costs a full multi-hundred-MB JSON parse per call.
    """
    recipe = bundle.get("recipe") if isinstance(bundle.get("recipe"), dict) else {}
    counts = bundle.get("counts") if isinstance(bundle.get("counts"), dict) else {}
    recipe_agents = recipe.get("agents") if isinstance(recipe.get("agents"), dict) else {}
    agents = recipe_agents or {
        "truck": {"count": counts.get("truck")},
        "order": {"count": counts.get("order")},
        "facility": {"count": counts.get("facility")},
    }
    return {
        "name": bundle.get("name"),
        "slug": bundle.get("slug"),
        "domain": bundle.get("domain"),
        "createdAt": bundle.get("createdAt"),
        "source": bundle.get("source"),
        "simulationDays": recipe.get("simulationDays"),
        "orderCountUnit": recipe.get("orderCountUnit"),
        "agents": agents,
        "orderDemandCurve": recipe.get("orderDemandCurve"),
        "hauliers": recipe.get("hauliers"),
        "roleSettings": recipe.get("roleSettings"),
        # The FOURTH closed literal this key passes through (the facility rules plan
        # names three). Carried so a loaded bundle can still say which rules produced
        # its facilities: the run provenance stamp derives its VALUES from the
        # compiled collection, but it can only attribute them to a rule if it can see
        # the authored rules, and this is the only channel that survives a bundle load.
        "facilityRules": recipe.get("facilityRules"),
        "facilityRulesWorld": recipe.get("facilityRulesWorld"),
    }


def read_meta(scenario_path: str) -> dict[str, Any] | None:
    meta_path = os.path.join(scenario_path, META_FILENAME)
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_meta(scenario_path: str, meta: dict[str, Any]) -> None:
    meta_path = os.path.join(scenario_path, META_FILENAME)
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump(meta, fp, indent=2, sort_keys=True)
        fp.write("\n")


# The editable construction source: spec.json is the canonical recipe (same shape as
# the recipe mirrored inside scenario.json), hoisted to a first-class source file so a
# scenario can be hand-edited and recompiled from its own folder. See docs/scenario_workflow.md.
SPEC_FILENAME = "spec.json"


def read_spec(scenario_path: str) -> dict[str, Any] | None:
    spec_path = os.path.join(scenario_path, SPEC_FILENAME)
    if not os.path.isfile(spec_path):
        return None
    try:
        with open(spec_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_spec(scenario_path: str, payload: dict[str, Any]) -> None:
    os.makedirs(scenario_path, exist_ok=True)
    target = os.path.join(scenario_path, SPEC_FILENAME)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, indent=2, sort_keys=True)
        fp.write("\n")
    os.replace(tmp, target)


def _spec_slug(raw: dict[str, Any]) -> str:
    """The slug :func:`assemble_spec` will assign (name-slugified only as a default).

    Shared so boundary callers can locate the target folder *before* assembling —
    they must never re-derive it, or the two drift.
    """
    name = str(raw.get("name") or "").strip()
    return str(raw.get("slug") or slugify(name)).strip()


def _authored_pools(structure: Any) -> list[dict[str, Any]] | None:
    """A structure's authored ``pools``, or ``None`` when the author wrote none.

    ``None`` (key absent) means *edges-authored* — the recipe echoes ``pools``
    back only for the structures whose author supplied it
    (``datagen/preprocess.py``), so an edges-authored structure must never gain
    the key. A present-but-empty list is authored intent and is kept as ``[]``.
    """
    if not isinstance(structure, dict):
        return None
    raw = structure.get("pools")
    if not isinstance(raw, (list, tuple)):
        return None
    pools: list[dict[str, Any]] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        members = [str(m) for m in (p.get("members") or [])]
        pools.append({"id": str(p.get("id") or ""), "members": members})
    return pools


def _cooperation_structures(cooperation: Any) -> list[dict[str, Any]]:
    """The structure dicts of a cooperation block (empty for any other shape)."""
    if not isinstance(cooperation, dict):
        return []
    raw = cooperation.get("structures")
    if not isinstance(raw, (list, tuple)):
        return []
    return [s for s in raw if isinstance(s, dict)]


def cooperation_content_structure_ids(cooperation: Any) -> set[str]:
    """Ids of the structures that carry authored cooperation CONTENT.

    Content = at least one edge or at least one pool. The always-present
    ``no-coop`` baseline has neither, so a scenario without collaboration has an
    empty content set and can never trip the destructive-save guard below.
    """
    out: set[str] = set()
    for s in _cooperation_structures(cooperation):
        if s.get("edges") or s.get("pools"):
            sid = str(s.get("id") or "").strip()
            if sid:
                out.add(sid)
    return out


def _carry_authored_pools(incoming: Any, previous: Any) -> Any:
    """Re-attach an authored ``pools`` list the incoming client dropped.

    Pools are the canonical sharing primitive; edges are derived sugar. A client
    that speaks only edges (the Scenario editor before FIX-6) hydrates a
    3-member pool as a 3-edge clique and posts that back, which recompiles into
    **three 2-member pools** — a different market, silently (review finding F2).
    So: for every structure the previous ``spec.json`` authored pools for whose
    incoming counterpart carries no ``pools`` key, carry the authored pools over.

    A client that DOES send ``pools`` stays authoritative (never overridden).
    If the carried pools and the incoming edges disagree, ``normalize_cooperation``
    raises — loudly, which is the point; it is never resolved silently here.
    """
    prev_pools: dict[str, list[dict[str, Any]]] = {}
    for s in _cooperation_structures(previous):
        sid = str(s.get("id") or "").strip()
        pools = _authored_pools(s)
        if sid and pools is not None:
            prev_pools[sid] = pools
    if not prev_pools or not isinstance(incoming, dict):
        return incoming
    structures = incoming.get("structures")
    if not isinstance(structures, (list, tuple)):
        return incoming
    out: list[Any] = []
    changed = False
    for s in structures:
        if isinstance(s, dict) and s.get("pools") is None:
            sid = str(s.get("id") or "").strip()
            if sid in prev_pools:
                s = {**s, "pools": deepcopy(prev_pools[sid])}
                changed = True
        out.append(s)
    if not changed:
        return incoming
    return {**incoming, "structures": out}


class Carry(Enum):
    """How ``assemble_spec`` decides a key's value when merging over a saved spec.

    Plan §14.5 (R3-3). ``assemble_spec`` used to RECONSTRUCT the spec from a flat
    18-key dict literal in which exactly one key consulted ``previous``. A dashboard
    save sends ``cooperation`` but not ``planner``/``solverParams``/``overrides``/
    ``earlyOrderCount``, so all four were rewritten to ``null`` — and because
    ``_normalize_planner(None)`` rebuilds ``topology="partitioned"``, **a save
    silently turned shared-pool planning off while leaving the user's pools visibly
    intact in the UI**. That is the class defect; adding ``planner`` to the payload
    would have left the other three broken and key nineteen broken by default.
    """

    #: The body (or a derived value) is authoritative; never inherited.
    FROM_BODY = auto()
    #: The body wins if it SUPPLIED the key; otherwise inherit from ``previous``.
    CARRY_IF_ABSENT = auto()
    #: Structural merge against ``previous`` (cooperation's authored pools today).
    MERGE_NESTED = auto()


#: Every top-level key ``spec.json`` may carry, and how it survives a save.
#: ``assemble_spec`` ITERATES this, so a key that is not registered is not emitted
#: at all — a loud, immediate failure rather than a silent null.
#: Order is the historical key order, so a written spec.json is byte-comparable.
SPEC_KEYS: dict[str, Carry] = {
    "name": Carry.FROM_BODY,
    "slug": Carry.FROM_BODY,
    "domain": Carry.FROM_BODY,
    "source": Carry.FROM_BODY,
    "simulationDays": Carry.CARRY_IF_ABSENT,
    "seed": Carry.CARRY_IF_ABSENT,
    "orderCountUnit": Carry.CARRY_IF_ABSENT,
    # A scenario's own simulation epoch. Registered rather than defaulted so a
    # scenario can DECLARE its hour axis instead of inheriting a global constant —
    # the P8 shape this feature must not reproduce. Unregistered, it would be
    # silently dropped on every dashboard save (G23), which is exactly the defect
    # F1 found one level down in `_normalize_planner`.
    "referenceTime": Carry.CARRY_IF_ABSENT,
    "agents": Carry.CARRY_IF_ABSENT,
    "earlyOrderCount": Carry.CARRY_IF_ABSENT,
    "orderDemandCurve": Carry.CARRY_IF_ABSENT,
    "tripMatrix": Carry.CARRY_IF_ABSENT,
    "hauliers": Carry.CARRY_IF_ABSENT,
    "cooperation": Carry.MERGE_NESTED,
    "planner": Carry.CARRY_IF_ABSENT,
    "solver": Carry.CARRY_IF_ABSENT,
    "solverParams": Carry.CARRY_IF_ABSENT,
    "roleSettings": Carry.CARRY_IF_ABSENT,
    "overrides": Carry.CARRY_IF_ABSENT,
    # --- per-facility rules (facility rules plan §9) -------------------------
    # Registered at the TOP LEVEL on purpose. An unregistered key is silently
    # stripped from every saved spec.json — ``assemble_spec`` builds its output
    # solely from this table — which is the G23/F1 defect the rebate work walked
    # into one level down in ``_normalize_planner``. Rules are not overrides: an
    # override is blanket, a rule is targeted, and burying the two in one key is
    # what made ``rebate_by_code`` read like a sub-feature of the blanket merge.
    #
    # CARRY_IF_ABSENT, matching ``overrides``/``referenceTime``. Note that
    # ``_key_supplied`` is PRESENCE-based, so ``"facilityRules": []`` is a supplied
    # empty list ("this scenario deliberately has no rules") that CLEARS an
    # inherited one, while an absent key inherits.
    #
    # CORRECTED (F1). This comment used to read "Neither key belongs in
    # _NULL_MEANS_UNSUPPLIED" and that instruction was WRONG: `[]` is in a list's
    # domain and legitimately clears, but `null` is NOT, and honouring it as a clear
    # wipes the rule list. Both keys are now NullPolicy.UNSUPPLIED — see
    # SPEC_KEY_NULL_POLICY, whose completeness is enforced at import so the next key
    # cannot skip the question.
    "facilityRules": Carry.CARRY_IF_ABSENT,
    # The recorded facility world the rules were authored against. It lives in
    # spec.json and is never RECOMPUTED in the compiled scenario.json — but it IS
    # echoed verbatim through $.recipe, and that echo is what makes
    # recompile-from-bundle work. (Corrected: this used to say "never in
    # scenario.json", which licenses deleting the echo.) What must never happen is
    # the bundle regenerating the digest: it would then always match and guard
    # nothing, while looking exactly like a guard.
    "facilityRulesWorld": Carry.CARRY_IF_ABSENT,
}

#: Accepted spellings per key. A client that speaks any alias has SUPPLIED the key.
SPEC_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "simulationDays": ("simulationDays", "simulation_days"),
    "roleSettings": ("roleSettings", "roleProfiles"),
    "agents": ("agents", "numTrucks", "numOrders", "numFacilities"),
}


class NullPolicy(Enum):
    """What an explicit ``null`` MEANS for one spec key.

    The question every key must answer is **"is ``null`` inside this key's value
    domain?"** — not "is this key important?". A list's clear is spelled ``[]``, a
    string's is ``""``; for those, ``null`` is a malformed value and the only thing
    it can honestly mean is *the client did not speak this key*.
    """

    #: ``null`` is OUT of the domain -> treat as "not spoken" and inherit.
    UNSUPPLIED = auto()
    #: ``null`` is IN the domain -> honour the clear.
    CLEARS = auto()


#: **Mandatory, one entry per SPEC_KEYS key**, enforced at import below.
#:
#: This replaces an opt-in ``frozenset`` in which every newly registered key defaulted
#: to the dangerous behaviour. That list was added for ``referenceTime`` and then not
#: maintained: ``facilityRules`` was registered with an explicit code comment saying it
#: did **not** belong here, and a save payload carrying ``"facilityRules": null`` would
#: have wiped the rule list and silently reverted every per-facility physics value.
#: Two rounds, two keys, same hole — so the question is now unanswerable-by-default
#: rather than answered-by-default-wrongly.
#:
#: Each ``CLEARS`` entry carries its own justification. ``UNSUPPLIED`` is the safe
#: answer and needs one only where it is surprising.
SPEC_KEY_NULL_POLICY: dict[str, NullPolicy] = {
    # --- identity: required strings, FROM_BODY (policy not consulted, answered anyway
    # so a future Carry change inherits a decision rather than a default) ------------
    "name": NullPolicy.UNSUPPLIED,
    "slug": NullPolicy.UNSUPPLIED,
    "domain": NullPolicy.UNSUPPLIED,
    "source": NullPolicy.UNSUPPLIED,
    # --- calendar / generation inputs -----------------------------------------------
    # Every one of these falls back to a GLOBAL DEFAULT when absent, so honouring a
    # null as a clear silently reverts an authored scenario to the caller's default —
    # the P8 shape, and exactly how referenceTime was lost.
    "simulationDays": NullPolicy.UNSUPPLIED,
    "seed": NullPolicy.UNSUPPLIED,
    "orderCountUnit": NullPolicy.UNSUPPLIED,
    "referenceTime": NullPolicy.UNSUPPLIED,
    "agents": NullPolicy.UNSUPPLIED,
    # CLEARS: `null` is the ONLY spelling of "this scenario has no demand curve"
    # (uniform arrival time). An empty list is not a curve, and the compile path
    # already reads absent-or-null as "uniform" rather than as a default curve, so
    # null is genuinely inside this key's domain.
    "orderDemandCurve": NullPolicy.CLEARS,
    # A null trip matrix silently substitutes D.DEFAULT_TRIP_MATRIX, which
    # re-partitions the facility code mix (the per-code counts are matrix-derived) —
    # i.e. it moves facilities. Never a clear.
    "tripMatrix": NullPolicy.UNSUPPLIED,
    # A null haulier list rebuilds a default single haulier, which silently collapses
    # the multi-haulier experiment this project exists to run. "No hauliers" is `[]`.
    "hauliers": NullPolicy.UNSUPPLIED,
    # MERGE_NESTED (policy not consulted). A null cooperation block rebuilds
    # topology="partitioned" — the CRITICAL-1 defect that turned shared-pool planning
    # off while the pools stayed visibly intact in the editor.
    "cooperation": NullPolicy.UNSUPPLIED,
    # --- keys whose existing, TESTED contract is that null clears --------------------
    # `test_presence_not_truthiness_so_a_client_can_clear_a_field` pins these four.
    # For each, null is the established spelling of "no override at all", and there is
    # no second spelling that means the same thing.
    "earlyOrderCount": NullPolicy.CLEARS,
    "planner": NullPolicy.CLEARS,
    "solverParams": NullPolicy.CLEARS,
    "overrides": NullPolicy.CLEARS,
    # CLEARS: solver and planner.deployment.type are ONE dial and `solver` wins when
    # both are given, so null is how an author says "defer to the planner". No other
    # spelling expresses that.
    "solver": NullPolicy.CLEARS,
    # CLEARS: the legacy sibling of `overrides`; same semantics, same reasoning.
    "roleSettings": NullPolicy.CLEARS,
    # --- per-facility rules ----------------------------------------------------------
    # THE F1 DEFECT. A rule list's clear is `[]`; `null` is a malformed list and is
    # what a generic form serialiser emits for an untouched field. Honouring it as a
    # clear reverts every per-facility gate_count/service_time/rebate to the blanket
    # layer — 300 facilities at one gate each — with nothing in the bundle saying so.
    "facilityRules": NullPolicy.UNSUPPLIED,
    # There is no "cleared world": a baseline is either recorded or deleted outright.
    # A null here would also orphan the rules it was recorded for.
    "facilityRulesWorld": NullPolicy.UNSUPPLIED,
}

# Completeness is enforced at IMPORT, so it fires on test collection rather than on
# the one save payload that happens to carry a null. Precedent: the RuntimeError below
# for a registered key with no value builder — the one other place this module refuses
# to let an author skip a question.
_missing_null_policy = set(SPEC_KEYS) - set(SPEC_KEY_NULL_POLICY)
if _missing_null_policy:
    raise RuntimeError(
        f"SPEC_KEYS entries with no declared null policy: {sorted(_missing_null_policy)}. "
        f"Every key must answer whether an explicit null means 'clear it' (NullPolicy."
        f"CLEARS) or 'the client did not speak it' (NullPolicy.UNSUPPLIED). The test is "
        f"whether null is inside the key's value domain — a list clears with [], a "
        f"string with ''. An unanswered key silently meant 'clear it', which is how "
        f"referenceTime (R3-1) and facilityRules (F1) were both lost."
    )
_stale_null_policy = set(SPEC_KEY_NULL_POLICY) - set(SPEC_KEYS)
if _stale_null_policy:
    raise RuntimeError(
        f"SPEC_KEY_NULL_POLICY declares keys that are not in SPEC_KEYS: "
        f"{sorted(_stale_null_policy)}. Remove them, or register them."
    )

#: Back-compat view, DERIVED from the table above so it can never disagree with it.
#: Kept because existing tests and callers import it; it is no longer authored.
_NULL_MEANS_UNSUPPLIED = frozenset(
    k for k, v in SPEC_KEY_NULL_POLICY.items() if v is NullPolicy.UNSUPPLIED
)


def _key_supplied(raw: dict[str, Any], key: str) -> bool:
    """Did the client actually speak this key (under any accepted spelling)?

    **Presence, not truthiness.** ``key in raw`` is what distinguishes *"the client
    omitted this"* (inherit) from *"the client cleared it"* (honour the clear).
    A truthiness test would make clearing a field impossible — the bug the obvious
    fix introduces.
    """
    if not isinstance(raw, dict):
        return False
    supplied = any(alias in raw for alias in SPEC_KEY_ALIASES.get(key, (key,)))
    if supplied and SPEC_KEY_NULL_POLICY.get(key) is NullPolicy.UNSUPPLIED:
        # For a key whose domain excludes null, an explicit `null` is NOT a "clear" —
        # it is how a declared value is silently LOST. A generic form serialiser emits
        # null for an untouched field, which would otherwise stand the carry down and
        # wipe the value, reverting the scenario to a caller/global default.
        #
        # This was a per-key exception (`referenceTime` only) and that is precisely
        # why it failed a second time: an opt-in safety list leaves every NEW key
        # defaulting to the dangerous behaviour, and `facilityRules` was registered
        # with a comment explicitly declining to join it. It is now a MANDATORY,
        # import-checked table (SPEC_KEY_NULL_POLICY), so the question cannot be
        # skipped rather than merely being answerable.
        #
        # Clearing stays possible for every key whose domain contains null, and for
        # the rest it is spelled with the empty value: `[]`, `""`, `{}`.
        value = next(
            (raw[a] for a in SPEC_KEY_ALIASES.get(key, (key,)) if a in raw), None
        )
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    return supplied


def assemble_spec(
    raw: dict[str, Any], domain: str, *, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Shape an incoming payload into the canonical ``spec.json`` form — **no validation.**

    The active generation path (``generate_scenario`` / ``compile_scenario`` /
    ``edit_scenario``) uses this instead of ``normalize_generate_spec``: the
    ``Preprocessor`` is the SINGLE validation surface, so this only maps legacy/alt
    input keys (``numTrucks`` -> ``agents.truck.count``, ``roleProfiles`` ->
    ``roleSettings``) into the shape the Preprocessor reads and forwards everything
    else verbatim — including ``{"$file": ...}`` matrix/curve refs, which the
    Preprocessor resolves. No clamping, no name/slug rejection, no order-unit
    coercion, no early-order clamp, no matrix/curve parsing, no haulier/solver
    normalization — all of that now happens once, in ``Preprocessor.compile``.

    ``slug`` is slugified only as a *default* (from the name) when absent; the
    Preprocessor validates the final slug. (Boundary callers still run a lightweight
    ``validate_slug`` for filesystem-path safety before touching the folder.)

    ``previous`` is the target folder's current ``spec.json``, when one exists. Its
    ONLY use is to carry an authored ``cooperation.structures[].pools`` list that the
    incoming payload does not speak (see :func:`_carry_authored_pools`); with
    ``previous=None`` (the default) the result is byte-identical to before FIX-6.
    """
    name = str(raw.get("name") or "").strip()
    slug = _spec_slug(raw)

    agents_raw = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}

    def _role(role: str, legacy_count_key: str) -> dict[str, Any]:
        r = agents_raw.get(role) if isinstance(agents_raw.get(role), dict) else {}
        out: dict[str, Any] = {}
        count = r.get("count", raw.get(legacy_count_key))
        if count is not None:
            out["count"] = count
        pol = r.get("policy")
        if isinstance(pol, dict):
            out["policy"] = deepcopy(pol)
        unit = r.get("orderCountUnit")
        if unit is not None:
            out["orderCountUnit"] = unit
        return out

    role_settings = raw.get("roleSettings")
    if not isinstance(role_settings, dict):
        role_settings = raw.get("roleProfiles")
    if not isinstance(role_settings, dict):
        role_settings = None

    solver_params = raw.get("solverParams") if isinstance(raw.get("solverParams"), dict) else None
    overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else None

    # The value each key takes IF the client supplied it (or if it is derived).
    # Identical to the old dict literal, so with ``previous=None`` the result is
    # byte-identical to before this registry existed.
    from_body: dict[str, Any] = {
        "name": name,
        "slug": slug,
        "domain": str(raw.get("domain") or domain or "container-logistics-sim"),
        "source": "frontend",
        "simulationDays": raw.get("simulationDays", raw.get("simulation_days")),
        "seed": raw.get("seed"),
        "orderCountUnit": raw.get("orderCountUnit"),
        "referenceTime": raw.get("referenceTime"),
        "agents": {
            "truck": _role("truck", "numTrucks"),
            "order": _role("order", "numOrders"),
            "facility": _role("facility", "numFacilities"),
        },
        "earlyOrderCount": raw.get("earlyOrderCount"),
        "orderDemandCurve": deepcopy(raw.get("orderDemandCurve")),
        "tripMatrix": deepcopy(raw.get("tripMatrix")),
        "hauliers": deepcopy(raw.get("hauliers")),
        "cooperation": _carry_authored_pools(
            deepcopy(raw.get("cooperation")),
            previous.get("cooperation") if isinstance(previous, dict) else None,
        ),
        "planner": deepcopy(raw.get("planner")),
        "solver": raw.get("solver"),
        "solverParams": deepcopy(solver_params) if solver_params else None,
        "roleSettings": deepcopy(role_settings) if role_settings else None,
        "overrides": deepcopy(overrides) if overrides else None,
        # Echoed verbatim — never normalised here. ``Preprocessor`` is the single
        # validation surface, and a "helpful" tidy-up in this literal is how the
        # authored and compiled forms drift apart.
        "facilityRules": deepcopy(raw.get("facilityRules")),
        "facilityRulesWorld": deepcopy(raw.get("facilityRulesWorld")),
    }
    # Every declared key must have a value builder — a key added to SPEC_KEYS
    # without one is a loud failure here rather than a silent null in the spec.
    missing = [k for k in SPEC_KEYS if k not in from_body]
    if missing:
        raise RuntimeError(f"assemble_spec: SPEC_KEYS without a value builder: {missing}")

    prev = previous if isinstance(previous, dict) else None
    out: dict[str, Any] = {}
    for key, policy in SPEC_KEYS.items():
        if policy is Carry.CARRY_IF_ABSENT and not _key_supplied(raw, key) and prev and key in prev:
            # The client did not speak this key and the saved spec has it: inherit.
            # PRESENCE, not truthiness — see the Carry docstring.
            out[key] = deepcopy(prev[key])
        else:
            out[key] = from_body[key]
    return out


def normalize_generate_spec(raw: dict[str, Any]) -> dict[str, Any]:
    """LEGACY normalizer — kept ONLY for the deprecated ``ScenarioManager`` regen path
    and ``build_generation_spec_from_meta``. The active generation path uses
    :func:`assemble_spec` + the ``Preprocessor`` (the single validation surface); do
    not reuse this for new code (it duplicates the Preprocessor's validation).
    """
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("Scenario name is required")

    slug = str(raw.get("slug") or slugify(name)).strip()
    slug_err = validate_slug(slug)
    if slug_err:
        raise ValueError(slug_err)

    simulation_days = int(raw.get("simulationDays") or raw.get("simulation_days") or 1)
    simulation_days = max(1, min(MAX_SIMULATION_DAYS, simulation_days))

    agents_raw = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    truck_raw = agents_raw.get("truck") if isinstance(agents_raw.get("truck"), dict) else {}
    order_raw = agents_raw.get("order") if isinstance(agents_raw.get("order"), dict) else {}
    facility_raw = agents_raw.get("facility") if isinstance(agents_raw.get("facility"), dict) else {}

    num_trucks = int(truck_raw.get("count") or raw.get("numTrucks") or MIN_TRUCKS)
    num_trucks = max(MIN_TRUCKS, min(MAX_TRUCKS, num_trucks))

    # Orders are now a PER-DAY rate (orders generated each simulated day). The
    # total over the run is derived as count * simulation_days at the datagen
    # boundary (frontend_scenario_config_override). Stored/displayed value stays
    # per-day so the editor round-trips cleanly.
    num_orders = int(order_raw.get("count") or raw.get("numOrders") or 1)
    num_orders = max(1, min(MAX_TRUCKS * 100, num_orders))
    # Marker so the boundary knows to multiply. Absent marker => legacy scenario
    # whose stored count is a total over the run (no multiply). The current
    # frontend always sends "per_day" explicitly.
    order_count_unit = str(raw.get("orderCountUnit") or "total")
    if order_count_unit not in ("per_day", "total"):
        order_count_unit = "total"

    num_facilities = int(facility_raw.get("count") or raw.get("numFacilities") or DEFAULT_FACILITY_COUNT)
    num_facilities = max(1, min(MAX_FACILITY_COUNT, num_facilities))

    early_raw = raw.get("earlyOrderCount")
    recommended = recommended_early_order_count(num_trucks, num_orders)
    if early_raw is not None:
        early_order_count = max(0, min(num_orders, int(early_raw)))
        # GENERATION_SPEC from older builds stored 600 — that clobbers curve sampling.
        if early_order_count >= LEGACY_EARLY_ORDER_COUNT or early_order_count > num_trucks * 3:
            early_order_count = recommended
    else:
        early_order_count = recommended

    # A trip matrix / demand curve may be supplied as a file reference
    # ({"$file": "/abs/path.csv|.xlsx|.json"}) — parse + inline it here, at the
    # spec-creation step, so spec.json ends up with the value inline (no file ref
    # persisted). Plain inline dicts pass straight through. (CLI/TUI use $file; the
    # dashboard parses client-side and already sends inline.)
    from .scenario_inputs import maybe_parse_spec_file

    order_demand_curve = parse_order_demand_curve(
        maybe_parse_spec_file(raw.get("orderDemandCurve"), "curve")
    )
    trip_matrix = parse_trip_matrix(maybe_parse_spec_file(raw.get("tripMatrix"), "matrix"))
    hauliers = scenario_config.normalize_hauliers(raw.get("hauliers"))
    solver = normalize_solver(raw.get("solver"))
    solver_params = raw.get("solverParams") if isinstance(raw.get("solverParams"), dict) else None
    role_settings = raw.get("roleSettings")
    if not isinstance(role_settings, dict):
        role_settings = raw.get("roleProfiles")
    if not isinstance(role_settings, dict):
        role_settings = None

    # Preserve the per-role policy selection (new schema) so it flows to the
    # Preprocessor. Absent => the role's default policy (today's behavior).
    def _policy(role_raw):
        pol = role_raw.get("policy") if isinstance(role_raw, dict) else None
        return deepcopy(pol) if isinstance(pol, dict) else None

    truck_agent = {"count": num_trucks}
    order_agent = {"count": num_orders, "orderCountUnit": order_count_unit}
    facility_agent = {"count": num_facilities}
    if _policy(truck_raw):
        truck_agent["policy"] = _policy(truck_raw)
    if _policy(order_raw):
        order_agent["policy"] = _policy(order_raw)
    if _policy(facility_raw):
        facility_agent["policy"] = _policy(facility_raw)

    seed = raw.get("seed")
    overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else None

    return {
        "name": name[:80],
        "slug": slug,
        "domain": str(raw.get("domain") or "container-logistics-sim"),
        "simulationDays": simulation_days,
        "source": "frontend",
        "frozen": True,
        "orderCountUnit": order_count_unit,
        "seed": int(seed) if seed is not None else None,
        "agents": {
            "truck": truck_agent,
            "order": order_agent,
            "facility": facility_agent,
        },
        "earlyOrderCount": early_order_count,
        "orderDemandCurve": order_demand_curve,
        "tripMatrix": trip_matrix,
        "hauliers": hauliers,
        "solver": solver,
        "solverParams": deepcopy(solver_params) if solver_params else None,
        "roleSettings": deepcopy(role_settings) if role_settings else None,
        "overrides": deepcopy(overrides) if overrides else None,
        # Carried so the LEGACY path can REFUSE a rules-carrying scenario (R2-2).
        # This normalizer does not implement rules and must not: two implementations
        # of a precedence rule is how they diverge.
        "facilityRules": deepcopy(raw.get("facilityRules")),
        "facilityRulesWorld": deepcopy(raw.get("facilityRulesWorld")),
    }


def _apply_role_settings(settings: dict[str, Any], patch: dict[str, Any] | None) -> None:
    if not patch or not isinstance(patch, dict):
        return
    profile_patch = patch.get("profile")
    if isinstance(profile_patch, dict):
        base = settings.setdefault("profile", {})
        if isinstance(base, dict):
            for key, value in profile_patch.items():
                if value is not None:
                    base[key] = value
    for key, value in patch.items():
        if key == "profile" or value is None:
            continue
        if key in settings:
            settings[key] = value


def build_generation_spec_payload(spec: dict[str, Any]) -> dict[str, Any]:
    """Persisted GENERATION_SPEC block written into orsim_settings.json."""
    return {
        "source": "frontend",
        "frozen": True,
        "name": spec["name"],
        "slug": spec["slug"],
        "simulationDays": spec["simulationDays"],
        "seed": spec.get("seed"),
        "orderCountUnit": spec.get("orderCountUnit", "total"),
        "agents": deepcopy(spec["agents"]),
        "earlyOrderCount": spec["earlyOrderCount"],
        "orderDemandCurve": deepcopy(spec.get("orderDemandCurve")),
        "tripMatrix": deepcopy(spec.get("tripMatrix")),
        "hauliers": deepcopy(spec.get("hauliers")),
        "solver": spec.get("solver") or DEFAULT_SOLVER,
        "solverParams": deepcopy(spec.get("solverParams")),
        "roleSettings": deepcopy(spec.get("roleSettings")),
        "overrides": deepcopy(spec.get("overrides")),
        "behaviorRevision": scenario_config.BEHAVIOR_REVISION,
    }


def build_generation_spec_from_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Rebuild GENERATION_SPEC from scenario_meta.json when orsim_settings lacks it."""
    if not isinstance(meta, dict) or meta.get("source") != "frontend":
        return None
    agents = meta.get("agents")
    if not isinstance(agents, dict):
        return None
    role_settings = meta.get("roleSettings") if isinstance(meta.get("roleSettings"), dict) else None
    early_from_role = None
    if isinstance(role_settings, dict):
        order_settings = role_settings.get("order")
        if isinstance(order_settings, dict) and order_settings.get("early_order_count") is not None:
            early_from_role = order_settings.get("early_order_count")
    normalized = normalize_generate_spec(
        {
            "name": meta.get("name") or meta.get("slug") or "scenario",
            "slug": meta.get("slug") or slugify(str(meta.get("name") or "scenario")),
            "domain": meta.get("domain") or "container-logistics-sim",
            "simulationDays": meta.get("simulationDays") or 1,
            "orderCountUnit": meta.get("orderCountUnit"),
            "agents": agents,
            "earlyOrderCount": meta.get("earlyOrderCount", early_from_role),
            "orderDemandCurve": meta.get("orderDemandCurve"),
            "tripMatrix": meta.get("tripMatrix"),
            "hauliers": meta.get("hauliers"),
            "solver": meta.get("solver"),
            "solverParams": meta.get("solverParams"),
            "roleSettings": role_settings,
        }
    )
    return build_generation_spec_payload(normalized)


@contextmanager
def frontend_scenario_config_override(spec: dict[str, Any]):
    """Patch scenario_config module constants for one frontend generation run."""
    import apps.container_logistics.scenario.scenario_config as cfg

    backup = {
        "FACILITY_RULES": deepcopy(getattr(cfg, "FACILITY_RULES", None)),
        "SIMULATION_DAYS": cfg.SIMULATION_DAYS,
        "HAULIERS": deepcopy(cfg.HAULIERS),
        "truck_settings": deepcopy(cfg.truck_settings),
        "order_settings": deepcopy(cfg.order_settings),
        "facility_settings": deepcopy(cfg.facility_settings),
        "assignment_settings": deepcopy(cfg.assignment_settings),
        "analytics_settings": deepcopy(cfg.analytics_settings),
    }
    # Carried onto the module so the legacy generation path can SEE a rule list and
    # refuse it. Never consumed as configuration — build_generation_spec raises.
    cfg.FACILITY_RULES = deepcopy(spec.get("facilityRules")) or []
    cfg.SIMULATION_DAYS = spec["simulationDays"]
    cfg.HAULIERS = cfg.normalize_hauliers(spec.get("hauliers"))
    cfg.truck_settings["num_trucks"] = spec["agents"]["truck"]["count"]
    # Orders are stored per-day; the datagen contract (spec.num_orders) is the
    # TOTAL over the run. Multiply here (the single boundary) when the spec is
    # per-day. Legacy specs without the marker keep their stored total.
    order_count = int(spec["agents"]["order"]["count"])
    if spec.get("orderCountUnit") == "per_day":
        order_count *= int(spec["simulationDays"])
    cfg.order_settings["num_orders"] = order_count
    cfg.order_settings["early_order_count"] = spec["earlyOrderCount"]
    curve = spec.get("orderDemandCurve")
    cfg.order_settings["order_demand_curve"] = deepcopy(curve) if curve else None
    cfg.order_settings["order_demand_weights"] = (
        hourly_weights_from_spec(curve) if curve else None
    )
    trip_matrix = spec.get("tripMatrix")
    cfg.order_settings["trip_matrix"] = deepcopy(trip_matrix) if trip_matrix else None
    facility_n = int(spec["agents"]["facility"]["count"])
    facility_n = max(1, min(MAX_FACILITY_COUNT, facility_n))
    # Demand-proportional facility mix follows this scenario's own trip matrix.
    sites = scenario_config.facilities_for_count(
        facility_n, trip_matrix=cfg.order_settings.get("trip_matrix")
    )
    cfg.facility_settings["num_facilities"] = facility_n

    # Assignment solver selection flows into the assignment behavior via the
    # profile ``strategy``/``solver_params`` keys (GenerateBehavior dumps the
    # whole assignment profile). Coerce to a known solver so a stale/garbage
    # value can't pick a non-existent strategy.
    assign_profile = cfg.assignment_settings.setdefault("profile", {})
    if isinstance(assign_profile, dict):
        assign_profile["strategy"] = normalize_solver(spec.get("solver"))
        solver_params = spec.get("solverParams")
        if isinstance(solver_params, dict):
            base_params = dict(assign_profile.get("solver_params") or {})
            base_params.update(solver_params)
            assign_profile["solver_params"] = base_params

    role_settings = spec.get("roleSettings")
    if isinstance(role_settings, dict):
        _apply_role_settings(cfg.truck_settings, role_settings.get("truck"))
        _apply_role_settings(cfg.order_settings, role_settings.get("order"))
        _apply_role_settings(cfg.facility_settings, role_settings.get("facility"))

    # Always use freshly-computed facility sites (never let stale roleSettings
    # from a previous generation override the current land-safe coordinates).
    # facilities_for_count emits placeholder per-site service_time / gate_count;
    # stamp the scenario's configured profile values onto every fresh site so an
    # edited service_time actually takes effect. Otherwise the per-site value
    # shadows the profile default in GenerateBehavior._facility_service_time and
    # the edit is silently ignored.
    profile = cfg.facility_settings.setdefault("profile", {})
    if isinstance(profile, dict):
        configured_service_time = profile.get("service_time")
        configured_gate_count = profile.get("gate_count")
        for site in sites:
            if configured_service_time is not None:
                site["service_time"] = configured_service_time
            if configured_gate_count is not None:
                site["gate_count"] = configured_gate_count
        profile["facilities"] = sites
    try:
        yield
    finally:
        cfg.FACILITY_RULES = backup["FACILITY_RULES"]
        cfg.SIMULATION_DAYS = backup["SIMULATION_DAYS"]
        cfg.HAULIERS = backup["HAULIERS"]
        cfg.truck_settings = backup["truck_settings"]
        cfg.order_settings = backup["order_settings"]
        cfg.facility_settings = backup["facility_settings"]
        cfg.assignment_settings = backup["assignment_settings"]
        cfg.analytics_settings = backup["analytics_settings"]


def scenario_list_entry(scenario_path: str, slug: str) -> dict[str, Any]:
    meta = scenario_meta_view(scenario_path)
    complete = scenario_complete(scenario_path)
    created_at = None
    if meta and meta.get("createdAt"):
        created_at = meta["createdAt"]
    elif complete:
        from .scenario_bundle import BUNDLE_FILENAME, bundle_exists

        if bundle_exists(scenario_path):
            mtime_file = BUNDLE_FILENAME
        elif meta:
            mtime_file = META_FILENAME
        else:
            mtime_file = BEHAVIOR_FILES[0]
        try:
            created_at = datetime.fromtimestamp(
                os.path.getmtime(os.path.join(scenario_path, mtime_file)),
                tz=timezone.utc,
            ).isoformat()
        except OSError:
            created_at = None

    agents_meta = meta.get("agents") if meta and isinstance(meta.get("agents"), dict) else None
    generation_spec = _load_generation_spec_from_disk(scenario_path) if complete else None
    counts = _resolved_scenario_counts(
        scenario_path,
        meta_agents=agents_meta,
        generation_spec=generation_spec,
    )

    if meta:
        return {
            "slug": meta.get("slug") or slug,
            "name": meta.get("name") or slug,
            "createdAt": created_at,
            "simulationDays": meta.get("simulationDays") or counts["simulation_days"],
            "agents": {
                "truck": counts["trucks"],
                "order": counts["orders"],
                "facility": counts["facilities"],
            },
            "source": meta.get("source") or "unknown",
            "status": "complete" if complete else "corrupt",
        }

    return {
        "slug": slug,
        "name": slug,
        "createdAt": created_at,
        "simulationDays": counts["simulation_days"],
        "agents": {
            "truck": counts["trucks"],
            "order": counts["orders"],
            "facility": counts["facilities"],
        },
        "source": "builtin",
        "status": "complete" if complete else "corrupt",
    }


def list_scenarios(datahub_dir: str, domain: str) -> list[dict[str, Any]]:
    """List scenario metadata via the derived browse-index (Layer B roll-up).

    Hot path: reads only ``scenarios/_index.json`` (KB) + one cheap ``os.stat`` per
    scenario to verify freshness — never parses the multi-MB ``scenario.json``. A
    drifted/missing entry is lazily rebuilt (parses that one bundle, then cached).
    Legacy / ridehail folders (no bundle) fall back to the direct reader.
    """
    from . import scenario_index as idx

    root = scenario_root(datahub_dir, domain)
    if not os.path.isdir(root):
        return []
    rollup = idx.read_rollup(root)
    entries: list[dict[str, Any]] = []
    for name in sorted(os.listdir(root)):
        if name.endswith(".tmp") or name.startswith(".") or name == idx.ROLLUP_FILENAME:
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        cached = rollup.get(name)
        if isinstance(cached, dict) and idx.is_fresh(cached, path):
            entries.append(idx.strip_internal(cached))
            continue
        detail = idx.refresh_scenario(path, name, root)  # None for legacy (no bundle)
        if detail is not None:
            entries.append(idx.strip_internal(idx.list_fields(detail)))
        else:
            entries.append(scenario_list_entry(path, name))

    # Overlay lifecycle state from the registry (draft/ready/stale/error) — cheap
    # os.stat per folder, so the dashboard can badge each scenario.
    try:
        from .scenario_registry import derive_state

        for e in entries:
            st = derive_state(os.path.join(root, e.get("slug", "")))
            if st is not None:
                e["state"] = st
    except Exception:
        pass
    return entries


def sample_agents(collection: dict[str, Any] | None, limit: int = PREVIEW_SAMPLE_LIMIT) -> list[dict[str, Any]]:
    if not collection:
        return []
    rows: list[dict[str, Any]] = []
    for agent_id in sorted(collection.keys())[:limit]:
        behavior = collection[agent_id]
        profile = behavior.get("profile") if isinstance(behavior.get("profile"), dict) else {}
        rows.append(
            {
                "id": agent_id,
                "role": behavior.get("persona", {}).get("role") if isinstance(behavior.get("persona"), dict) else None,
                "profileSummary": {
                    k: profile[k]
                    for k in list(profile.keys())[:6]
                },
            }
        )
    return rows


def _load_orsim_settings_from_disk(scenario_path: str) -> dict[str, Any] | None:
    from .scenario_bundle import read_bundle

    bundle = read_bundle(scenario_path)
    if bundle and isinstance(bundle.get("settings"), dict):
        return bundle["settings"]
    settings_path = os.path.join(scenario_path, "orsim_settings.json")
    if not os.path.isfile(settings_path):
        return None
    try:
        with open(settings_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


# role -> legacy behavior filename, for counting realized agents.
_ROLE_BEHAVIOR_FILE = {
    "truck": "truck_behavior.json",
    "order": "order_behavior.json",
    "facility": "facility_behavior.json",
}


def _count_behavior_agents(scenario_path: str, filename: str) -> int | None:
    # Bundle short-circuit: never parse the big agents blob just to count — use the
    # manifest header counts, then the inline collection length.
    from .scenario_bundle import read_bundle

    bundle = read_bundle(scenario_path)
    if bundle:
        role = next((r for r, fn in _ROLE_BEHAVIOR_FILE.items() if fn == filename), None)
        if role:
            counts = bundle.get("counts") if isinstance(bundle.get("counts"), dict) else {}
            if isinstance(counts.get(role), int):
                return counts[role]
            agents = bundle.get("agents") if isinstance(bundle.get("agents"), dict) else {}
            coll = agents.get(role)
            return len(coll) if isinstance(coll, dict) else None
    path = os.path.join(scenario_path, filename)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return len(data) if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _resolved_scenario_counts(
    scenario_path: str,
    *,
    meta_agents: dict[str, Any] | None = None,
    generation_spec: dict[str, Any] | None = None,
) -> dict[str, int | None]:
    """Truck/order/facility counts and simulation days from meta, GENERATION_SPEC, or behavior files."""
    trucks: int | None = None
    orders: int | None = None
    facilities: int | None = None
    simulation_days: int | None = None
    # "per_day" => stored order count is already a daily rate; "total"/None =>
    # legacy count over the whole run, converted to per-day for display below.
    order_unit: str | None = None
    if isinstance(generation_spec, dict) and generation_spec.get("orderCountUnit"):
        order_unit = str(generation_spec["orderCountUnit"])

    if isinstance(meta_agents, dict):
        truck_meta = meta_agents.get("truck")
        if isinstance(truck_meta, dict):
            trucks = truck_meta.get("count")
        elif isinstance(truck_meta, int):
            trucks = truck_meta
        order_meta = meta_agents.get("order")
        if isinstance(order_meta, dict):
            orders = order_meta.get("count")
        elif isinstance(order_meta, int):
            orders = order_meta
        facility_meta = meta_agents.get("facility")
        if isinstance(facility_meta, dict):
            facilities = facility_meta.get("count")
        elif isinstance(facility_meta, int):
            facilities = facility_meta

    if isinstance(generation_spec, dict):
        if simulation_days is None and generation_spec.get("simulationDays") is not None:
            simulation_days = int(generation_spec["simulationDays"])
        g_agents = generation_spec.get("agents")
        if isinstance(g_agents, dict):
            g_truck = g_agents.get("truck")
            if trucks is None and isinstance(g_truck, dict):
                trucks = g_truck.get("count")
            g_order = g_agents.get("order")
            if orders is None and isinstance(g_order, dict):
                orders = g_order.get("count")
            g_facility = g_agents.get("facility")
            if facilities is None and isinstance(g_facility, dict):
                facilities = g_facility.get("count")

    settings = _load_orsim_settings_from_disk(scenario_path)
    if settings:
        if simulation_days is None and settings.get("SIMULATION_DAYS") is not None:
            simulation_days = int(settings["SIMULATION_DAYS"])
        if simulation_days is None:
            steps = settings.get("SIMULATION_LENGTH_IN_STEPS")
            interval = settings.get("STEP_INTERVAL", scenario_config.STEP_INTERVAL_SECONDS)
            try:
                total_seconds = int(steps) * int(interval)
                if total_seconds > 0:
                    simulation_days = max(1, round(total_seconds / 86400))
            except (TypeError, ValueError):
                pass
        if generation_spec is None:
            spec = settings.get("GENERATION_SPEC")
            if isinstance(spec, dict):
                if order_unit is None and spec.get("orderCountUnit"):
                    order_unit = str(spec["orderCountUnit"])
                if simulation_days is None and spec.get("simulationDays") is not None:
                    simulation_days = int(spec["simulationDays"])
                g_agents = spec.get("agents")
                if isinstance(g_agents, dict):
                    if trucks is None and isinstance(g_agents.get("truck"), dict):
                        trucks = g_agents["truck"].get("count")
                    if orders is None and isinstance(g_agents.get("order"), dict):
                        orders = g_agents["order"].get("count")
                    if facilities is None and isinstance(g_agents.get("facility"), dict):
                        facilities = g_agents["facility"].get("count")

    if trucks is None:
        trucks = _count_behavior_agents(scenario_path, "truck_behavior.json")
    if orders is None:
        orders = _count_behavior_agents(scenario_path, "order_behavior.json")
    if facilities is None:
        facilities = _count_behavior_agents(scenario_path, "facility_behavior.json")

    # Normalize order count to a PER-DAY rate for display. New scenarios store
    # per-day already; legacy scenarios (and builtins) store the run total, so
    # divide by the simulated day count.
    if orders is not None and order_unit != "per_day" and simulation_days and simulation_days > 0:
        orders = max(1, round(int(orders) / simulation_days))

    return {
        "trucks": int(trucks) if trucks is not None else None,
        "orders": int(orders) if orders is not None else None,
        "facilities": int(facilities) if facilities is not None else None,
        "simulation_days": int(simulation_days) if simulation_days is not None else None,
    }


def _load_generation_spec_from_disk(scenario_path: str) -> dict[str, Any] | None:
    from .scenario_bundle import read_bundle

    # Prefer the editable source spec.json (tiny, the canonical recipe), then the
    # recipe mirrored inside scenario.json, then legacy meta.
    spec = read_spec(scenario_path)
    if isinstance(spec, dict):
        return spec
    bundle = read_bundle(scenario_path)
    if bundle and isinstance(bundle.get("recipe"), dict):
        return bundle["recipe"]
    settings = _load_orsim_settings_from_disk(scenario_path)
    if settings:
        spec = settings.get("GENERATION_SPEC")
        if isinstance(spec, dict):
            return spec
    return build_generation_spec_from_meta(scenario_meta_view(scenario_path))


def _profile_from_first_agent(scenario_path: str, filename: str, fallback_id: str) -> dict[str, Any] | None:
    from .scenario_bundle import read_bundle

    data: dict | None = None
    bundle = read_bundle(scenario_path)
    if bundle:
        role = next((r for r, fn in _ROLE_BEHAVIOR_FILE.items() if fn == filename), None)
        agents = bundle.get("agents") if isinstance(bundle.get("agents"), dict) else {}
        coll = agents.get(role) if role else None
        data = coll if isinstance(coll, dict) else None
    if data is None:
        path = os.path.join(scenario_path, filename)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            return None
    if not isinstance(data, dict) or not data:
        return None
    behavior = data.get(fallback_id) if fallback_id in data else next(iter(data.values()))
    if not isinstance(behavior, dict):
        return None
    profile = behavior.get("profile")
    return profile if isinstance(profile, dict) else None


def _role_settings_from_disk(scenario_path: str, generation_spec: dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(generation_spec, dict):
        if isinstance(generation_spec.get("roleSettings"), dict):
            return generation_spec["roleSettings"]
        legacy = generation_spec.get("roleProfiles")
        if isinstance(legacy, dict):
            return {
                role: {"profile": prof}
                for role, prof in legacy.items()
                if role in ("truck", "order", "facility") and isinstance(prof, dict)
            }

    extracted: dict[str, Any] = {}
    mapping = [
        ("truck", "truck_behavior.json", "truck_000000"),
        ("order", "order_behavior.json", "order_000000"),
        ("facility", "facility_behavior.json", "facility_000"),
    ]
    for role, fname, agent_id in mapping:
        profile = _profile_from_first_agent(scenario_path, fname, agent_id)
        if not profile:
            continue
        if role == "truck" and isinstance(profile.get("restricted_areas"), list):
            profile = {**profile, "restricted_areas": profile["restricted_areas"]}
        payload: dict[str, Any] = {"profile": profile}
        if role == "order":
            payload["business_hour_start"] = scenario_config.order_settings.get("business_hour_start", 0)
            payload["business_hour_end"] = scenario_config.order_settings.get("business_hour_end", 24)
            payload["early_order_count"] = scenario_config.order_settings.get(
                "early_order_count", scenario_config.EARLY_ORDER_COUNT
            )
        extracted[role] = payload
    return extracted or None


_DEFAULT_POLICY = {"truck": "default", "order": "matrix", "facility": "allocate"}


def _policies_from_spec(generation_spec: dict[str, Any] | None) -> dict[str, str]:
    """Per-role policy type from the recipe (falls back to each role's default)."""
    out = dict(_DEFAULT_POLICY)
    if isinstance(generation_spec, dict):
        agents = generation_spec.get("agents")
        if isinstance(agents, dict):
            for role in ("truck", "order", "facility"):
                a = agents.get(role)
                pol = a.get("policy") if isinstance(a, dict) else None
                if isinstance(pol, dict) and pol.get("type"):
                    out[role] = str(pol["type"])
    return out


def _cooperation_from_spec(generation_spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """Authored cooperation block for the edit form: ``{active, structures:[{id, edges}]}``.

    The persisted spec.json/recipe already stores the authored form; strip the
    DERIVED fields (``adjacency``/``components``) defensively and never invent a
    block when the spec predates the feature (None ⇒ the form shows the default
    no-coop-only state).

    ``pools`` is carried through, but ONLY for the structures whose author wrote
    it — mirroring the recipe, which echoes ``pools`` back only for those
    (``datagen/preprocess.py``). Handing the editor edges alone would lose the
    pool shape: a 3-member pool hydrates as a 3-edge clique and posts back as
    three 2-member pools, a different market (FIX-6 / review finding F2). An
    edges-authored structure gains no ``pools`` key, so its hydration is
    byte-identical to before.
    """
    if not isinstance(generation_spec, dict):
        return None
    coop = generation_spec.get("cooperation")
    if not isinstance(coop, dict):
        return None
    structures = []
    for s in coop.get("structures") or []:
        if not isinstance(s, dict):
            continue
        edges = [
            [str(e[0]), str(e[1])]
            for e in (s.get("edges") or [])
            if isinstance(e, (list, tuple)) and len(e) == 2
        ]
        entry: dict[str, Any] = {"id": str(s.get("id") or ""), "edges": edges}
        pools = _authored_pools(s)
        if pools is not None:
            entry["pools"] = pools
        structures.append(entry)
    if not structures:
        return None
    return {"active": coop.get("active"), "structures": structures}


def scenario_edit_form_from_detail(
    entry: dict[str, Any],
    generation_spec: dict[str, Any] | None,
    *,
    scenario_path: str | None = None,
) -> dict[str, Any]:
    """Fields used to hydrate the Scenario tab editor."""
    agents = entry.get("agents") if isinstance(entry.get("agents"), dict) else {}
    # NB: entry["agents"] are already per-day-normalized display counts (computed
    # by scenario_list_entry); don't feed them back in as meta_agents or the
    # order count gets divided by the horizon twice. Re-resolve from the raw
    # generation_spec / on-disk behaviors instead.
    counts = _resolved_scenario_counts(
        scenario_path or "",
        meta_agents=None,
        generation_spec=generation_spec,
    ) if scenario_path else {
        "trucks": agents.get("truck") if isinstance(agents.get("truck"), int) else None,
        "orders": agents.get("order") if isinstance(agents.get("order"), int) else None,
        "facilities": agents.get("facility") if isinstance(agents.get("facility"), int) else None,
        "simulation_days": entry.get("simulationDays"),
    }

    num_trucks = counts["trucks"]
    simulation_days = counts["simulation_days"] or entry.get("simulationDays")

    curve_raw = None
    if isinstance(generation_spec, dict) and generation_spec.get("orderDemandCurve"):
        curve_raw = generation_spec.get("orderDemandCurve")
    order_demand_curve = parse_order_demand_curve(curve_raw) if curve_raw else parse_order_demand_curve(None)

    matrix_raw = generation_spec.get("tripMatrix") if isinstance(generation_spec, dict) else None
    trip_matrix = parse_trip_matrix(matrix_raw)

    num_orders_for_early = int(counts["orders"] or 1)
    early_order_count = recommended_early_order_count(
        int(num_trucks or MIN_TRUCKS), num_orders_for_early
    )
    if isinstance(generation_spec, dict):
        if generation_spec.get("earlyOrderCount") is not None:
            raw_early = int(generation_spec["earlyOrderCount"])
            if raw_early >= LEGACY_EARLY_ORDER_COUNT or raw_early > int(num_trucks or 1) * 3:
                early_order_count = recommended_early_order_count(
                    int(num_trucks or MIN_TRUCKS), num_orders_for_early
                )
            else:
                early_order_count = max(0, min(num_orders_for_early, raw_early))

    role_settings = (
        _role_settings_from_disk(scenario_path, generation_spec) if scenario_path else None
    )
    if role_settings and isinstance(role_settings.get("order"), dict):
        order_patch = role_settings["order"]
        if order_patch.get("early_order_count") is not None:
            early_order_count = int(order_patch["early_order_count"])

    form: dict[str, Any] = {
        "name": entry.get("name") or entry.get("slug") or "",
        "slug": entry.get("slug") or "",
        "simulationDays": int(simulation_days or 1),
        "numTrucks": int(num_trucks or MIN_TRUCKS),
        "numOrders": int(counts["orders"] or 1),
        "numFacilities": int(counts["facilities"] or DEFAULT_FACILITY_COUNT),
        "orderDemandCurve": order_demand_curve,
        "tripMatrix": trip_matrix,
        "hauliers": scenario_config.normalize_hauliers(
            generation_spec.get("hauliers") if isinstance(generation_spec, dict) else None
        ),
        "solver": normalize_solver(
            generation_spec.get("solver") if isinstance(generation_spec, dict) else None
        ),
        "solverParams": (
            generation_spec.get("solverParams")
            if isinstance(generation_spec, dict) and isinstance(generation_spec.get("solverParams"), dict)
            else None
        ),
        "seed": (
            generation_spec.get("seed") if isinstance(generation_spec, dict) else None
        ),
        "policies": _policies_from_spec(generation_spec),
        # Cooperation structures (authored form: active + edges-only — never the
        # derived adjacency/components) so editing a collab scenario round-trips
        # its structures instead of silently dropping them (plan
        # docs/scenario_builder_cooperation_ui_plan.md §3).
        "cooperation": _cooperation_from_spec(generation_spec),
    }
    if role_settings:
        form["roleSettings"] = role_settings
        if isinstance(role_settings.get("order"), dict):
            form["roleSettings"]["order"] = {
                **role_settings["order"],
                "early_order_count": early_order_count,
            }
    return form


def get_scenario_detail(
    datahub_dir: str,
    domain: str,
    slug: str,
    *,
    preview_limit: int = PREVIEW_SAMPLE_LIMIT,
) -> dict[str, Any]:
    slug_err = validate_slug(slug)
    if slug_err:
        raise ValueError(slug_err)
    path = scenario_dir(datahub_dir, domain, slug)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Scenario not found: {slug}")

    # Fast path: serve from the per-scenario derived index (Layer A) when fresh,
    # rebuilding it once on drift. Never parses the big agents blob for a bundle
    # scenario. Legacy / ridehail folders (no bundle) fall through to the readers.
    from .scenario_bundle import bundle_exists

    if bundle_exists(path):
        from . import scenario_index as idx

        cached = idx.read_index(path)
        det = cached if (isinstance(cached, dict) and idx.is_fresh(cached, path)) else None
        if det is None:
            det = idx.refresh_scenario(path, slug, scenario_root(datahub_dir, domain))
        if det is not None:
            entry = det["entry"]
            preview = det.get("preview") if isinstance(det.get("preview"), dict) else {}
            return {
                **entry,
                "preview": {
                    "truck": (preview.get("truck") or [])[:preview_limit],
                    "order": (preview.get("order") or [])[: min(10, preview_limit)],
                },
                "editForm": det.get("editForm") or {},
            }

    entry = scenario_list_entry(path, slug)
    generation_spec = _load_generation_spec_from_disk(path)
    detail: dict[str, Any] = {
        **entry,
        "preview": {},
        "editForm": scenario_edit_form_from_detail(
            entry, generation_spec, scenario_path=path
        ),
    }

    if not scenario_complete(path):
        return detail

    from .scenario_bundle import read_bundle

    bundle = read_bundle(path)
    if bundle:
        # Bundle scenarios carry a precomputed preview in the manifest header — serve
        # it directly, never parsing the (potentially large) inline agents blob here.
        preview = bundle.get("preview") if isinstance(bundle.get("preview"), dict) else {}
        detail["preview"] = {
            "truck": (preview.get("truck") or [])[:preview_limit],
            "order": (preview.get("order") or [])[: min(10, preview_limit)],
        }
        counts = bundle.get("counts") if isinstance(bundle.get("counts"), dict) else {}
        if entry["agents"]["truck"] is None and isinstance(counts.get("truck"), int):
            detail["agents"]["truck"] = counts["truck"]
        if entry["agents"]["order"] is None and isinstance(counts.get("order"), int):
            detail["agents"]["order"] = counts["order"]
        return detail

    try:
        with open(os.path.join(path, "truck_behavior.json"), "r", encoding="utf-8") as fp:
            trucks = json.load(fp)
        with open(os.path.join(path, "order_behavior.json"), "r", encoding="utf-8") as fp:
            orders = json.load(fp)
        detail["preview"] = {
            "truck": sample_agents(trucks, preview_limit),
            "order": sample_agents(orders, min(10, preview_limit)),
        }
        if entry["agents"]["truck"] is None and isinstance(trucks, dict):
            detail["agents"]["truck"] = len(trucks)
        if entry["agents"]["order"] is None and isinstance(orders, dict):
            detail["agents"]["order"] = len(orders)
    except (OSError, json.JSONDecodeError):
        detail["status"] = "corrupt"

    return detail


def _stage_external_sources(payload: dict[str, Any], tmp_dir: str) -> None:
    """Make a spec folder-portable: copy any *absolute* data-file the order policy
    references (a ``historical`` policy's ``source``) into the folder's ``inputs/`` and
    rewrite the ref to a folder-relative ``inputs/<name>`` path.

    Matrix/curve refs need no staging — the Preprocessor resolves and **inlines** them
    into the recipe. Only ``historical`` keeps a live file ref (it re-learns from the
    records on every recompile), so without this an absolute path baked into ``spec.json``
    would break the moment the scenario folder is moved/shared. Missing/relative paths
    are left untouched (a missing file is reported by the Preprocessor — the one validator).
    """
    agents = payload.get("agents") if isinstance(payload.get("agents"), dict) else {}
    order = agents.get("order") if isinstance(agents.get("order"), dict) else {}
    pol = order.get("policy") if isinstance(order.get("policy"), dict) else None
    if not pol:
        return
    src = pol.get("source")
    path = src.get("$file") if isinstance(src, dict) else src
    if not isinstance(path, str) or not path.strip():
        return
    path = path.strip()
    if not os.path.isabs(path) or not os.path.isfile(path):
        return  # already relative/portable, or missing (Preprocessor will report it)
    inputs_dir = os.path.join(tmp_dir, "inputs")
    os.makedirs(inputs_dir, exist_ok=True)
    dst_name = os.path.basename(path)
    shutil.copy2(path, os.path.join(inputs_dir, dst_name))
    pol["source"] = os.path.join("inputs", dst_name)  # folder-relative → portable


def _compile_into_folder(
    datahub_dir: str,
    domain: str,
    slug: str,
    *,
    spec: dict[str, Any],
    carry_sources: bool,
) -> dict[str, Any]:
    """The preprocess step: spec (+ folder sources) -> scenario.json, atomically.

    Writes ``spec.json`` and runs the shared datagen (honoring a ``scenario_gen.py``
    override + ``inputs/`` that live in the scenario folder) into a ``<slug>.tmp`` dir,
    then atomically renames it over the target and refreshes the browse-index. The
    canonical recipe payload is mirrored into ``scenario.json`` for run self-containment.
    """
    from . import scenario_index as idx

    # ``spec`` is the assembled canonical payload (no validation applied). The
    # Preprocessor inside ``compile_spec_to_bundle`` is the SINGLE validation surface
    # — it validates/clamps/resolves and returns the frozen, self-contained recipe,
    # which we then persist as ``spec.json``.
    payload = spec
    target = scenario_dir(datahub_dir, domain, slug)
    tmp_slug = f"{slug}.tmp"
    tmp = scenario_dir(datahub_dir, domain, tmp_slug)
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    # Carry the authored sources (override module + raw uploads) from the existing
    # folder so a recompile sees them (and can resolve relative ``$file`` inputs).
    if carry_sources and os.path.isdir(target):
        gen_py = os.path.join(target, "scenario_gen.py")
        if os.path.isfile(gen_py):
            shutil.copy2(gen_py, os.path.join(tmp, "scenario_gen.py"))
        inputs = os.path.join(target, "inputs")
        if os.path.isdir(inputs):
            shutil.copytree(inputs, os.path.join(tmp, "inputs"))

    # Auto-stage any absolute external data file (historical order source) into
    # the folder's inputs/ and rewrite the ref to a relative path, so spec.json is
    # self-contained and recompiles survive a folder move (plan §9 portability).
    _stage_external_sources(payload, tmp)

    try:
        # Compile via the Preprocessor + policy engine (the sole validator) — no
        # scenario_config global mutation, no scenario_datagen relay. Schema-tolerant.
        from .spec_compile import compile_spec_to_bundle

        bundle = compile_spec_to_bundle(
            payload,
            domain=domain,
            scenario_dir=tmp,
            slug=slug,
            name=payload.get("name") or slug,
            source=payload.get("source") or "spec",
        )

        # Persist the RESOLVED, validated recipe as the editable ``spec.json`` so the
        # source is self-contained (matrix/curve inlined) and round-trips cleanly
        # through the Preprocessor on the next recompile.
        recipe = bundle.get("recipe") if isinstance(bundle, dict) else None
        write_spec(tmp, recipe if isinstance(recipe, dict) else payload)

        if not bundle_complete(tmp):
            raise RuntimeError("Scenario generation incomplete")

        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        os.rename(tmp, target)

        detail = idx.refresh_scenario(target, slug, scenario_root(datahub_dir, domain))
        # Register the scenario as compiled/ready in the registry (local + Mongo).
        try:
            from .scenario_registry import ScenarioRegistry
            from .scenario_bundle import read_bundle

            b = read_bundle(target) or {}
            ScenarioRegistry(scenario_root(datahub_dir, domain)).mark_compiled(
                slug,
                counts=b.get("counts") if isinstance(b.get("counts"), dict) else None,
            )
        except Exception:
            pass
        if detail is not None:
            return idx.strip_internal(idx.list_fields(detail))
        return scenario_list_entry(target, slug)
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            from .scenario_registry import ScenarioRegistry

            ScenarioRegistry(scenario_root(datahub_dir, domain)).mark_error(slug, str(exc))
        except Exception:
            pass
        raise


class CooperationResetRequired(ValueError):
    """A save would EMPTY a structure's cooperation content without saying so.

    A distinct type (not a bare ValueError) so the control plane can hand the UI a
    machine-readable code and offer "clear it on purpose" — an escape hatch nothing
    can reach is not an escape hatch. Subclasses ValueError so every existing
    ``except ValueError`` path keeps treating it as invalid input.
    """


def _previous_exists_but_is_unreadable(scenario_path: str, previous: Any) -> bool:
    """A saved spec is PRESENT on disk but could not be parsed.

    Review finding 12: the loader swallows ``OSError``/``JSONDecodeError`` and
    returns ``None``, which is indistinguishable from "brand-new scenario". The
    destructive-save guard keys off ``previous``, so it silently **fails open** —
    it protects least the folder most in need of protection (a corrupted one).
    """
    if previous is not None:
        return False
    if not os.path.isdir(scenario_path):
        return False  # genuinely new: nothing to protect
    for name in ("spec.json", "scenario.json"):
        candidate = os.path.join(scenario_path, name)
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return True
    return False


def _guard_cooperation_not_emptied(
    previous: dict[str, Any] | None,
    incoming_cooperation: Any,
    slug: str,
    *,
    allow_reset: bool,
) -> None:
    """Refuse a save that empties a structure's cooperation content (FIX-6, layer 2).

    ``generate_scenario`` rebuilds the whole recipe from the request body and then
    ``rmtree``s the folder, so **any field the client does not know about is
    deleted**. That is how a pools-authored consortium became an ``edges: []``
    no-op that still reported itself active (review finding F2). The guard is
    deliberately about the *class*, not about pools: if the saved ``spec.json``
    has content (edges or pools) for a structure and the incoming payload has
    none for it — emptied, dropped, renamed, or the whole ``cooperation`` key
    missing — the save is refused instead of applied.

    Intentional resets stay possible via ``allow_reset``; a brand-new scenario
    has no previous spec and can never trip it.
    """
    if allow_reset:
        return
    lost = sorted(
        cooperation_content_structure_ids(previous.get("cooperation") if previous else None)
        - cooperation_content_structure_ids(incoming_cooperation)
    )
    if not lost:
        return
    names = ", ".join(repr(s) for s in lost)
    raise CooperationResetRequired(
        f"Refusing to empty cooperation on scenario {slug!r}: structure(s) {names} carry "
        "edges/pools in the saved spec.json, but the incoming payload carries neither for "
        "them. A client that does not understand a cooperation field would silently delete "
        "it here (see FIX-6 / finding F2). Send the structure's content back, or pass "
        "allow_cooperation_reset=True to clear it on purpose."
    )


def _previous_scoped_to_untouched_structures(
    previous: dict[str, Any] | None, incoming_cooperation: Any
) -> dict[str, Any] | None:
    """``previous`` with the structures being RESET removed from its cooperation.

    Review finding 11: an explicit ``allow_cooperation_reset`` used to stand the
    layer-1 pools carry down **globally** (``previous=None``), so structures the
    user never touched lost their authored pools too — an n-member pool silently
    reshaped into pairwise pools. The escape hatch re-created the original bug
    inside itself.

    Scope it: only the structures whose content the incoming body actually empties
    stand down; every other structure keeps its carry. Reset one, keep the rest.
    """
    if not isinstance(previous, dict):
        return None
    being_reset = (
        cooperation_content_structure_ids(previous.get("cooperation"))
        - cooperation_content_structure_ids(incoming_cooperation)
    )
    if not being_reset:
        return previous
    scoped = deepcopy(previous)
    coop = scoped.get("cooperation")
    structures = _cooperation_structures(coop)
    if isinstance(coop, dict) and isinstance(structures, list):
        coop["structures"] = [
            st for st in structures
            if str((st or {}).get("id") or "").strip() not in being_reset
        ]
    return scoped


def generate_scenario(
    datahub_dir: str,
    domain: str,
    raw_spec: dict[str, Any],
    *,
    overwrite: bool = False,
    allow_cooperation_reset: bool = False,
) -> dict[str, Any]:
    """Create (or overwrite) a scenario from a spec: write spec.json + compile.

    Validation is the Preprocessor's job (inside ``_compile_into_folder``); this only
    assembles the canonical shape and runs a filesystem-path-safety slug check.

    When the target folder already exists, its ``spec.json`` is read first and used
    for two things (FIX-6): the destructive-save guard
    (:func:`_guard_cooperation_not_emptied` — pass ``allow_cooperation_reset=True``
    to clear cooperation on purpose) and carrying an authored ``pools`` list the
    incoming payload does not speak. A brand-new scenario reads nothing and behaves
    exactly as before.
    """
    payload = {**raw_spec, "domain": domain}
    slug = _spec_slug(payload)
    slug_err = validate_slug(slug)  # path safety only — spec validity is the Preprocessor's
    if slug_err:
        raise ValueError(slug_err)
    target = scenario_dir(datahub_dir, domain, slug)
    if os.path.exists(target) and not overwrite:
        raise FileExistsError(f"Scenario already exists: {slug}")
    previous = _load_generation_spec_from_disk(target) if os.path.isdir(target) else None
    if not isinstance(previous, dict):
        previous = None
    if _previous_exists_but_is_unreadable(target, previous) and not allow_cooperation_reset:
        # FAIL CLOSED (review finding 12). An unreadable saved spec means the guard
        # cannot tell what would be lost, so it must refuse rather than assume there
        # is nothing to lose.
        raise CooperationResetRequired(
            f"Refusing to overwrite scenario {slug!r}: a spec file exists on disk but "
            "could not be read, so the destructive-save guard cannot tell what this "
            "save would destroy. Fix or remove the unreadable spec, or pass "
            "allow_cooperation_reset=True to overwrite it deliberately."
        )
    _guard_cooperation_not_emptied(
        previous, payload.get("cooperation"), slug, allow_reset=allow_cooperation_reset
    )
    # An explicit reset makes the CLIENT authoritative about cooperation, so the
    # layer-1 pools carry must stand down: carrying an authored ``pools`` back onto
    # a body that just emptied ``edges`` makes the two disagree and the Preprocessor
    # rejects the save ("'pools' and 'edges' disagree"), which left the hatch dead
    # for exactly the pools-authored structures it exists for.
    spec = assemble_spec(
        payload,
        domain,
        previous=(
            _previous_scoped_to_untouched_structures(previous, payload.get("cooperation"))
            if allow_cooperation_reset
            else previous
        ),
    )
    # Fresh generation: no pre-existing sources to carry (a brand-new spec).
    return _compile_into_folder(datahub_dir, domain, slug, spec=spec, carry_sources=False)


def compile_scenario(datahub_dir: str, domain: str, slug: str, *, reseed: bool = False) -> dict[str, Any]:
    """Regenerate ``scenario.json`` by re-running datagen from the folder's sources.

    Reads the folder's ``spec.json`` (the editable recipe), honoring any
    ``scenario_gen.py`` / ``inputs/`` present, and rebuilds ``scenario.json`` + index.
    Back-compat: if there is no ``spec.json`` yet, synthesize it from the recipe
    mirrored inside an existing ``scenario.json``.

    Generation is **seeded**, so a plain recompile reproduces the *same* agent data. Pass
    ``reseed=True`` to draw a fresh master seed (persisted into ``spec.json``) → **new**
    data, still reproducible from there. This is the "regenerate data" action.
    """
    slug_err = validate_slug(slug)
    if slug_err:
        raise ValueError(slug_err)
    target = scenario_dir(datahub_dir, domain, slug)
    if not os.path.isdir(target):
        raise FileNotFoundError(f"Scenario not found: {slug}")

    raw = read_spec(target) or _load_generation_spec_from_disk(target)
    if not isinstance(raw, dict):
        raise ValueError(f"No spec.json or recipe to compile for scenario: {slug}")
    if reseed:
        import random as _random

        raw = {**raw, "seed": _random.randint(1, 2_147_483_647)}
    spec = assemble_spec({**raw, "slug": slug, "domain": domain}, domain)
    return _compile_into_folder(datahub_dir, domain, slug, spec=spec, carry_sources=True)


def stage_sources(
    datahub_dir: str,
    domain: str,
    slug: str,
    *,
    gen_file: str | None = None,
    inputs: list[str] | None = None,
    recompile: bool = True,
) -> dict[str, Any]:
    """Drop authored Tier-2 sources into an existing scenario folder, then recompile.

    ``gen_file`` is copied in as ``scenario_gen.py`` (the per-scenario datagen override);
    each path in ``inputs`` is copied into the folder's ``inputs/`` dir. These are exactly
    the sources ``_compile_into_folder`` honors (``carry_sources=True``), so a subsequent
    ``compile_scenario`` regenerates ``scenario.json`` with them applied. This is the
    CLI/host path for authoring an override + raw uploads without the dashboard (which can
    only POST JSON). Trusted host use only — ``scenario_gen.py`` is imported at generate
    time, never by the server or agents.
    """
    slug_err = validate_slug(slug)
    if slug_err:
        raise ValueError(slug_err)
    target = scenario_dir(datahub_dir, domain, slug)
    if not os.path.isdir(target):
        raise FileNotFoundError(f"Scenario not found: {slug}")

    staged: dict[str, Any] = {"scenario_gen": False, "inputs": []}
    if gen_file:
        if not os.path.isfile(gen_file):
            raise FileNotFoundError(f"gen-file not found: {gen_file}")
        shutil.copy2(gen_file, os.path.join(target, "scenario_gen.py"))
        staged["scenario_gen"] = True
    for src in inputs or []:
        if not os.path.isfile(src):
            raise FileNotFoundError(f"input file not found: {src}")
        dst_dir = os.path.join(target, "inputs")
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src, os.path.join(dst_dir, os.path.basename(src)))
        staged["inputs"].append(os.path.basename(src))

    entry = compile_scenario(datahub_dir, domain, slug) if recompile else scenario_list_entry(target, slug)
    return {"slug": slug, "staged": staged, "scenario": entry}


def edit_scenario(
    datahub_dir: str,
    domain: str,
    slug: str,
    patch: dict[str, Any],
    *,
    allow_cooperation_reset: bool = False,
) -> dict[str, Any]:
    """Patch an existing scenario's recipe and recompile, preserving its sources.

    Reads the folder's current ``spec.json`` (the faithful, lossless recipe — not the
    ``editForm`` projection, which drops fields like ``orderCountUnit``), shallow-merges
    ``patch`` over it (the CLI sends either a handful of ``--set`` fields or a whole edited
    ``spec.json``), then recompiles via ``carry_sources=True`` so any ``scenario_gen.py``
    override / ``inputs/`` already in the folder survive the edit. This is why edit goes
    through compile (not a fresh ``generate_scenario``, which would drop those sources).

    **Merging does NOT make this path safe** (FIX-6 layer 2). The merge is *shallow and
    top-level*, so a ``patch`` that mentions ``cooperation`` at all REPLACES the whole
    block rather than deepening into it: ``{"cooperation": {"structures": [{"id": "c",
    "edges": []}]}}`` empties a pools-authored consortium just as thoroughly as the
    generate path does, and ``{"cooperation": {"structures": []}}`` deletes the structure
    outright. Only a patch that *omits* ``cooperation`` is protected by the merge. So the
    same guard applies here, evaluated on the POST-merge block — an omitted key is not a
    reset — and on the raw incoming block *before* :func:`assemble_spec` runs, since the
    layer-1 pools carry would otherwise mask the loss it is meant to report.
    """
    slug_err = validate_slug(slug)
    if slug_err:
        raise ValueError(slug_err)
    target = scenario_dir(datahub_dir, domain, slug)
    if not os.path.isdir(target):
        raise FileNotFoundError(f"Scenario not found: {slug}")

    base = read_spec(target) or _load_generation_spec_from_disk(target) or {}
    if not isinstance(base, dict):
        base = {}
    merged = {**base, **(patch or {}), "slug": slug, "domain": domain}
    _guard_cooperation_not_emptied(
        base, merged.get("cooperation"), slug, allow_reset=allow_cooperation_reset
    )
    # See generate_scenario: an explicit reset stands the pools carry down, otherwise
    # the carried pools contradict the emptied edges and the Preprocessor rejects it.
    spec = assemble_spec(
        merged,
        domain,
        previous=(
            _previous_scoped_to_untouched_structures(base, merged.get("cooperation"))
            if allow_cooperation_reset
            else base
        ),
    )
    return _compile_into_folder(datahub_dir, domain, slug, spec=spec, carry_sources=True)


def get_scenario_spec(datahub_dir: str, domain: str, slug: str) -> dict[str, Any]:
    """Return the folder's faithful editable recipe (``spec.json``), for hand-editing.

    Unlike ``editForm`` (a lossy projection for the dashboard form), this is the exact
    recipe a recompile consumes — safe to edit and feed back through ``edit_scenario``.
    """
    slug_err = validate_slug(slug)
    if slug_err:
        raise ValueError(slug_err)
    target = scenario_dir(datahub_dir, domain, slug)
    if not os.path.isdir(target):
        raise FileNotFoundError(f"Scenario not found: {slug}")
    spec = read_spec(target) or _load_generation_spec_from_disk(target)
    if not isinstance(spec, dict):
        raise ValueError(f"No spec.json or recipe found for scenario: {slug}")
    return spec


def known_solvers() -> dict[str, Any]:
    """The runtime-selectable assignment solver strategies (the single source of truth)."""
    return {"solvers": sorted(SOLVER_REGISTRY.keys()), "default": DEFAULT_SOLVER}


def known_policies() -> dict[str, Any]:
    """The selectable generation policies per role (single source of truth for the UI).

    Reads the datagen policy registry so the dashboard's per-role dropdowns can never
    drift from what the engine actually supports (mirrors ``known_solvers``)."""
    from apps.container_logistics.datagen.agents import (
        default_policy_name,
        known_policies as _kp,
    )

    per_role = _kp()
    return {
        "policies": {role: names for role, names in per_role.items() if role in ("truck", "order", "facility")},
        "defaults": {role: default_policy_name(role) for role in ("truck", "order", "facility")},
    }


def delete_scenario(datahub_dir: str, domain: str, slug: str) -> None:
    """Remove a scenario dataset folder from disk."""
    slug_err = validate_slug(slug)
    if slug_err:
        raise ValueError(slug_err)
    if slug in PROTECTED_SCENARIO_SLUGS:
        raise ValueError(f"Cannot delete protected scenario: {slug}")
    path = scenario_dir(datahub_dir, domain, slug)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"Scenario not found: {slug}")
    shutil.rmtree(path)  # removes the folder's index.json too
    from . import scenario_index as idx

    idx.remove_from_rollup(scenario_root(datahub_dir, domain), slug)
    try:
        from .scenario_registry import ScenarioRegistry

        ScenarioRegistry(scenario_root(datahub_dir, domain)).delete(slug)
    except Exception:
        pass


def scenario_exists(datahub_dir: str, domain: str, slug: str) -> bool:
    try:
        path = scenario_dir(datahub_dir, domain, slug)
    except ValueError:
        return False
    return scenario_complete(path)
