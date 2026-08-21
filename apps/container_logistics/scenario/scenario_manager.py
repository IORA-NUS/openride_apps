import json
import logging
import os
from contextlib import nullcontext
from datetime import datetime

from apps.common.base_scenario_manager import BaseScenarioManager
from apps.orsim_config import orsim_settings as global_orsim_settings

from .frontend_scenario_spec import build_generation_spec_from_meta, scenario_meta_view
from .generate_behavior import GenerateBehavior
from .runtime_settings import apply_long_run_orsim_settings
from . import scenario_config
from .scenario_config import assignment_settings, build_orsim_settings
from .scenario_overrides import smoke_scenario_config_override
from .validation_config import build_smoke_orsim_settings


# Sentinel for the scenario-meta memo: ``None`` is a legitimate cached value (no meta at all).
_UNSET = object()


class ScenarioManager(BaseScenarioManager):
    """Loads or generates behaviors and ORSim settings for a container logistics run."""

    def __init__(self, datahub_dir, scenario_name, domain, *, generation_profile=None):
        self.truck_collection = None
        self.order_collection = None
        self.facility_collection = None
        self.assignment_collection = None
        self.analytics_collection = None
        self.order_lifecycle_collection = None
        self._orsim_settings = None
        self._scenario_meta_cache = _UNSET
        self.reference_time = datetime(2020, 1, 1, 8, 0, 0)
        self.generation_profile = generation_profile
        super().__init__(datahub_dir, scenario_name, domain)

    @property
    def behavior_dir(self):
        # Container-logistics scenarios are co-located with the ecosystem code at
        # apps/container_logistics/scenarios/<name> (overrides the base datahub path;
        # ridehail keeps the base dataset/ layout). Keeps the runtime read/write path
        # identical to frontend_scenario_spec.scenario_dir.
        from .frontend_scenario_spec import container_logistics_scenarios_root

        path = os.path.join(container_logistics_scenarios_root(), self.scenario_name)
        os.makedirs(path, exist_ok=True)
        return path

    def _read_scenario_meta(self) -> dict | None:
        # Bundle-aware: prefers scenario.json's recipe-derived meta, else legacy meta.
        # Memoized: this is consulted several times per load (frozen check, config match,
        # generation spec) and each miss is a FULL re-parse of scenario.json — multi-second and
        # hundreds of MB on large bundles. ``load_behaviors_from_disk`` seeds the memo from the
        # bundle it already parsed; ``_persist_bundle`` invalidates it after a rewrite.
        if self._scenario_meta_cache is _UNSET:
            self._scenario_meta_cache = scenario_meta_view(self.behavior_dir)
        return self._scenario_meta_cache

    def _generation_spec(self):
        if isinstance(self.orsim_settings, dict):
            spec = self.orsim_settings.get("GENERATION_SPEC")
            if isinstance(spec, dict):
                return spec
        return build_generation_spec_from_meta(self._read_scenario_meta())

    def _is_frontend_frozen_scenario(self) -> bool:
        spec = self._generation_spec()
        if spec and spec.get("source") == "frontend":
            return bool(spec.get("frozen", True))
        meta = self._read_scenario_meta()
        return isinstance(meta, dict) and meta.get("source") == "frontend"

    def get_scenario_display_name(self) -> str:
        spec = self._generation_spec()
        if spec and spec.get("name"):
            return str(spec["name"])
        meta_path = os.path.join(self.behavior_dir, "scenario_meta.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as fp:
                    meta = json.load(fp)
                if isinstance(meta, dict) and meta.get("name"):
                    return str(meta["name"])
            except (OSError, json.JSONDecodeError):
                pass
        return self.scenario_name

    def get_run_config_meta(self):
        spec = self._generation_spec()
        meta = {
            "num_truck_agents": len(self.get_agent_collection("truck")),
            "num_order_agents": len(self.get_agent_collection("order")),
            "num_facility_agents": len(self.get_agent_collection("facility")),
            "num_assignment_agents": len(self.get_agent_collection("assignment")),
            "num_analytics_agents": len(self.get_agent_collection("analytics")),
            "simulation_settings": self.orsim_settings,
            # Mirrored to the top level as well as riding simulation_settings, so a run
            # explains its own pricing without a reader knowing which channel carried
            # it (plan §9 / FIX-7).
            "rebate": self.orsim_settings.get("REBATE"),
            # Mirrored for the same reason as `rebate`: a run must be able to explain
            # its own facility setup without a reader knowing which channel carried
            # it. This matters more here than for pricing, because gate_count is
            # WORLD PHYSICS — an analyst comparing two runs must be able to SEE that
            # the gate counts differ rather than infer it from a KPI that moved.
            "facility_rules": self.orsim_settings.get("FACILITY_RULES"),
            "services": {
                "assignment_agents": self.get_agent_collection("assignment"),
                "analytics_agents": self.get_agent_collection("analytics"),
            },
        }
        if spec:
            meta["scenario_slug"] = spec.get("slug") or self.scenario_name
            meta["scenario_display_name"] = spec.get("name") or self.scenario_name
        else:
            meta["scenario_slug"] = self.scenario_name
            meta["scenario_display_name"] = self.scenario_name
        return meta

    _LEGACY_BEHAVIOR_FILES = (
        "truck_behavior.json",
        "order_behavior.json",
        "facility_behavior.json",
        "assignment_behavior.json",
        "analytics_behavior.json",
        "orsim_settings.json",
    )

    def behaviors_exist_on_disk(self):
        from .scenario_bundle import bundle_exists

        if bundle_exists(self.behavior_dir):
            return True
        return all(
            os.path.exists(os.path.join(self.behavior_dir, fname))
            for fname in self._LEGACY_BEHAVIOR_FILES
        )

    def load_behaviors_from_disk(self):
        from .frontend_scenario_spec import scenario_meta_from_bundle
        from .scenario_bundle import BUNDLE_FILENAME, bundle_exists, read_bundle, split_bundle

        if bundle_exists(self.behavior_dir):
            # New self-contained bundle: one scenario.json holds settings + the five
            # agent collections (+ optional later roles + recipe). This is the authoritative
            # run artifact — and it is parsed EXACTLY ONCE per run load (the file is
            # multi-hundred MB on large scenarios; every extra parse costs seconds and
            # hundreds of MB). Everything derived from it below reuses THIS dict.
            bundle = read_bundle(self.behavior_dir)
            if bundle is None:
                raise FileNotFoundError(f"No {BUNDLE_FILENAME} in {self.behavior_dir}")
            collections, settings, _recipe, extras = split_bundle(bundle, self.behavior_dir)
            # Seed the meta memo from the same parse (the frozen-scenario check and the
            # loaded-behaviors-match-config check both consult it during this very load).
            self._scenario_meta_cache = scenario_meta_from_bundle(bundle)
            self.truck_collection = collections["truck"]
            self.order_collection = collections["order"]
            self.facility_collection = collections["facility"]
            self.assignment_collection = collections["assignment"]
            self.analytics_collection = collections["analytics"]
            # Optional role (WP6) — {} for bundles predating order_lifecycle.
            self.order_lifecycle_collection = extras.get("order_lifecycle") or {}
            self.orsim_settings = settings
        else:
            # Back-compat: legacy six-file layout (un-migrated / other ecosystems).
            self._scenario_meta_cache = _UNSET
            self._load_legacy_behavior_files()

        self.orsim_settings = apply_long_run_orsim_settings(self.orsim_settings)
        self._sync_runtime_tuning_from_config()
        if self.orsim_settings.get("REFERENCE_TIME") is not None:
            self.reference_time = datetime.strptime(
                self.orsim_settings.get("REFERENCE_TIME"), "%Y-%m-%d %H:%M:%S"
            )
        self._sync_collections()

    def _load_legacy_behavior_files(self):
        with open(f"{self.behavior_dir}/truck_behavior.json", "r") as fp:
            self.truck_collection = json.load(fp)
        with open(f"{self.behavior_dir}/order_behavior.json", "r") as fp:
            self.order_collection = json.load(fp)
        with open(f"{self.behavior_dir}/facility_behavior.json", "r") as fp:
            self.facility_collection = json.load(fp)
        with open(f"{self.behavior_dir}/assignment_behavior.json", "r") as fp:
            self.assignment_collection = json.load(fp)
        with open(f"{self.behavior_dir}/analytics_behavior.json", "r") as fp:
            self.analytics_collection = json.load(fp)
        # Legacy six-file layout predates order_lifecycle; no file to load.
        self.order_lifecycle_collection = {}
        with open(f"{self.behavior_dir}/orsim_settings.json", "r") as fp:
            self.orsim_settings = json.load(fp)

    def _persist_bundle(self):
        """Write the scenario's single ``scenario.json`` bundle from current state."""
        from .scenario_bundle import compile_bundle, write_bundle

        recipe = self._generation_spec()
        slug = (recipe or {}).get("slug") or self.scenario_name
        name = (recipe or {}).get("name") or self.scenario_name
        source = (recipe or {}).get("source") or "generated"
        bundle = compile_bundle(
            {
                "truck": self.truck_collection,
                "order": self.order_collection,
                "facility": self.facility_collection,
                "assignment": self.assignment_collection,
                "analytics": self.analytics_collection,
                "order_lifecycle": self.order_lifecycle_collection,
            },
            self.orsim_settings,
            recipe,
            domain=self.domain,
            slug=slug,
            name=name,
            source=source,
            behavior_revision=scenario_config.BEHAVIOR_REVISION,
            generator_override_sha256=getattr(self, "_generator_override_sha256", None),
        )
        write_bundle(self.behavior_dir, bundle)
        # The on-disk bundle just changed; any memoized meta view derived from the old one
        # is stale. Next read re-derives from disk.
        self._scenario_meta_cache = _UNSET

    def _sync_collections(self):
        self.collections["truck"] = self.truck_collection
        self.collections["order"] = self.order_collection
        self.collections["facility"] = self.facility_collection
        self.collections["assignment"] = self.assignment_collection
        self.collections["analytics"] = self.analytics_collection
        self.collections["order_lifecycle"] = self.order_lifecycle_collection

    def generate_random_behaviors(self):
        # GenerateBehavior reads `orsim_settings["DOMAIN"]` from the shared module dict.
        global_orsim_settings["DOMAIN"] = self.domain
        spec = self._generation_spec()
        if self.generation_profile == "smoke":
            ctx = smoke_scenario_config_override()
        elif spec:
            from .frontend_scenario_spec import frontend_scenario_config_override, normalize_generate_spec

            normalized = normalize_generate_spec(
                {
                    "name": spec.get("name") or self.scenario_name,
                    "slug": spec.get("slug") or self.scenario_name,
                    "simulationDays": spec.get("simulationDays", 1),
                    "agents": spec.get("agents") or {},
                    "earlyOrderCount": spec.get("earlyOrderCount"),
                    "orderDemandCurve": spec.get("orderDemandCurve"),
                    "roleSettings": spec.get("roleSettings"),
                    # THE FOURTH CLOSED LITERAL (R2-2 / F3). This seven-key dict
                    # dropped facilityRules, so the legacy generation path produced a
                    # complete, internally consistent, RULES-FREE world with no error
                    # — 300 facilities at one gate each — and build_generation_spec
                    # had no value to object to. The literal must be fixed BEFORE the
                    # refusal downstream, or the refusal ships and does nothing.
                    "facilityRules": spec.get("facilityRules"),
                    "facilityRulesWorld": spec.get("facilityRulesWorld"),
                }
            )
            ctx = frontend_scenario_config_override(normalized)
        else:
            ctx = nullcontext()
        with ctx:
            self._generate_random_behaviors_body()

    def _generate_random_behaviors_body(self):
        expected = self._expected_behavior_counts()
        preserve = dict(self.orsim_settings) if isinstance(self.orsim_settings, dict) else None
        ref_time = (
            (preserve or {}).get("REFERENCE_TIME")
            or global_orsim_settings.get("REFERENCE_TIME", "2020-01-01 08:00:00")
        )
        if self.generation_profile == "smoke":
            self.orsim_settings = build_smoke_orsim_settings(domain=self.domain, reference_time=ref_time)
        elif preserve and self._generation_spec():
            self.orsim_settings = apply_long_run_orsim_settings(preserve)
        elif preserve and int(expected["trucks"]) == len(self.truck_collection or {}):
            self.orsim_settings = apply_long_run_orsim_settings(preserve)
        else:
            self.orsim_settings = build_orsim_settings(domain=self.domain, reference_time=ref_time)
        global_orsim_settings["DOMAIN"] = self.domain
        global_orsim_settings["SIMULATION_LENGTH_IN_STEPS"] = self.orsim_settings[
            "SIMULATION_LENGTH_IN_STEPS"
        ]
        global_orsim_settings["STEP_INTERVAL"] = self.orsim_settings["STEP_INTERVAL"]

        # Generate all behaviors through the isolated datagen package: resolve a
        # single immutable spec from the (post-override) scenario_config, then
        # sample real locations + build the agent dicts. ScenarioManager keeps
        # ownership of the run lifecycle (orsim settings, early-order staggering,
        # file persistence) below.
        from apps.container_logistics.datagen import ScenarioGenerator
        from apps.container_logistics.datagen.overrides import (
            load_scenario_overrides,
            override_sha256,
        )
        from .scenario_datagen import build_generation_spec

        spec = build_generation_spec(
            self.domain,
            counts=expected,
            reference_time=self.orsim_settings.get("REFERENCE_TIME", ref_time),
        )
        # Optional per-scenario datagen override (scenario_gen.py in the scenario
        # folder). Absent => pure shared-engine generation. Its hash is recorded in
        # the bundle's integrity block for reproducibility.
        overrides = load_scenario_overrides(self.behavior_dir)
        self._generator_override_sha256 = override_sha256(self.behavior_dir)
        result = ScenarioGenerator(spec, overrides=overrides).generate()
        self.truck_collection = result.truck
        self.order_collection = result.order
        self.facility_collection = result.facility
        self.assignment_collection = result.assignment
        self.analytics_collection = result.analytics
        self.order_lifecycle_collection = getattr(result, "order_lifecycle", {}) or {}

        self._stagger_early_orders()

        self.orsim_settings["BEHAVIOR_REVISION"] = scenario_config.BEHAVIOR_REVISION
        generation_spec = self._generation_spec()
        if generation_spec:
            self.orsim_settings["GENERATION_SPEC"] = generation_spec
        if self.orsim_settings.get("REFERENCE_TIME") is not None:
            self.reference_time = datetime.strptime(
                self.orsim_settings.get("REFERENCE_TIME"), "%Y-%m-%d %H:%M:%S"
            )
        self._sync_collections()
        self._sync_runtime_tuning_from_config()
        # Persist AFTER runtime tuning so the single self-contained scenario.json
        # (settings + the five agent collections + recipe) is exactly what runs —
        # replaces the old six loose behavior files. (load reapplies tuning idempotently.)
        self._persist_bundle()

    def _stamp_use_osrm_at_assignment(self) -> None:
        """Stamp the OSRM-at-assignment flag onto every truck profile.

        Deliberately applied to EVERY scenario, frozen or not. `use_osrm_at_assignment`
        decides where route geometry comes from — a runtime/infrastructure concern, not
        authored scenario content like agent counts or step interval, which is what the
        frozen-bundle guard exists to protect.

        This distinction is load-bearing:
        every compiled frontend bundle bakes `use_osrm_at_assignment: false` (they were
        generated before routes were stored at assignment), and a bundle's top-level
        `source: "frontend"` makes `_is_frontend_frozen_scenario()` true — which is the
        case for the standard verification scenario. Stamping inside the frozen guard
        therefore made the flag a silent no-op on exactly the scenarios we run, leaving
        `routes.planned[leg]` null and replay with no geometry at all.
        """
        from .runtime_settings import USE_OSRM_AT_ASSIGNMENT

        use_osrm = bool(self.orsim_settings.get("USE_OSRM_AT_ASSIGNMENT", USE_OSRM_AT_ASSIGNMENT))

        # Make the RUN self-describing. Three places used to be able to disagree, which
        # matters because "old runs are the ones without stored routes" is the premise for
        # ever deleting anything: the effective value must be readable from the run's own
        # record, not inferred.
        #   1. orsim_settings — recorded verbatim into run_config.meta.simulation_settings,
        #      and read by the analytics agent to decide whether stored routes are
        #      authoritative (see `ContainerTripGeoPublisher`).
        self.orsim_settings["USE_OSRM_AT_ASSIGNMENT"] = use_osrm
        #   2. the GENERATION_SPEC override, which is snapshotted into run_config and still
        #      said False on every compiled bundle while the run actually used True.
        spec = self.orsim_settings.get("GENERATION_SPEC")
        if isinstance(spec, dict):
            overrides = spec.get("overrides")
            if isinstance(overrides, dict):
                truck_overrides = overrides.get("truck")
                if isinstance(truck_overrides, dict):
                    truck_overrides["use_osrm_at_assignment"] = use_osrm
        #   3. the truck behavior profiles, which are what the agents actually read.
        if not self.truck_collection:
            return
        for behavior in self.truck_collection.values():
            behavior.setdefault("profile", {})["use_osrm_at_assignment"] = use_osrm

    def _sync_runtime_tuning_from_config(self) -> None:
        """Align cached behaviors with scenario_config (step size, OSRM, cadence)."""
        # Runs for frozen scenarios too — see the docstring above.
        self._stamp_use_osrm_at_assignment()

        if self._is_frontend_frozen_scenario():
            # A frozen bundle keeps the settings it declares, so they are ALREADY the
            # effective ones — stamp here and return.
            self._stamp_rebate_provenance()
            self._stamp_facility_rules_provenance()
            return

        self.orsim_settings["STEP_INTERVAL"] = scenario_config.STEP_INTERVAL_SECONDS
        self.orsim_settings["SIMULATION_DAYS"] = scenario_config.SIMULATION_DAYS
        self.orsim_settings["SIMULATION_LENGTH_IN_STEPS"] = scenario_config.simulation_length_in_steps()
        self.orsim_settings = apply_long_run_orsim_settings(self.orsim_settings)

        agent_tuning = (
            ("truck", self.truck_collection, scenario_config.truck_settings),
            ("order", self.order_collection, scenario_config.order_settings),
            ("facility", self.facility_collection, scenario_config.facility_settings),
            ("assignment", self.assignment_collection, scenario_config.assignment_settings),
            ("analytics", self.analytics_collection, scenario_config.analytics_settings),
        )
        for _role, collection, settings in agent_tuning:
            if not collection:
                continue
            for behavior in collection.values():
                behavior["steps_per_action"] = settings.get("steps_per_action", 1)
                if "dormant_steps_per_action" in settings:
                    behavior["dormant_steps_per_action"] = settings["dormant_steps_per_action"]
                if "step_only_on_events" in settings:
                    behavior["step_only_on_events"] = settings["step_only_on_events"]

        # AFTER the tuning block, deliberately: the non-frozen path rewrites
        # STEP_INTERVAL a few lines above, and a stamp taken before that would record
        # the pre-tuning value while claiming to be effective. Plan §9 is blunt that a
        # stamp recording the authored intent, or stopping early, is worse than none,
        # because it reads exact.
        self._stamp_rebate_provenance()
        self._stamp_facility_rules_provenance()

    def _hour_axis_stamp(self) -> dict:
        """Which clock each of the two curves is measured on (plan §18.3 step 1)."""
        # R3-4: the SHARED epoch rule. This used to be an independent second
        # derivation, and the two disagreed on 4 of 7 realistic epoch spellings — so
        # the compiled bundle's recipe.hour_axis and this run record's
        # meta.rebate.hour_axis could carry contradictory `axes_aligned` values for the
        # very same run. One rule, one function.
        from apps.container_logistics.rebate import reference_hour

        ref = self.orsim_settings.get("REFERENCE_TIME")
        hour = reference_hour(ref)
        return {
            "orderDemandCurve": (
                "hours since REFERENCE_TIME (sampling.py never reads the epoch, so "
                "authored hour H is realised at wall hour (H + reference_hour) % 24)"
            ),
            "rebate": (
                "sim wall clock; epoch = REFERENCE_TIME (price_at parses the recorded "
                "RFC-1123 arrival stamp)"
            ),
            "reference_hour": hour,
            "demand_to_wall_offset_hours": hour,
            # False means every rebate band in this run sits `reference_hour` hours away
            # from the authored demand hour it appears to target.
            "axes_aligned": hour == 0,
        }

    def _stamp_rebate_provenance(self) -> None:
        """Record what this run actually prices, from the EFFECTIVE compiled facilities.

        Plan §9. Written into ``orsim_settings["REBATE"]``, the established channel for
        a scenario-derived provenance block (the ``COOPERATION`` and
        ``USE_OSRM_AT_ASSIGNMENT`` precedents): it lands verbatim in
        ``run_config.meta.simulation_settings.REBATE`` — ``run_config.meta`` has no
        sub-schema, so no migration and no api container rebuild — and it ships to
        every Celery agent.

        Three things this is NOT:

        * It is **not** the audit's P8 defect. P8 is bad because a global constant
          *overrides a scenario's own declaration*. This stamp is derived FROM the
          scenario's compiled facilities and overrides nothing — it is a description,
          not an input. **Nothing downstream may read it to make a pricing decision**;
          the per-facility ``profile.rebate`` block is the single source.
        * It is **not** read from ``spec.json``. An operator who hand-patches a compiled
          bundle (``scratch_p4_portgates8`` proves they do, and its own note admits a
          recompile would discard the patch) must still get a stamp describing what
          actually ran.
        * It is **not** per-facility. 300 inline copies of one curve is how a stamp
          becomes unreadable, so identical schedules collapse by digest, with a count
          and an example facility to make the claim checkable.

        ``reference_time`` and ``step_interval_seconds`` are stamped specifically
        because the tree carries three conflicting epochs (04:00, 08:00, 00:00). A
        future reader asking "which hour did 14:00 mean in this run?" must be able to
        answer it from the run record.

        Never raises: a provenance stamp must not be able to fail a run.
        """
        from apps.container_logistics.rebate import (
            RebateSpecError,
            parse_rebate_schedule,
            schedules_by_digest,
        )

        try:
            facilities = getattr(self, "facility_collection", None) or {}
            entries = []
            unparseable = 0
            for agent_id, behavior in facilities.items():
                if not isinstance(behavior, dict):
                    continue
                profile = behavior.get("profile") or {}
                block = profile.get("rebate")
                if not block:
                    continue
                try:
                    schedule = parse_rebate_schedule(
                        block, where=f"{agent_id}.profile.rebate"
                    )
                except RebateSpecError as exc:
                    unparseable += 1
                    logging.warning(
                        "facility %s carries an unparseable rebate block (%s); it is "
                        "counted in the run stamp but will price nothing", agent_id, exc,
                    )
                    continue
                entries.append((
                    str(profile.get("name") or agent_id),
                    profile.get("facility_type"),
                    schedule,
                ))

            grouped = schedules_by_digest(entries)
            currencies = sorted({s.currency for _n, _t, s in entries})
            stamp = {
                # Stamped explicitly when nothing resolved, so "no rebates" and
                # "rebates silently lost" stay distinguishable in the run record.
                # That distinction is the whole point of stamping at all.
                "enabled": bool(entries),
                "currency": (currencies[0] if len(currencies) == 1 else currencies) or None,
                "reference_time": self.orsim_settings.get("REFERENCE_TIME"),
                "step_interval_seconds": self.orsim_settings.get("STEP_INTERVAL"),
                # --- which clock each curve speaks (plan §18.3, review F2) --------
                # The rebate curve and the order-demand curve in the same spec.json are
                # measured on DIFFERENT clocks: `price_at` reads the true wall clock,
                # while `sampling.py` never reads the epoch, so an authored demand hour
                # H is realised at wall hour (H + reference_hour) % 24. Stamped on the
                # RUN so an existing result is self-describing about the offset it was
                # produced under — run_20260821_045528 was inverted by exactly this and
                # nothing in its record said so.
                "hour_axis": self._hour_axis_stamp(),
                "total_facilities": len(facilities),
                "facilities_with_schedule": len(entries),
                "facilities_with_unparseable_schedule": unparseable,
                "schedules": grouped,
            }
            if entries:
                # Plan §16.1, recorded here rather than left in a doc nobody reads:
                # the laden leg takes ZERO sim time — re-verified 2026-08-21 on
                # 785/785 completed trips of run_20260817_164156, where
                # loaded_started_at == dropoff_queue_arrival_time. So a dropoff
                # arrival hour is NOT an independent time signal; it is determined
                # entirely by pickup-side queueing. Settlement is correct given the
                # timestamps, but no behavioural conclusion should be drawn from a
                # dropoff-side rebate result until that is resolved.
                stamp["caveats"] = [
                    "dropoff_arrival_hour_is_not_independent: the laden leg takes zero "
                    "sim time (loaded_started_at == dropoff_queue_arrival_time), so a "
                    "dropoff arrival is priced on a clock determined by pickup-side "
                    "queueing. Pickup-side results are unaffected."
                ]
            else:
                logging.warning(
                    "Scenario %r: no compiled facility carries a rebate schedule, so "
                    "meta.rebate.enabled=false. If a schedule was authored, it was lost "
                    "between spec.json and the compiled bundle.", self.scenario_name,
                )
            self.orsim_settings["REBATE"] = stamp
        except Exception:  # pragma: no cover - a stamp must never break a run
            logging.debug("rebate provenance stamp failed", exc_info=True)

    def _authored_facility_rules(self):
        """``(rules, source)`` — the authored rule list, or ``([], "unavailable")``.

        Used ONLY to attribute a compiled value to the rule that produced it. The
        stamp's value distribution is derived from the compiled facilities and never
        from this, because people hand-patch bundles and the stamp must describe what
        actually ran.
        """
        spec = self._generation_spec()
        if isinstance(spec, dict) and isinstance(spec.get("facilityRules"), list):
            return spec["facilityRules"], "GENERATION_SPEC"
        meta = self._read_scenario_meta()
        if isinstance(meta, dict) and isinstance(meta.get("facilityRules"), list):
            return meta["facilityRules"], "scenario.json:$.recipe"
        return [], "unavailable"

    @staticmethod
    def _facility_rules_audit(rules, matched_counts) -> list:
        """Per-rule audit rows, tolerant of a malformed authored rule.

        A rule that is not even shaped like one is reported as unparseable rather
        than skipped: "there is a rule here I could not read" is information, and
        dropping the row would make the list silently disagree with ``rule_count``.
        """
        rows = []
        for i, rule in enumerate(rules):
            row = {"index": i, "matched": matched_counts[i]}
            if isinstance(rule, dict) and isinstance(rule.get("match"), dict):
                row["match"] = dict(rule["match"])
            else:
                row["match"] = None
                row["unparseable"] = repr(rule)[:120]
            setter = rule.get("set") if isinstance(rule, dict) else None
            row["set_keys"] = sorted(setter.keys()) if isinstance(setter, dict) else []
            rows.append(row)
        return rows

    def _stamp_facility_rules_provenance(self) -> None:
        """Record the EFFECTIVE per-facility configuration this run will use.

        Facility rules plan §10, modelled on :meth:`_stamp_rebate_provenance` and
        written to the same channel: ``orsim_settings["FACILITY_RULES"]`` lands
        verbatim in ``run_config.meta.simulation_settings`` (which has no sub-schema,
        so no migration and no api container rebuild) and ships to every Celery agent.

        Four properties, each preventing a named failure:

        1. **Derived from the compiled facility collection, never the authored spec.**
           People hand-patch bundles; the stamp must describe what ran. A hand-patched
           value therefore shows up as a group with ``source: null`` — no authored
           rule explains it — which is strictly more informative than a stamp that
           confidently repeats the spec.
        2. **Stamped from BOTH branches** of ``_sync_runtime_tuning_from_config``.
           The frozen predicate is ``False`` for the flagship bundle even though its
           recipe says ``frozen: true``, so relying on either branch alone is a coin
           flip.
        3. **Collapsed by value, with a per-rule audit alongside.** 300 inline copies
           is how a stamp becomes unreadable. ``rules_applied`` is the audit
           ``resolved`` cannot give: it shows each rule actually bit, and how hard —
           a rule reporting ``matched: 6`` beside a value group of 5 IS a more
           specific rule winning on one facility, and seeing that is the point.
        4. **``enabled: false`` stamped explicitly** when nothing resolved, so
           "no rules" and "rules silently lost" stay distinguishable.

        Self-check: for every key, the group counts must sum to the facility count.
        A resolver that drops a facility otherwise produces a stamp that looks fine.

        Never raises: a provenance stamp must not be able to fail a run.
        """
        import hashlib

        try:
            from apps.container_logistics.datagen import defaults as D
            from apps.container_logistics.datagen import facility_rules as FR

            facilities = getattr(self, "facility_collection", None) or {}
            rules, rules_source = self._authored_facility_rules()
            index = FR.code_index(D.LOCATION_TYPE_METADATA)

            def _label(key, value):
                if value is None:
                    return "none"
                if key == "rebate":
                    blob = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
                    return "blake2b:" + hashlib.blake2b(blob, digest_size=8).hexdigest()
                return str(value)

            # Which rule (if any) explains this facility's value for this key.
            def _attribute(profile, code, key, value):
                best_rank, best_i = None, None
                for i, rule in enumerate(rules):
                    if not isinstance(rule, dict) or not isinstance(rule.get("set"), dict):
                        continue
                    if key not in rule["set"]:
                        continue
                    site = {"name": profile.get("name"), "code": code}
                    try:
                        if not FR.rule_matches(rule, site):
                            continue
                        rank = FR.rule_rank(rule)
                    except Exception:
                        continue
                    if best_rank is None or rank > best_rank:
                        best_rank, best_i = rank, i
                if best_i is None:
                    return None
                authored = rules[best_i]["set"][key]
                # Only claim the rule produced this value if it actually matches what
                # the bundle carries. A hand-patched bundle must not be attributed.
                if key == "rebate":
                    matches = (authored is None) == (value is None)
                else:
                    matches = authored == value
                if not matches:
                    return None
                return FR.describe_rule(best_i, rules[best_i])

            resolved: dict = {}
            matched_counts = [0] * len(rules)
            for agent_id, behavior in facilities.items():
                if not isinstance(behavior, dict):
                    continue
                profile = behavior.get("profile") or {}
                code = FR.code_for_compiled_facility(profile, index)
                site = {"name": profile.get("name"), "code": code}
                for i, rule in enumerate(rules):
                    try:
                        if isinstance(rule, dict) and FR.rule_matches(rule, site):
                            matched_counts[i] += 1
                    except Exception:
                        pass
                for key in ("gate_count", "service_time", "rebate"):
                    value = profile.get(key)
                    group = resolved.setdefault(key, {}).setdefault(
                        _label(key, value),
                        {"facility_count": 0, "codes": set(), "example": None,
                         "source": _attribute(profile, code, key, value)},
                    )
                    group["facility_count"] += 1
                    if code:
                        group["codes"].add(code)
                    if group["example"] is None:
                        group["example"] = profile.get("name") or agent_id

            total = len(facilities)
            partition_ok = True
            for key, groups in resolved.items():
                for group in groups.values():
                    group["codes"] = sorted(group["codes"])
                if sum(g["facility_count"] for g in groups.values()) != total:
                    partition_ok = False
                    logging.warning(
                        "Scenario %r: FACILITY_RULES stamp does not partition the "
                        "facility collection for %r — a facility was dropped between "
                        "resolution and the stamp.", self.scenario_name, key,
                    )

            # --- physics_digest (R2-4 / F4) --------------------------------------
            # site_digest CANNOT serve as a comparability key and this is measured, not
            # argued: rebate_ports_500_trucks (gate_count 1) and port_gates_500_trucks
            # (gate_count 4) carry the IDENTICAL site_digest blake2b16:86bfd8764d6d9d46.
            # That is not a bug in the fingerprint — it digests (name, code, lat, lon)
            # because its question is "do the rules still target what they targeted".
            # Identity and physics are different questions and need different digests.
            #
            # Over {key: {value_label: facility_count}} only: `example` and `source` are
            # excluded so the digest is STABLE under a facility rename and SENSITIVE to
            # a value or population change. It covers BOTH layers, because `resolved`
            # already carries rank-0 groups — so a blanket-only gate_count edit, with no
            # rules at all, is caught too.
            physics_payload = {
                key: {label: grp["facility_count"] for label, grp in sorted(groups.items())}
                for key, groups in sorted(resolved.items())
            }
            physics_digest = "blake2b16:" + hashlib.blake2b(
                json.dumps(physics_payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                digest_size=8,
            ).hexdigest()

            stamp = {
                "enabled": bool(rules),
                "rule_count": len(rules),
                "facility_count": total,
                "physics_digest": physics_digest,
                "rules_source": rules_source,
                "partitions_every_facility": partition_ok,
                "resolved": resolved,
                # Built in its own try: `resolved` is the load-bearing half (it is
                # what the run actually runs on) and a malformed authored rule must
                # never be able to cost us it. Losing the audit is a degradation;
                # losing the stamp entirely leaves a run unable to describe itself,
                # which is the failure this whole block exists to prevent.
                "rules_applied": self._facility_rules_audit(rules, matched_counts),
            }
            if not rules and rules_source == "unavailable":
                # Distinguishable from "this scenario has no rules": the difference
                # matters when a value group comes back unattributed.
                stamp["note"] = (
                    "no authored rule list was reachable from this run's scenario "
                    "record; the resolved values above still describe what ran"
                )
            self.orsim_settings["FACILITY_RULES"] = stamp
        except Exception:  # pragma: no cover - a stamp must never break a run
            logging.debug("facility rules provenance stamp failed", exc_info=True)

    def _stagger_early_orders(self) -> None:
        """Place a small warm-up batch in sim hour 0; must not override demand-curve sampling."""
        from .order_demand import recommended_early_order_count

        early_n = int(
            scenario_config.order_settings.get(
                "early_order_count",
                scenario_config.EARLY_ORDER_COUNT,
            )
        )
        truck_n = len(self.truck_collection or {})
        order_n = len(self.order_collection or {})
        if truck_n > 0 and order_n > 0:
            early_n = min(early_n, recommended_early_order_count(truck_n, order_n))
        if early_n <= 0 or not self.order_collection:
            return
        interval = int(self.orsim_settings.get("STEP_INTERVAL", scenario_config.STEP_INTERVAL_SECONDS))
        steps_per_hour = max(1, 3600 // interval)
        sim_end = int(self.orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", 0))
        window = min(steps_per_hour, sim_end + 1)
        ordered_ids = sorted(
            self.order_collection.keys(),
            key=lambda k: int(self.order_collection[k].get("request_time_step", 0)),
        )
        for i, agent_id in enumerate(ordered_ids[: min(early_n, len(ordered_ids))]):
            step = min(int(i * window / max(1, early_n)), sim_end)
            self.order_collection[agent_id]["request_time_step"] = step

    def _expected_behavior_counts(self):
        spec = self._generation_spec()
        if spec and isinstance(spec.get("agents"), dict):
            agents = spec["agents"]
            truck = agents.get("truck") if isinstance(agents.get("truck"), dict) else {}
            order = agents.get("order") if isinstance(agents.get("order"), dict) else {}
            facility = agents.get("facility") if isinstance(agents.get("facility"), dict) else {}
            return {
                "trucks": int(truck.get("count", 0)),
                "orders": int(order.get("count", 0)),
                "facilities": int(facility.get("count", 0)),
            }
        facilities = GenerateBehavior._get_facilities()
        return {
            "trucks": scenario_config.truck_settings["num_trucks"],
            "orders": scenario_config.order_settings["num_orders"],
            "facilities": len(facilities),
        }

    def _loaded_behaviors_match_config(self) -> bool:
        expected = self._expected_behavior_counts()
        counts_ok = (
            len(self.truck_collection or {}) == expected["trucks"]
            and len(self.order_collection or {}) == expected["orders"]
            and len(self.facility_collection or {}) == expected["facilities"]
        )
        if not counts_ok:
            return False

        if self._is_frontend_frozen_scenario():
            spec = self._generation_spec()
            if spec and spec.get("simulationDays") is not None:
                return int(self.orsim_settings.get("SIMULATION_DAYS", 0)) == int(spec["simulationDays"])
            return True

        revision_ok = int((self.orsim_settings or {}).get("BEHAVIOR_REVISION", 0)) == int(
            scenario_config.BEHAVIOR_REVISION
        )
        interval_ok = int((self.orsim_settings or {}).get("STEP_INTERVAL", 0)) == int(
            scenario_config.STEP_INTERVAL_SECONDS
        )
        return revision_ok and interval_ok

    def load_or_generate_behaviors(self):
        processed_input_dir = os.path.join(
            self.datahub_dir, self.domain, "processed_input", self.scenario_name
        )
        logging.info(
            "Checking for existing behaviors on disk for scenario %s in %s",
            self.scenario_name,
            self.behavior_dir,
        )
        if self.behaviors_exist_on_disk():
            logging.info("Found existing behaviors on disk for scenario %s. Loading...", self.scenario_name)
            logging.warning(
                "Loading scenario behaviors %s from disk in %s", self.scenario_name, self.behavior_dir
            )
            self.load_behaviors_from_disk()
            if not self._loaded_behaviors_match_config():
                expected = self._expected_behavior_counts()
                logging.info(
                    "Cached behaviors do not match scenario spec "
                    "(on disk: trucks=%s, orders=%s, facilities=%s; expected: %s); regenerating...",
                    len(self.truck_collection),
                    len(self.order_collection),
                    len(self.facility_collection),
                    expected,
                )
                logging.warning(
                    "Regenerating behaviors for %s — cached agent counts out of date vs scenario spec",
                    self.scenario_name,
                )
                self.generate_random_behaviors()
        elif os.path.exists(processed_input_dir):
            raise NotImplementedError(
                "Container logistics scenarios from processed_input are not implemented yet; "
                f"remove {processed_input_dir} or populate behaviors under {self.behavior_dir}."
            )
        else:
            logging.info(
                "No existing behaviors or processed input data found for scenario %s. Generating random behaviors...",
                self.scenario_name,
            )
            logging.warning("Generating a scenario with random behaviors for %s", self.scenario_name)
            self.generate_random_behaviors()
