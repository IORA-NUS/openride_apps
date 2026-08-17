"""Orchestrates sampling + building into the per-scenario behavior collections.

``ScenarioGenerator(spec).generate()`` is pure (no I/O); ``GenerationResult.write``
is the only thing that touches the filesystem. Both are driveable with nothing
but a :class:`GenerationSpec` — no Kafka/Celery/Mongo/engine — which is what makes
data generation runnable and testable in isolation.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from typing import Optional

from .builders import (
    AnalyticsBuilder,
    AssignmentBuilder,
    FacilityBuilder,
    OrderBuilder,
    TruckBuilder,
)
from .catalog import LocationCatalog
from .spec import GenerationSpec

_BEHAVIOR_FILES = {
    "truck": "truck_behavior.json",
    "order": "order_behavior.json",
    "facility": "facility_behavior.json",
    "assignment": "assignment_behavior.json",
    "analytics": "analytics_behavior.json",
    "order_lifecycle": "order_lifecycle_behavior.json",
}

# Default builder class per role. A per-scenario override may replace any of these
# via a ``BUILDERS`` dict (see datagen/overrides.py).
DEFAULT_BUILDERS = {
    "truck": TruckBuilder,
    "order": OrderBuilder,
    "facility": FacilityBuilder,
    "assignment": AssignmentBuilder,
    "analytics": AnalyticsBuilder,
}


@dataclass
class GenerationResult:
    truck: dict
    order: dict
    facility: dict
    assignment: dict
    analytics: dict
    order_lifecycle: dict = field(default_factory=dict)
    orsim_settings: Optional[dict] = None

    def collections(self) -> dict:
        return {
            "truck": self.truck,
            "order": self.order,
            "facility": self.facility,
            "assignment": self.assignment,
            "analytics": self.analytics,
            "order_lifecycle": self.order_lifecycle,
        }

    def write(self, behavior_dir: str) -> None:
        os.makedirs(behavior_dir, exist_ok=True)
        for role, collection in self.collections().items():
            with open(os.path.join(behavior_dir, _BEHAVIOR_FILES[role]), "w") as fp:
                json.dump(collection, fp, indent=4, sort_keys=True)
        if self.orsim_settings is not None:
            with open(os.path.join(behavior_dir, "orsim_settings.json"), "w") as fp:
                json.dump(self.orsim_settings, fp, indent=4, sort_keys=True)


class ScenarioGenerator:
    def __init__(
        self,
        spec: GenerationSpec,
        catalog: Optional[LocationCatalog] = None,
        rng=None,
        overrides=None,
    ):
        self.spec = spec
        self.catalog = catalog or LocationCatalog(
            spec.locations_csv, spec.sg_mask_path, excluded=spec.excluded_codes
        )
        self.rng = rng or random
        # Optional per-scenario override module (see datagen/overrides.py). None =
        # pure shared-engine generation (byte-identical to before this feature).
        self.overrides = overrides

    @staticmethod
    def _hauliers(spec: GenerationSpec, role: str, n: int):
        configured = spec.truck_hauliers if role == "truck" else spec.order_hauliers
        if configured and len(configured) >= n:
            return configured
        default = {"id": "haulier", "name": "Haulier"}
        base = list(configured) if configured else [default]
        return [base[i % len(base)] for i in range(n)]

    def _hook(self, name: str):
        """Return a callable override hook by name, or None."""
        fn = getattr(self.overrides, name, None) if self.overrides is not None else None
        return fn if callable(fn) else None

    @staticmethod
    def _role_rng(seed: int, role: str) -> random.Random:
        """Independent, deterministic per-role RNG (plan §4.6 seed propagation).

        Using a string seed keeps this stable across runs/platforms, so editing one
        role's policy never perturbs another role's data.
        """
        return random.Random(f"{seed}:{role}")

    def generate(self) -> GenerationResult:
        from .agents import PolicyContext, agent_class, resolve_policy

        spec = self.spec
        catalog = self.catalog

        # --- per-scenario override seams (Tier-2, optional/deprecated) ---
        customize_spec = self._hook("customize_spec")
        if customize_spec is not None:
            spec = customize_spec(spec) or spec
        customize_catalog = self._hook("customize_catalog")
        if customize_catalog is not None:
            catalog = customize_catalog(catalog, spec) or catalog

        seed = int(getattr(spec, "seed", 20260712))
        counts = {
            "truck": max(1, int(spec.num_trucks)),
            "order": max(1, int(spec.num_orders)),
            "facility": max(1, int(spec.num_facilities)),
            "assignment": 1,
            "analytics": 1,
            "order_lifecycle": 1,
        }
        role_hauliers = {
            "truck": list(spec.truck_hauliers) or self._hauliers(spec, "truck", counts["truck"]),
            "order": list(spec.order_hauliers) or self._hauliers(spec, "order", counts["order"]),
        }
        policies_cfg = getattr(spec, "role_policies", None) or {}

        # One PolicyContext (facilities resolved once). Facility first (trucks/orders
        # reference facilities), then per-role with its own sub-seed (§4.6). datagen
        # picks the Agent by role and the Policy by type; the agent runs the policy.
        ctx = PolicyContext(spec, catalog)
        collections: dict[str, dict] = {}
        for role in ("facility", "truck", "order", "assignment", "analytics", "order_lifecycle"):
            policy = resolve_policy(role, policies_cfg.get(role), ctx)
            agent = agent_class(role)(
                spec, catalog, self._role_rng(seed, role), policy,
                hauliers=role_hauliers.get(role, []),
            )
            collections[role] = agent.generate(counts[role])

        orsim_settings = None
        if spec.orsim_settings is not None:
            orsim_settings = dict(spec.orsim_settings)
            orsim_settings["BEHAVIOR_REVISION"] = spec.behavior_revision
            if spec.generation_spec_meta:
                orsim_settings["GENERATION_SPEC"] = spec.generation_spec_meta

        result = GenerationResult(
            truck=collections["truck"],
            order=collections["order"],
            facility=collections["facility"],
            assignment=collections["assignment"],
            analytics=collections["analytics"],
            order_lifecycle=collections["order_lifecycle"],
            orsim_settings=orsim_settings,
        )
        post_generate = self._hook("post_generate")
        if post_generate is not None:
            result = post_generate(result, spec) or result
        return result

    def write(self, behavior_dir: str) -> GenerationResult:
        result = self.generate()
        result.write(behavior_dir)
        return result
