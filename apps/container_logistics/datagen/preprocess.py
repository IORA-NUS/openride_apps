"""The single preprocess step: ``spec.json`` (+ folder inputs) -> ``GenerationSpec``.

This is the ONE thing between the file and the engine (plan §4.1). It does **all**
validation, resolves each role's policy config into the data the (behavior-tested)
builders read — building the trip-matrix / demand-curve via the ``distributions``
package — normalizes hauliers, computes the calendar + orsim_settings, and derives
per-role sub-seeds. No module global is ever mutated.

It replaces ``normalize_generate_spec`` + ``frontend_scenario_config_override`` +
the ``scenario_config`` global reads + ``scenario_datagen.build_generation_spec``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from copy import deepcopy
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Optional

from apps.container_logistics.rebate import (
    CANONICAL_EPOCH_FORMAT,
    RebateSpecError,
    parse_rebate_schedule,
)
from apps.container_logistics.rebate import reference_hour as _rebate_reference_hour

from . import facility_rules
from .catalog import LocationCatalog, default_locations_csv, default_sg_mask_path
from .codes import CodeRegistry
from . import defaults as D
from . import demand
from . import hauliers as H
from .distributions import ProbabilityMatrix, load_records_file
from .distributions.sources import load_curve_file, load_matrix_file
from .agents.registry import POLICY_REGISTRY, canonical_policy_type, default_policy_name
from .spec import GenerationSpec

MAX_TRUCKS = 1_000_000
MAX_FACILITY_COUNT = 1_000_000
MAX_SIMULATION_DAYS = 7
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Planner framework axes (collaboration plan §5/§6). Topology/timing are framework
# constants validated here; deployment/sharing algorithm NAMES are fail-soft at
# runtime (same contract as the solver strategy — datagen never imports assignment).
PLANNER_TOPOLOGIES = ("partitioned", "pooled", "two-stage")
# 'two-stage' is a DEPRECATED authoring alias for 'pooled': accepted at compile,
# resolved away so the runtime only ever sees the canonical name.
PLANNER_TOPOLOGY_ALIASES = {"two-stage": "pooled"}
PLANNER_TIMING_MODES = ("online", "static", "hybrid")
DEFAULT_SHARING_ALGORITHM = "transfer-when-cheaper"

# Shared-pool market axes (plan §3.4). Shape is validated here; algorithm NAMES
# stay fail-soft at runtime (datagen never imports assignment), exactly like the
# solver strategy / deployment.type.
DEFAULT_MARKET = {
    "offer": {"type": "OfferAll", "params": {}},
    "claim": {"type": "ClaimAllPlanned", "params": {}},
    "arbitration": {"type": "LowestCost", "params": {}},
    # SAFETY BACKSTOP, not a tuning knob (plan §13.4 FIX-1 / §13.8 errata).
    # The pooled market terminates on CONVERGENCE; this only bounds a pathological
    # case. The former default of 2 truncated the auction before convergence in
    # proportion to how much cooperation was configured, which — because serving
    # fewer orders improves mean deadhead (solver_boundary_audit.md P3) — handed
    # the cooperating arm a spurious deadhead advantage that grew with the number
    # of cooperating companies. Must stay in sync with
    # ``assignment.pooled_planner.DEFAULT_MAX_ROUNDS`` (a test asserts it).
    "max_rounds": 20,
}
MARKET_ROLES = ("offer", "claim", "arbitration")
MARKET_MAX_ROUNDS_MIN = 1
# Ceiling raised from 10: the old ceiling sat AT the observed convergence point,
# so an operator who noticed the truncation could not configure their way out.
MARKET_MAX_ROUNDS_MAX = 100


class SpecValidationError(ValueError):
    """Raised for any invalid spec.json — the single validation surface."""


@dataclass
class Compiled:
    spec: GenerationSpec
    orsim_settings: dict
    recipe: dict


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return (slug or "scenario")[:64]


def _uniform_grid(codes: list[str]) -> dict[str, dict[str, float]]:
    """Equal weight on every off-diagonal (row, col) pair — the 'random' matrix."""
    grid = {p: {d: (0.0 if p == d else 1.0) for d in codes} for p in codes}
    total = sum(v for row in grid.values() for v in row.values())
    if total <= 0:
        raise SpecValidationError("Cannot build a uniform matrix with fewer than 2 codes")
    return {p: {d: v / total for d, v in row.items()} for p, row in grid.items()}


class Preprocessor:
    """Compile a raw spec.json dict into an immutable :class:`GenerationSpec`."""

    # A single shared catalog is fine (real addresses never change per compile).
    _catalog: Optional[LocationCatalog] = None

    @classmethod
    def catalog(cls) -> LocationCatalog:
        if cls._catalog is None:
            cls._catalog = LocationCatalog(
                default_locations_csv(),
                mask_path=default_sg_mask_path(),
                excluded=D.EXCLUDED_CODES,
            )
        return cls._catalog

    # ---- validation helpers ----------------------------------------------

    @staticmethod
    def _resolve_file(value: Any, kind: str, scenario_dir: Optional[str]):
        """Resolve a `{"$file": path}` or a bare path (relative to scenario_dir)."""
        path = None
        if isinstance(value, dict) and "$file" in value:
            path = value["$file"]
        elif isinstance(value, str):
            path = value
        else:
            return value  # already inline
        if not isinstance(path, str) or not path.strip():
            raise SpecValidationError(f"{kind} $file must be a non-empty path")
        path = path.strip()
        if not os.path.isabs(path) and scenario_dir:
            path = os.path.join(scenario_dir, path)
        if not os.path.isfile(path):
            raise SpecValidationError(f"{kind} file not found: {path}")
        if kind == "matrix":
            return load_matrix_file(path)
        if kind == "curve":
            return load_curve_file(path)
        if kind == "records":
            return load_records_file(path)
        raise SpecValidationError(f"unknown file kind {kind!r}")

    @staticmethod
    def _normalize_planner(raw_planner) -> dict:
        """Validate the planner axes (topology × timing × algorithms) — plan §6.

        Topology/timing are framework constants → hard-validated here. Algorithm
        *names* (deployment/sharing) are fail-soft at runtime like the solver
        strategy; deployment.type is resolved against 'solver' by the caller.
        """
        if raw_planner is None:
            raw_planner = {}
        if not isinstance(raw_planner, dict):
            raise SpecValidationError("planner: must be an object")

        topology = str(raw_planner.get("topology") or "partitioned")
        if topology not in PLANNER_TOPOLOGIES:
            raise SpecValidationError(
                f"planner: unknown topology {topology!r}. Available: {list(PLANNER_TOPOLOGIES)}"
            )
        # Resolve the deprecated alias AFTER validation: the runtime only sees
        # canonical names. ``topology`` below stays the AUTHORED value so the
        # legacy 'two-stage' sharing default keeps behaving exactly as before.
        resolved_topology = PLANNER_TOPOLOGY_ALIASES.get(topology, topology)

        timing_raw = raw_planner.get("timing")
        if isinstance(timing_raw, str):
            timing = {"mode": timing_raw}
        elif isinstance(timing_raw, dict):
            timing = deepcopy(timing_raw)
        elif timing_raw is None:
            timing = {"mode": "online"}
        else:
            raise SpecValidationError("planner: 'timing' must be a mode string or object")
        mode = str(timing.get("mode") or "online")
        if mode not in PLANNER_TIMING_MODES:
            raise SpecValidationError(
                f"planner: unknown timing mode {mode!r}. Available: {list(PLANNER_TIMING_MODES)}"
            )
        timing["mode"] = mode

        deployment_raw = raw_planner.get("deployment")
        deployment = deepcopy(deployment_raw) if isinstance(deployment_raw, dict) else {}

        sharing_raw = raw_planner.get("sharing")
        if isinstance(sharing_raw, dict) and sharing_raw.get("type"):
            sharing = deepcopy(sharing_raw)
            sharing.setdefault("params", {})
        elif topology == "two-stage":
            sharing = {"type": DEFAULT_SHARING_ALGORITHM, "params": {}}
        else:
            sharing = None

        return {
            "topology": resolved_topology,
            "timing": timing,
            "deployment": deployment,
            "sharing": sharing,
            "market": Preprocessor._normalize_market(raw_planner.get("market")),
            # R2-7 / review F1 — HIGH-1. This closed dict literal is the SECOND instance
            # of the G23 silent-drop mechanism in this feature. The plan checked
            # SPEC_KEYS because the project had a scar there, registered the authoring
            # surface under `overrides.facility` to sidestep it, and then walked into the
            # identical failure one level down: `rebate_aware` was stripped here, so the
            # opt-in seam could not be enabled from a scenario file AT ALL and the
            # feature's stated purpose was unreachable. The generalisable rule is that
            # "is this key registered?" is a question about EVERY dict literal a config
            # passes through, not about the one that burned us last time.
            #
            # Defaults false, and no shipped scenario sets it, so this restores the
            # ABILITY to switch the seam on without switching it on.
            # R3-9 / review R2-8: a boolean POLICY flag gets no truthiness coercion.
            # `bool("false")` is True, so a JSON-ish client sending the string "false"
            # would have SWITCHED THE SEAM ON. Matches the house discipline at
            # `preprocess.py` max_rounds and `rebate.py::_is_real_number`.
            "rebate_aware": Preprocessor._strict_bool(
                raw_planner.get("rebate_aware", False), "planner.rebate_aware"
            ),
        }

    @staticmethod
    def _strict_bool(value, where: str) -> bool:
        """A real boolean, or a spec error. No truthiness coercion on a policy flag."""
        if isinstance(value, bool):
            return value
        raise SpecValidationError(
            f"{where}: must be a boolean true/false (got {value!r} of type "
            f"{type(value).__name__}). Strings are refused rather than coerced: "
            f"bool(\"false\") is True, which would silently ENABLE the flag."
        )

    @staticmethod
    def _normalize_market(raw_market) -> dict:
        """Shape-validate the planner's ``market`` sub-block; always emit it (defaulted).

        Validated: ``market`` is an object; ``offer``/``claim``/``arbitration`` are each
        an object with a string ``type`` and a dict ``params`` (missing -> the default);
        ``max_rounds`` is an int in ``MARKET_MAX_ROUNDS_MIN..MARKET_MAX_ROUNDS_MAX``
        (1..100 — a safety backstop, not a tuning knob; the market terminates on
        convergence, plan §13.4 FIX-1). Algorithm NAMES are deliberately NOT
        checked against any registry — a typo must degrade a run at runtime, not abort
        the compile (plan §7).
        """
        if raw_market is None:
            raw_market = {}
        if not isinstance(raw_market, dict):
            raise SpecValidationError("planner: 'market' must be an object")

        market = deepcopy(raw_market)
        for role in MARKET_ROLES:
            default = DEFAULT_MARKET[role]
            block_raw = market.get(role)
            if block_raw is None:
                market[role] = deepcopy(default)
                continue
            if not isinstance(block_raw, dict):
                raise SpecValidationError(
                    f"planner: market {role!r} must be an object with 'type' and 'params'"
                )
            block = deepcopy(block_raw)
            ptype = block.get("type")
            if ptype is None:
                ptype = default["type"]
            if not isinstance(ptype, str) or not ptype.strip():
                raise SpecValidationError(
                    f"planner: market {role!r} 'type' must be a non-empty string"
                )
            params = block.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise SpecValidationError(f"planner: market {role!r} 'params' must be an object")
            block["type"] = ptype.strip()
            block["params"] = params
            market[role] = block

        rounds = market.get("max_rounds")
        if rounds is None:
            rounds = DEFAULT_MARKET["max_rounds"]
        if isinstance(rounds, bool) or not isinstance(rounds, int):
            raise SpecValidationError(
                f"planner: market 'max_rounds' must be an integer in "
                f"{MARKET_MAX_ROUNDS_MIN}..{MARKET_MAX_ROUNDS_MAX} (got {rounds!r})"
            )
        if not MARKET_MAX_ROUNDS_MIN <= rounds <= MARKET_MAX_ROUNDS_MAX:
            raise SpecValidationError(
                f"planner: market 'max_rounds' must be an integer in "
                f"{MARKET_MAX_ROUNDS_MIN}..{MARKET_MAX_ROUNDS_MAX} (got {rounds!r})"
            )
        market["max_rounds"] = rounds
        return market

    # ---- the compile ------------------------------------------------------

    @classmethod
    def compile(
        cls,
        raw: dict,
        *,
        domain: str,
        scenario_dir: Optional[str] = None,
        reference_time: str = "2020-01-01 08:00:00",
    ) -> Compiled:
        if not isinstance(raw, dict):
            raise SpecValidationError("spec.json must be a JSON object")

        # --- the simulation epoch (R3-1 + R3-5; review R2-3, R2-4) ---------------
        # Validated UNCONDITIONALLY, for every spec, rebate or not. The previous guard
        # was scoped to specs carrying a rebate schedule, justified in §18.3 as
        # "it touches no existing scenario" — which optimised for blast radius and
        # thereby protected exactly ONE ARM of a two-arm experiment. The control arm is
        # the one nobody inspects: it declared midnight, a save stood the carry down to
        # `null`, and it silently reverted to the 08:00 caller default. A paired
        # estimator is structurally BLIND to that, because each arm is internally
        # consistent while their demand curves sit on different wall clocks.
        #
        # Safe because it refuses nothing that exists: all 15 compiled bundles use the
        # canonical "%Y-%m-%d %H:%M:%S" form (13x 08:00:00, 1x 04:00:00 in
        # smoke_container_logistics_1d, 1x 00:00:00 in the rebate scenario), surveyed
        # 2026-08-21. If a later scenario fails to compile here, that survey is where to
        # start: a NON-canonical epoch is now refused rather than silently stamped.
        #
        # ABSENT is fine (the 13 scenarios predating the key carry no `referenceTime`
        # at all, and inherit the caller default as they always did). PRESENT-BUT-NULL
        # is the error: it is what a save produces when the carry is stood down by
        # presence, and it is indistinguishable in the compiled bundle from a scenario
        # that never declared an epoch.
        # A null/empty `referenceTime` is treated as ABSENT here, not as an error.
        #
        # DEVIATION from §19.4 R3-1's literal "present-but-null refused for every spec",
        # with the reason measured: `assemble_spec` writes `referenceTime: None` into
        # EVERY spec that never declared one, so present-but-null is the ordinary
        # post-save state rather than an anomaly. Refusing it here failed 84 tests
        # across 10 files — ordinary scenario saves, not edge cases.
        #
        # The loss R2-3 describes is prevented at the layer where it actually happens:
        # `frontend_scenario_spec._NULL_MEANS_UNSUPPLIED` makes an explicit null on this
        # key inherit rather than stand the carry down, so a DECLARED epoch can no
        # longer be wiped by a save. That is the reviewer's own option (b), and it is
        # strictly stronger than refusing at compile — it protects the value instead of
        # detecting its absence after the fact. A surviving null now means only "nobody
        # ever declared an epoch", which is exactly the harmless case.
        if raw.get("referenceTime") is not None and not (
            isinstance(raw.get("referenceTime"), str) and not raw["referenceTime"].strip()
        ):
            if cls._reference_hour(raw["referenceTime"]) is None:
                raise SpecValidationError(
                    f"spec: declared 'referenceTime' {raw['referenceTime']!r} is not in "
                    f"the canonical {CANONICAL_EPOCH_FORMAT!r} form "
                    f"(e.g. '2020-01-01 00:00:00')."
                )

        # The epoch that will actually be compiled in must be readable by the ONE shared
        # rule (rebate.reference_hour). The `None` exemption is deliberately GONE: an
        # unparseable epoch used to be waved through and, in the ISO-offset case, even
        # stamped `axes_aligned: true` — a confident, wrong provenance record.
        if cls._reference_hour(reference_time) is None:
            raise SpecValidationError(
                f"spec: simulation epoch {reference_time!r} is not in the canonical "
                f"{CANONICAL_EPOCH_FORMAT!r} form (e.g. '2020-01-01 00:00:00'). "
                f"Offset/ISO spellings are refused rather than guessed: an offset makes "
                f"'which wall clock' ambiguous, and the permissive parser this replaces "
                f"blessed '2020-01-01T00:00:00+08:00' as hour 0 and stamped the run "
                f"axes_aligned=true. All 15 existing bundles already use the canonical "
                f"form, so this refuses nothing that exists."
            )

        # --- identity ---
        name = str(raw.get("name") or "").strip()
        if not name:
            raise SpecValidationError("Scenario name is required")
        slug = str(raw.get("slug") or _slugify(name)).strip()
        if not _SLUG_RE.match(slug):
            raise SpecValidationError(
                "Slug must be 1-64 chars: lowercase letters, digits, underscore, hyphen"
            )

        # --- calendar ---
        days = int(raw.get("simulationDays") or raw.get("simulation_days") or 1)
        days = max(1, min(MAX_SIMULATION_DAYS, days))
        step_interval = D.STEP_INTERVAL_SECONDS
        sim_len = D.simulation_length_in_steps(days, step_interval)
        seed = int(raw.get("seed") if raw.get("seed") is not None else D.DEFAULT_SEED)

        # --- agents / counts ---
        agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
        truck_a = agents.get("truck") if isinstance(agents.get("truck"), dict) else {}
        order_a = agents.get("order") if isinstance(agents.get("order"), dict) else {}
        facility_a = agents.get("facility") if isinstance(agents.get("facility"), dict) else {}

        num_trucks = max(1, min(MAX_TRUCKS, int(truck_a.get("count") or 1)))
        raw_orders = max(1, int(order_a.get("count") or 1))
        order_unit = str(order_a.get("orderCountUnit") or raw.get("orderCountUnit") or "total")
        if order_unit not in ("per_day", "total"):
            order_unit = "total"
        num_orders = raw_orders * days if order_unit == "per_day" else raw_orders
        num_facilities = max(1, min(MAX_FACILITY_COUNT, int(facility_a.get("count") or D.NUM_FACILITIES)))

        # --- policies (validate types up front) ---
        role_policies = {}
        for role, a in (("truck", truck_a), ("order", order_a), ("facility", facility_a)):
            pol = a.get("policy") if isinstance(a.get("policy"), dict) else {"type": default_policy_name(role)}
            # Normalize deprecated aliases (e.g. order "matrix" -> "historical") to the
            # canonical type, so old spec.json compiles and the recipe records the new name.
            ptype = canonical_policy_type(role, str(pol.get("type") or default_policy_name(role)))
            if (role, ptype) not in POLICY_REGISTRY:
                avail = sorted(n for (r, n) in POLICY_REGISTRY if r == role)
                raise SpecValidationError(f"Unknown {role} policy type {ptype!r}. Available: {avail}")
            role_policies[role] = {**pol, "type": ptype}
        role_policies["assignment"] = {"type": "default"}
        role_policies["analytics"] = {"type": "default"}

        # --- hauliers (normalize + enforce 100% shares — one validation surface) ---
        try:
            hlist = H.normalize_hauliers(raw.get("hauliers"))
        except ValueError as exc:
            raise SpecValidationError(str(exc))
        truck_hauliers = tuple(H.distribute_by_share(num_trucks, hlist, "fleet_share"))
        order_hauliers = tuple(H.distribute_by_share(num_orders, hlist, "order_share"))

        # --- cooperation structures + planner config (collaboration plan §1/§6) ---
        # Carried DATA only (invariant I1): nothing here touches sampling inputs, so
        # compiling the same spec with/without these blocks yields byte-identical agents.
        try:
            cooperation = H.normalize_cooperation(
                raw.get("cooperation"), [h["id"] for h in hlist]
            )
        except ValueError as exc:
            raise SpecValidationError(str(exc))
        # Which structures the AUTHOR wrote pools for — the recipe echoes pools back
        # only for those (see the recipe block below).
        authored_pool_ids = H.authored_pool_structure_ids(raw.get("cooperation"))
        planner_cfg = cls._normalize_planner(raw.get("planner"))

        # The arm must always be NAMED, never inherited silently (plan §14.7 item 5).
        # A scenario that declares cooperation pools but resolves to `partitioned`
        # runs the LEGACY planner while its pools sit visibly intact in the editor —
        # which is exactly the state CRITICAL-1 leaves behind when a save drops the
        # `planner` key. This warning is a cheap detector for that silent flip; it is
        # deliberately NOT an error, because partitioned + cooperation is a legitimate
        # (and currently default) configuration.
        try:
            _active_struct = H.active_structure(cooperation)
            _declared_pools = _active_struct.get("pools") or []
            if _declared_pools and planner_cfg.get("topology") == "partitioned":
                logging.warning(
                    "Scenario %r declares %d cooperation pool(s) on structure %r but "
                    "planner.topology resolves to 'partitioned', so shared-pool "
                    "planning is OFF for this scenario. If that is not intended, set "
                    "planner.topology='pooled' (or run with ORSIM_PLANNER_TOPOLOGY="
                    "pooled). A dropped 'planner' key silently produces this state.",
                    slug, len(_declared_pools), _active_struct.get("id"),
                )
        except Exception:  # pragma: no cover - a warning must never break a compile
            logging.debug("pools/topology mismatch check failed", exc_info=True)

        # --- order location distribution (matrix/random/historical) ---
        catalog = cls.catalog()
        codes = list(catalog.codes())
        order_pol = role_policies["order"]
        otype = order_pol["type"]
        # Historical policies read records both for the OD matrix (facilities) and the
        # arrival curve; load them once here and hand them to the policy via params.
        # 'historical' (given data) samples a ProbabilityMatrix (+ Curve). The data is an
        # authored trip matrix by default, OR learned from a records file when 'source' is
        # given (source is optional — no source means "use the authored/default matrix").
        order_records = None
        if otype == "historical":
            src = order_pol.get("source")
            if src:
                order_records = cls._resolve_file({"$file": src} if isinstance(src, str) else src, "records", scenario_dir)
        trip_matrix = cls._resolve_order_matrix(order_pol, otype, codes, scenario_dir, raw, records=order_records)
        # Hand the loaded records to the order policy (used for the arrival Curve too).
        role_policies["order"] = {**order_pol, "records": order_records}

        # --- order demand curve ---
        curve_raw = cls._resolve_file(order_pol.get("curve") or raw.get("orderDemandCurve"), "curve", scenario_dir)
        if otype == "random" and order_pol.get("curve") is None:
            hourly_weights = None  # uniform time
            curve_persist = None
        else:
            curve_persist = demand.parse_order_demand_curve(curve_raw) if curve_raw else demand.parse_order_demand_curve(None)
            hourly_weights = [p["weight"] for p in curve_persist["points"]]

        # --- early orders ---
        early_raw = raw.get("earlyOrderCount")
        recommended = demand.recommended_early_order_count(num_trucks, num_orders)
        if early_raw is not None:
            early = max(0, min(num_orders, int(early_raw)))
            if early >= demand.LEGACY_EARLY_ORDER_COUNT or early > num_trucks * 3:
                early = recommended
        else:
            early = recommended

        # --- facilities: weight the code mix per the facility policy ---
        # "allocate" (default) is demand-proportional to the order matrix;
        # "random" ignores demand (None matrix -> CodeRegistry equal-split).
        fac_matrix = None if role_policies["facility"]["type"] == "random" else trip_matrix
        registry = CodeRegistry.build(codes, fac_matrix, D.LOCATION_TYPE_METADATA)
        sites = catalog.facility_sites(
            num_facilities, registry,
            gate_count=D.FACILITY_GATE_COUNT, service_time=D.FACILITY_SERVICE_TIME,
        )

        # --- build the *_settings the builders read (defaults + overrides) ---
        settings = D.role_settings()
        overrides = cls._merged_overrides(raw)
        cls._reject_rebate_by_code(overrides, slug=slug)
        cls._validate_blanket_facility_overrides(overrides, slug=slug)
        settings["truck"]["num_trucks"] = num_trucks
        settings["order"]["num_orders"] = num_orders
        settings["order"]["early_order_count"] = early
        settings["order"]["order_demand_weights"] = hourly_weights
        settings["order"]["trip_matrix"] = trip_matrix
        settings["facility"]["num_facilities"] = num_facilities
        # Per-role static overrides (safe profile fields — the builders regenerate
        # sampled fields, so nothing stale from a merged roleSettings survives).
        for role in ("truck", "order", "facility"):
            patch = overrides.get(role) if isinstance(overrides.get(role), dict) else {}
            prof = settings[role].setdefault("profile", {})
            for k, v in patch.items():
                if v is not None:
                    prof[k] = v
        # Stamp the (possibly overridden) service_time / gate_count onto each fresh
        # site, then set facilities LAST so no override can clobber the land-safe list.
        fac_profile = settings["facility"]["profile"]
        for s in sites:
            if fac_profile.get("service_time") is not None:
                s["service_time"] = fac_profile["service_time"]
            if fac_profile.get("gate_count") is not None:
                s["gate_count"] = fac_profile["gate_count"]
        # --- facilityRules: the targeted layer (plan §4, §7, §8.3) --------------
        # ORDERING TRAP (§8.3.1): this MUST run *after* the blanket re-stamp above,
        # which is rank 0. Applying rules first would let the blanket layer overwrite
        # them — an inverted precedence that no test of the pure resolver can catch,
        # because the resolver would be correct and only the wiring wrong.
        # The values land on the SITE, not the profile (§8.3.2): both builders read
        # gate_count/service_time off the site, and the facility agent emits
        # gate_count BOTH at the top level (which the runtime schema validates) and
        # inside profile (which facility/manager.py actually reads). Stamping the
        # site gives both; patching either alone is silently wrong in one direction.
        facility_rules_list = cls._resolve_facility_rules(
            raw, sites, codes, fac_profile, slug=slug
        )
        # RE-POINTED (facility rules plan §11.2 — the top migration hazard).
        # This used to run BEFORE any of the above and to early-return when
        # `overrides.facility.rebate` and `.rebate_by_code` were both absent. The
        # migration makes both absent, which would have silently disarmed the
        # midnight-epoch refusal, the mixed-currency refusal and two warnings — with
        # NO test failing, because the tests covering them authored the very key
        # being removed. It now judges the POST-RESOLUTION set (`site["rebate"]`),
        # so it covers rules-authored and blanket-authored schedules identically and
        # cannot be disarmed by the authoring surface changing again.
        cls._validate_facility_rebate(
            overrides, facility_rules_list, sites,
            slug=slug, reference_time=reference_time,
        )
        fac_profile["facilities"] = sites
        # Solver selection. The planner's deployment algorithm and the legacy
        # 'solver' knob are ONE dial: 'solver' wins if both are given, and the
        # resolved strategy is mirrored into planner.deployment.type so runtime,
        # recipe, and UI can never drift.
        strategy = str(
            raw.get("solver")
            or (planner_cfg.get("deployment") or {}).get("type")
            or settings["assignment"]["profile"]["strategy"]
        )
        settings["assignment"]["profile"]["strategy"] = strategy
        # Solver params: defaults <- planner.deployment.params <- top-level solverParams
        # (most-specific last). Authored deployment params must never be silently
        # dropped (F4) — the recipe echoes exactly what runs.
        base = dict(settings["assignment"]["profile"].get("solver_params") or {})
        dp = (planner_cfg.get("deployment") or {}).get("params")
        if isinstance(dp, dict):
            base.update(dp)
        sp = raw.get("solverParams")
        if isinstance(sp, dict):
            base.update(sp)
        settings["assignment"]["profile"]["solver_params"] = base
        planner_cfg["deployment"] = {
            "type": strategy,
            "params": dict(base),
        }
        # Bake collaboration config onto the assignment profile — pure data the
        # planner host reads at spawn (and the per-run override patches in memory).
        settings["assignment"]["profile"]["cooperation"] = deepcopy(cooperation)
        settings["assignment"]["profile"]["planner"] = deepcopy(planner_cfg)

        # --- orsim settings (from calendar + runtime tuning) ---
        orsim = cls._orsim_settings(domain, days, step_interval, sim_len, reference_time)

        # --- recipe (persisted alongside for edit/list) ---
        # Drop the order policy's raw file/inline matrix+curve refs: the RESOLVED,
        # inlined values live in ``tripMatrix``/``orderDemandCurve`` below, so the
        # persisted recipe (== spec.json) stays self-contained and re-resolves nothing
        # on recompile. (``source`` is kept — a 'historical' policy re-learns from it.)
        recipe_order_pol = {k: v for k, v in order_pol.items() if k not in ("matrix", "curve")}
        recipe = {
            "source": "spec",
            "frozen": True,
            "name": name[:80],
            "slug": slug,
            "simulationDays": days,
            "seed": seed,
            # The scenario's declared epoch, round-tripped so a recompile keeps the
            # hour axis it was authored on rather than silently inheriting the caller
            # default (which is how the two axes drifted apart in the first place).
            "referenceTime": reference_time,
            # --- hour-axis provenance (plan §18.3 step 1, review F2) -------------
            # These two curves in the SAME spec.json are measured on DIFFERENT clocks,
            # and nothing said so. Stamped so every bundle is self-describing about
            # which clock each curve speaks — retroactively legible for bundles
            # compiled before the midnight rule existed.
            "hour_axis": {
                "orderDemandCurve": (
                    "hours since REFERENCE_TIME (sampling.py never reads the epoch, so "
                    "authored hour H is realised at wall hour (H + reference_hour) % 24)"
                ),
                "rebate": (
                    "sim wall clock; epoch = REFERENCE_TIME (price_at parses the "
                    "recorded RFC-1123 arrival stamp)"
                ),
                "reference_time": reference_time,
                "reference_hour": cls._reference_hour(reference_time),
                # 0 means the two axes coincide. Anything else is the offset by which
                # an authored demand hour is displaced from its wall-clock label.
                "demand_to_wall_offset_hours": cls._reference_hour(reference_time),
                "axes_aligned": cls._reference_hour(reference_time) == 0,
            },
            "orderCountUnit": order_unit,
            "agents": {
                "truck": {"count": num_trucks, "policy": role_policies["truck"]},
                "order": {"count": raw_orders, "orderCountUnit": order_unit, "policy": recipe_order_pol},
                "facility": {"count": num_facilities, "policy": role_policies["facility"]},
            },
            "earlyOrderCount": early,
            "orderDemandCurve": curve_persist,
            "tripMatrix": trip_matrix,
            "hauliers": hlist,
            # Authored (round-trip) form: edges always; ``pools`` ONLY for the
            # structures whose author wrote pools, so an edges-authored scenario
            # round-trips byte-identically. adjacency/components stay derived at
            # compile and live on the baked assignment profile.
            "cooperation": {
                "active": cooperation["active"],
                "structures": [
                    (
                        {"id": s["id"], "edges": deepcopy(s["edges"]), "pools": deepcopy(s["pools"])}
                        if s["id"] in authored_pool_ids
                        else {"id": s["id"], "edges": deepcopy(s["edges"])}
                    )
                    for s in cooperation["structures"]
                ],
            },
            "planner": deepcopy(planner_cfg),
            "solver": strategy,
            "solverParams": deepcopy(sp) if isinstance(sp, dict) else None,
            "overrides": deepcopy(overrides) if overrides else None,
            # Echoed VERBATIM (facility rules plan §9, literal 2). scenario.json's
            # $.recipe is the round-trip source for _load_generation_spec_from_disk,
            # the editor's index.json.editForm, and the back-compat "synthesise a
            # spec from the bundle" path — miss it and a recompile from the bundle
            # silently drops the rules. The raw authored value is echoed, not the
            # normalised one, so an empty list stays an empty list.
            "facilityRules": deepcopy(raw.get("facilityRules")),
            "facilityRulesWorld": deepcopy(raw.get("facilityRulesWorld")),
            "behaviorRevision": D.BEHAVIOR_REVISION,
        }

        spec = GenerationSpec(
            domain=domain,
            num_trucks=num_trucks,
            num_orders=num_orders,
            num_facilities=len(sites),
            simulation_days=days,
            step_interval_seconds=step_interval,
            simulation_length_in_steps=sim_len,
            reference_time=reference_time,
            behavior_revision=D.BEHAVIOR_REVISION,
            truck_settings=settings["truck"],
            order_settings=settings["order"],
            facility_settings=settings["facility"],
            assignment_settings=settings["assignment"],
            analytics_settings=settings["analytics"],
            truck_hauliers=truck_hauliers,
            order_hauliers=order_hauliers,
            hourly_weights=hourly_weights,
            business_hour_start=int(settings["order"].get("business_hour_start", 0)),
            business_hour_end=int(settings["order"].get("business_hour_end", 24)),
            trip_matrix=trip_matrix,
            excluded_codes=tuple(D.EXCLUDED_CODES),
            truck_origin_codes=None,
            locations_csv=default_locations_csv(),
            sg_mask_path=default_sg_mask_path(),
            orsim_settings=orsim,
            generation_spec_meta=recipe,
            seed=seed,
            role_policies=role_policies,
        )
        return Compiled(spec=spec, orsim_settings=orsim, recipe=recipe)

    # ---- pieces -----------------------------------------------------------

    # Keys never merged from a legacy roleSettings.profile (regenerated per agent
    # or would clobber the fresh land-safe facility list).
    _UNSAFE_OVERRIDE_KEYS = frozenset(
        {"facilities", "location", "name", "pickup_facility_name", "dropoff_facility_name",
         "pickup_loc", "dropoff_loc", "pickup_service_time", "dropoff_service_time",
         "estimated_time_to_pickup", "estimated_time_to_dropoff", "home_facility_name"}
    )

    @classmethod
    def _merged_overrides(cls, raw: dict) -> dict:
        """New-schema ``overrides`` + legacy ``roleSettings.profile`` (safe keys only)."""
        merged: dict[str, dict] = {}
        role_settings = raw.get("roleSettings") if isinstance(raw.get("roleSettings"), dict) else {}
        for role in ("truck", "order", "facility"):
            rs = role_settings.get(role) if isinstance(role_settings.get(role), dict) else {}
            prof = rs.get("profile") if isinstance(rs.get("profile"), dict) else {}
            safe = {k: v for k, v in prof.items() if k not in cls._UNSAFE_OVERRIDE_KEYS}
            if safe:
                merged[role] = safe
        overrides = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
        for role in ("truck", "order", "facility"):
            patch = overrides.get(role) if isinstance(overrides.get(role), dict) else {}
            if patch:
                merged.setdefault(role, {}).update(patch)
        return merged

    #: R3-4: the epoch rule lives in ONE place. This shim keeps the internal call sites
    #: unchanged while delegating to the shared helper, so ``recipe.hour_axis`` and
    #: ``meta.rebate.hour_axis`` can never disagree about the same run again.
    _reference_hour = staticmethod(_rebate_reference_hour)

    @classmethod
    def _resolve_facility_rules(
        cls, raw: dict, sites: list, codes: list, fac_profile: dict, *, slug: str,
    ) -> list:
        """Validate + apply ``facilityRules`` onto the freshly sampled sites.

        Returns the validated rule list, used only to tell
        :meth:`_validate_facility_rebate` whether pricing was INTENDED. The recipe
        echoes ``raw`` directly rather than anything returned here, so the persisted
        form is the AUTHORED one verbatim — an empty list stays an empty list, and a
        "helpful" normalisation cannot drift the authored and compiled forms apart.

        The staleness fingerprint is checked FIRST, before rule schema validation:
        if the facility world moved, "these rules were written against a different
        world" is the useful thing to be told, and a zero-match error would be a
        misleading answer to it (§4.6 / §18.3).
        """
        rules_raw = raw.get("facilityRules")
        world = raw.get("facilityRulesWorld")
        try:
            facility_rules.validate_world_baseline(world, sites, rules_raw, slug=slug)
            rules = facility_rules.validate_facility_rules(
                rules_raw, sites, codes, slug=slug
            )
            resolution = facility_rules.resolve_facility_rules(
                rules, sites, fac_profile, slug=slug
            )
            facility_rules.apply_resolution(sites, resolution)
        except facility_rules.FacilityRulesError as exc:
            # Re-raised as the house type so the single validation surface stays
            # single; facility_rules keeps its own error class to avoid a back-edge
            # import into this module.
            raise SpecValidationError(str(exc)) from exc
        return rules

    @classmethod
    def _validate_blanket_facility_overrides(cls, overrides: dict, *, slug: str) -> None:
        """Run the rule layer's own coercers over ``overrides.facility`` (R2-3 / F2).

        Until now ``SETTABLE_KEYS`` validated rank 10/30 and nothing validated rank 0,
        so the two authoring paths to the same key disagreed about what a legal value
        is. The rules validator's own comment says it exists to stop an agent-boot
        crash 500 agents into a run — and it stopped it on one of the two doors that
        can cause it. ``overrides.facility.gate_count: 0`` still reached
        ``FacilityQueueController``, whose assert fires at boot.

        **Unconditional and hard**, deliberately. Measured blast radius is zero: of
        the 15 shipped specs, 6 carry ``gate_count``/``service_time`` and every one is
        ``1``/``1800``, which these coercers accept. A warning-first phase or a
        presence-scoped exemption would be caution bought with nothing — and scoping a
        guard for a reason that turns out to cost more than it saves is the exact
        failure this feature has already paid for once.

        Only the intersection with ``SETTABLE_KEYS`` is touched. The blanket layer
        legitimately carries dead keys (``facility_type``, ``fifo_queue_policy``,
        ``max_queue_size``, ``operating_hours``, ``status``) that every shipped spec
        sets, and those must keep flowing untouched — the allow-list governs what a
        RULE may set, not what the blanket layer may carry.
        """
        facility_overrides = (
            overrides.get("facility") if isinstance(overrides.get("facility"), dict) else {}
        )
        for key, spec_ in facility_rules.SETTABLE_KEYS.items():
            if key not in facility_overrides:
                continue
            value = facility_overrides[key]
            if value is None:
                # The blanket merge itself skips None (`if v is not None`), so a null
                # here means "not set" rather than "set to null" — the one place this
                # layer's semantics legitimately differ from a rule's.
                continue
            try:
                spec_.coerce(value, f"spec {slug!r}: overrides.facility.{key}")
            except facility_rules.FacilityRulesError as exc:
                raise SpecValidationError(str(exc)) from exc

    @classmethod
    def _reject_rebate_by_code(cls, overrides: dict, *, slug: str) -> None:
        """``overrides.facility.rebate_by_code`` is REJECTED, not deprecated.

        Not silently translated either. Three reasons, in order of weight (facility
        rules plan §11.1):

        1. The blast radius was ONE scenario, migrated in the same change — there is
           no installed base to protect.
        2. Silent translation creates two mechanisms that must agree forever, and
           their precedence semantics are not identical (``rebate_by_code[code] =
           null`` is a rank-10 ``{"rebate": null}``). Encoding that equivalence is a
           permanent correctness obligation for one file's worth of value.
        3. Accept-with-deprecation is the P11 shape: a key that still works is a key
           new scenarios will use, and the boundary audit's verdict on config that
           advertises a seam it no longer has is that it is *worse* than no key.

        The rejection carries the translation, so the error IS the migration guide.

        Checked against the MERGED overrides, so the key cannot ride in through the
        legacy ``roleSettings.facility.profile`` back door either.
        """
        facility_overrides = (
            overrides.get("facility") if isinstance(overrides.get("facility"), dict) else {}
        )
        if "rebate_by_code" not in facility_overrides:
            return
        raise SpecValidationError(
            f"spec {slug!r}: overrides.facility.rebate_by_code is no longer supported. "
            f"It was one per-key mechanism; facilityRules is one mechanism for every "
            f"per-facility key.\n\n"
            f"Translate:\n"
            f'    "overrides": {{ "facility": {{ "rebate_by_code": {{ "CT": {{…}}, "MT": null }} }} }}\n'
            f"into:\n"
            f'    "facilityRules": [\n'
            f'      {{ "match": {{"code": "CT"}}, "set": {{"rebate": {{…}}}} }},\n'
            f'      {{ "match": {{"code": "MT"}}, "set": {{"rebate": null}} }}\n'
            f"    ]\n"
            f'An explicit null still means "this code gets NO schedule, overriding the\n'
            f'scenario-wide overrides.facility.rebate" — in facilityRules, what counts is\n'
            f'the presence of "rebate" in "set", exactly as it did here.\n'
            f"Then record the facility world these rules target:\n"
            f"    openride scenario rules-baseline {slug}"
        )

    @classmethod
    def _validate_facility_rebate(
        cls, overrides: dict, rules: list, sites: list, *, slug: str,
        reference_time: str = "",
    ) -> None:
        """Judge the EFFECTIVE, post-rule-resolution set of facility rebate schedules.

        **Re-pointed by the facilityRules migration (plan §11.2), and this is the
        single most likely way this feature ships broken.** The previous version
        keyed off the two AUTHORED keys (``overrides.facility.rebate`` and
        ``.rebate_by_code``) and early-returned when both were absent. Migration
        makes both absent. Every check below would then have stopped running,
        silently and green, because the tests that exercised them authored the very
        key the migration removes. That is R2-3's defect shape exactly: a guard
        scoped by the presence of the treatment.

        What it now reads is ``site["rebate"]`` — what the run will actually price,
        whether it arrived via a rule or via the scenario-wide blanket override. By
        construction that cannot be disarmed by the authoring surface changing again.

        Per-point shape/value validation stays delegated to
        :func:`parse_rebate_schedule`; this method only owns the cross-facility
        questions that no single schedule can answer.
        """
        facility_overrides = (
            overrides.get("facility") if isinstance(overrides.get("facility"), dict) else {}
        )
        rebate_raw = facility_overrides.get("rebate")
        if rebate_raw is not None:
            try:
                parse_rebate_schedule(rebate_raw, where="overrides.facility.rebate")
            except RebateSpecError as exc:
                raise SpecValidationError(str(exc))

        # "Was pricing INTENDED?" — used only to decide whether silence deserves a
        # warning. Never used to decide whether the refusals below run.
        rules = list(rules or [])
        rule_authored = any(
            isinstance(r, dict)
            and isinstance(r.get("set"), dict)
            and r["set"].get("rebate") is not None
            for r in rules
        )
        authored = rebate_raw is not None or rule_authored

        # The effective set: what this run will actually price.
        effective = [(s, s.get("rebate")) for s in sites if s.get("rebate") is not None]

        if not effective:
            if authored:
                logging.warning(
                    "spec %r: a facility rebate was authored but NO facility resolves "
                    "to a schedule — check the rule matchers and values "
                    "(overrides.facility.rebate=%s, facilityRules setting 'rebate'=%d).",
                    slug,
                    "present" if rebate_raw is not None else "absent",
                    sum(1 for r in rules
                        if isinstance(r, dict) and isinstance(r.get("set"), dict)
                        and "rebate" in r["set"]),
                )
            return

        # --- the hour-axis collision (rebate plan §18.3, review F2) -------------
        # A rebate schedule is priced on the TRUE WALL CLOCK: `price_at` parses the
        # recorded RFC-1123 stamp. The order-demand curve is NOT — `sampling.py`
        # never reads the epoch, so its authored hour H is realised at wall hour
        # `(H + reference_hour) % 24`. At the historical 08:00 default the two axes
        # in one spec.json are EIGHT HOURS APART, which silently inverts what a
        # schedule means. This is not warned about, it is REFUSED.
        #
        # It now fires for a RULES-authored schedule too — see this method's
        # docstring. `test_midnight_epoch_guard_fires_for_a_rules_authored_schedule`
        # and mutation M10 are what keep that true.
        ref_hour = cls._reference_hour(reference_time)
        if ref_hour != 0:
            raise SpecValidationError(
                f"spec {slug!r}: a facility rebate schedule requires a midnight "
                f"simulation epoch, but reference_time is {reference_time!r} "
                f"(hour {ref_hour}). A rebate is priced on the true wall clock, while "
                f"the order-demand curve's authored hour H is realised at wall hour "
                f"(H + {ref_hour}) % 24 — so the two axes in this spec are {ref_hour} "
                f"hours apart and every schedule would mean something other than it "
                f"says. Fix: add \"referenceTime\": \"2020-01-01 00:00:00\" to "
                f"spec.json and recompile. (Generation is byte-identical under this "
                f"change; only wall-clock LABELS move — plan §18.3.)"
            )

        # --- R2-6 / review F5: mixed currency is unauthorable ---------------------
        # `RebateBook.currency` reduces a set of labels to ONE by taking the
        # alphabetically first, and the haulier ledger publishes a single
        # `rebate_currency` beside a single `rebate_credited`. So two currencies in one
        # bundle produce a scalar that silently sums unlike units and labels the total
        # with whichever name sorts first — a number nobody can audit, on money.
        currencies = sorted({
            str((blk or {}).get("currency"))
            for _site, blk in effective
            if isinstance(blk, dict) and blk.get("currency") is not None
        })
        if len(currencies) > 1:
            examples = {}
            for site, blk in effective:
                cur = str((blk or {}).get("currency"))
                examples.setdefault(cur, site.get("name") or site.get("code"))
            raise SpecValidationError(
                f"spec {slug!r}: rebate schedules resolve to {len(currencies)} different "
                f"currencies {currencies} across this scenario's facilities "
                f"(e.g. {', '.join(f'{c!r} at {examples[c]!r}' for c in currencies)}). "
                f"The haulier ledger publishes ONE signed total beside ONE currency "
                f"label, so mixed units would be summed together and labelled with "
                f"whichever name sorts first. There is no conversion and no FX in this "
                f"model — use a single currency label for the whole scenario."
            )

        # --- R2-6 companion / review F7: a flat schedule is not an incentive -------
        # A zero-variance schedule is a participation payment: it changes every
        # haulier's total by a constant and can never shift behaviour by hour, so an
        # author who believed they were creating a time-of-day incentive should be
        # told. Non-fatal: a flat schedule is a legitimate thing to want.
        for _digest, block in {
            json.dumps(blk, sort_keys=True): blk for _s, blk in effective
        }.items():
            amounts = [p.get("amount") for p in (block or {}).get("points") or []]
            if amounts and len(set(amounts)) == 1:
                logging.warning(
                    "spec %r: a rebate schedule is FLAT (every hour pays %s), so it is a "
                    "participation payment, not a time-of-day incentive — it shifts every "
                    "haulier's total by a constant and can never change behaviour by hour. "
                    "If a time-of-day effect was intended, vary the amounts.",
                    slug, amounts[0],
                )

    @classmethod
    def _resolve_order_matrix(cls, order_pol, otype, codes, scenario_dir, raw=None, records=None):
        if otype == "random":
            return _uniform_grid(codes)
        # 'historical' (given data): learn the OD matrix from records when a source was
        # supplied; otherwise use the authored matrix (policy.matrix / top-level tripMatrix,
        # else the default) — the old "matrix" default path, kept byte-identical.
        if records:
            try:
                return ProbabilityMatrix.from_records(records).restrict(codes).as_grid()
            except ValueError as exc:
                raise SpecValidationError(f"historical source produced no usable matrix: {exc}")
        raw_matrix = order_pol.get("matrix")
        if raw_matrix is None and isinstance(raw, dict):
            raw_matrix = raw.get("tripMatrix")
        matrix_raw = cls._resolve_file(raw_matrix, "matrix", scenario_dir)
        matrix_raw = matrix_raw or D.DEFAULT_TRIP_MATRIX
        try:
            return ProbabilityMatrix.from_weights(matrix_raw).restrict(codes).as_grid()
        except ValueError:
            return ProbabilityMatrix.from_weights(D.DEFAULT_TRIP_MATRIX).restrict(codes).as_grid()

    @staticmethod
    def _orsim_settings(domain, days, step_interval, sim_len, reference_time):
        from apps.container_logistics.scenario.runtime_settings import (
            apply_long_run_orsim_settings,
        )

        settings = {
            "DOMAIN": domain,
            "SIMULATION_LENGTH_IN_STEPS": sim_len,
            "STEP_INTERVAL": step_interval,
            "SIMULATION_DAYS": days,
            "REFERENCE_TIME": reference_time,
            **D.ORSIM_RUNTIME_TUNING,
        }
        return apply_long_run_orsim_settings(settings)
