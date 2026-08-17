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

import os
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Optional

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
        }

    @staticmethod
    def _normalize_market(raw_market) -> dict:
        """Shape-validate the planner's ``market`` sub-block; always emit it (defaulted).

        Validated: ``market`` is an object; ``offer``/``claim``/``arbitration`` are each
        an object with a string ``type`` and a dict ``params`` (missing -> the default);
        ``max_rounds`` is an int in ``1..10``. Algorithm NAMES are deliberately NOT
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
