"""Agents + their Policies — the datagen's generation core.

Model (see docs/policy_strategy_refactor_plan.md):

    datagen ──▶ Agent (OrderAgent, TruckAgent, FacilityAgent)   ← top / parent classes
                  └── runs ──▶ Policy  (two parents by data source)
                                RandomOrderPolicy       (random data → Uniform)
                                HistoricalOrderPolicy   (given/real data → a ProbabilityMatrix)
                                  └─ TimeBasedHistoricalOrderPolicy  (extends: + Curve for time)

An **Agent** builds its role's behavior dicts, sourcing each sampled field from the
**Policy** it runs (`policy.location_dist()`, `policy.time_dist()`, …). A **Policy**
only decides *how* a field is sampled — it returns a `Distribution`. The matrix/curve
are distributions (the ways to sample), not policies.

Byte-compat: an agent's assembly is the old builder body verbatim; the default
(`Historical…Policy` with the supplied matrix) reproduces today's output under a shared
seed because its distributions wrap the exact same sampling functions.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Optional

from ..builders import resolve_facilities
from ..catalog import LocationCatalog


class PolicyContext:
    """Everything a policy needs to build its distributions — no globals."""

    def __init__(self, spec, catalog: LocationCatalog):
        self.spec = spec
        self.catalog = catalog
        self.facilities = resolve_facilities(spec.facility_settings)

    def codes(self) -> list[str]:
        return list(self.catalog.codes())

    def facility_pairs(self) -> list[tuple[str, str]]:
        """All off-diagonal (pickup_code, dropoff_code) pairs present in the facilities."""
        codes = sorted({f.get("code") for f in self.facilities if f.get("code")})
        return [(p, d) for p in codes for d in codes if p != d]

    def steps(self) -> int:
        return self.spec.simulation_end_step + 1


class Policy(ABC):
    """A sampling strategy an agent runs. Subclasses implement the field ``*_dist`` methods."""

    role: str = ""
    name: str = ""

    def __init__(self, params: Optional[dict], ctx: PolicyContext):
        self.params = params or {}
        self.ctx = ctx


class Agent(ABC):
    """A role's generator. Runs one policy and builds the behavior dicts."""

    role: str = ""

    def __init__(self, spec, catalog: LocationCatalog, rng: random.Random, policy: Policy,
                 hauliers: Optional[list] = None):
        self.spec = spec
        self.catalog = catalog
        self.rng = rng
        self.policy = policy
        self.hauliers = list(hauliers or [])
        self.facilities = resolve_facilities(spec.facility_settings)

    @property
    def domain(self):
        return self.spec.domain

    def haulier_for(self, i: int) -> Optional[dict]:
        return self.hauliers[i] if 0 <= i < len(self.hauliers) else None

    def _default_haulier(self) -> dict:
        hs = self.spec.truck_hauliers or self.spec.order_hauliers
        return dict(hs[0]) if hs else {"id": "haulier", "name": "Haulier"}

    @abstractmethod
    def generate(self, n: int) -> dict:
        """Return ``{agent_id: behavior_dict}`` for this role."""
