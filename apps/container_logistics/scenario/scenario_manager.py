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
