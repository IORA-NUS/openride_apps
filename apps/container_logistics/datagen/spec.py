"""The pure boundary of data generation: one immutable spec in, behaviors out.

Everything the generator needs is resolved into a :class:`GenerationSpec` by the
adapter (``scenario/scenario_datagen.py``) *before* generation runs. The
``datagen`` package never reads module globals, env, or scenario_config — same
spec in, same behaviors out. That is what makes generation isolated and testable.

The per-role ``*_settings`` dicts are opaque pass-throughs mirroring
``scenario_config``'s settings (so behavior-dict field parity is preserved
exactly); the generator only reads them, never mutates shared state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class GenerationSpec:
    domain: str

    # Agent counts.
    num_trucks: int
    num_orders: int
    num_facilities: int

    # Simulated calendar.
    simulation_days: int
    step_interval_seconds: int
    simulation_length_in_steps: int
    reference_time: str = "2020-01-01 08:00:00"
    behavior_revision: int = 0

    # Opaque per-role settings (mirror scenario_config.*_settings).
    truck_settings: dict = field(default_factory=dict)
    order_settings: dict = field(default_factory=dict)
    facility_settings: dict = field(default_factory=dict)
    assignment_settings: dict = field(default_factory=dict)
    analytics_settings: dict = field(default_factory=dict)

    # Per-agent haulier assignment (length num_trucks / num_orders), resolved by
    # the adapter via scenario_config.distribute_by_share.
    truck_hauliers: tuple = ()
    order_hauliers: tuple = ()

    # Demand curve (24 normalized hourly weights) and business window.
    hourly_weights: Optional[list] = None
    business_hour_start: int = 0
    business_hour_end: int = 24

    # Pickup->delivery trip distribution (normalized, restricted to codes that
    # have real addresses). Required for order generation.
    trip_matrix: Optional[dict] = None

    # Location codes excluded from the address book (e.g. {"YD"}); and the codes
    # trucks may start at (None = any code with real addresses). Both data-driven.
    excluded_codes: tuple = ()
    truck_origin_codes: Optional[tuple] = None

    # Real-location data sources.
    locations_csv: Optional[str] = None
    sg_mask_path: Optional[str] = None

    # Fully-built ORSim settings to persist verbatim (optional — only needed when
    # the generator writes orsim_settings.json directly, e.g. standalone/CLI).
    orsim_settings: Optional[dict] = None
    generation_spec_meta: Optional[dict] = None

    # Master seed for reproducible generation (plan §4.6). Each role gets its own
    # sub-seed derived from this, so editing one role never perturbs another.
    seed: int = 20260712

    # One policy per role: {role: {"type": name, ...params}}. Empty => each role's
    # default policy (which reproduces today's builders).
    role_policies: dict = field(default_factory=dict)

    @property
    def simulation_end_step(self) -> int:
        return max(0, int(self.simulation_length_in_steps) - 1)

    def facilities(self) -> list:
        """Resolved facility site list (from facility_settings.profile.facilities)."""
        profile = self.facility_settings.get("profile", {}) if self.facility_settings else {}
        facilities = profile.get("facilities") or []
        return list(facilities)
