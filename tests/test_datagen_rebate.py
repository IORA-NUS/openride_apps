"""Facility rebates — Phase 2: validation + compile-time resolution.

Covers plan §5's validation table (one test per row), the compile-time per-facility
resolution of §8 (`rebate` scenario-wide default at rank 0 + targeted `facilityRules`
above it, including the `null`-suppresses-the-default trap), the G22 two-builder-sync
guard, and R-I1a (schedule resolution is inert at generation, plan §7 / mutation M1).

The per-code authoring surface used to be `overrides.facility.rebate_by_code`; it is
now the generic `facilityRules` list (`{"match": {"code": "CT"}, "set": {"rebate": …}}`)
and the old key is hard-rejected at compile. The SEMANTICS pinned here are unchanged —
in particular a rank-10 `{"rebate": null}` still beats a rank-0 schedule, because what
counts is the PRESENCE of "rebate" in "set", exactly as it was for a `null` map entry.

Model for the inertness test is `tests/test_datagen_cooperation.py:220`
(`test_cooperation_is_inert_at_generation`) — same shape, applied to `rebate`.
"""

import json
import math
import random
from copy import deepcopy

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.agents.facility import FacilityAgent
from apps.container_logistics.datagen.builders import FacilityBuilder
from apps.container_logistics.datagen.catalog import LocationCatalog
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor, SpecValidationError

DOMAIN = "container-logistics-sim-test"

# 30 facilities under the default ("allocate") facility policy + default trip
# matrix deterministically yields all three active codes (Port=6, Warehouse=15,
# Depot=9 — verified empirically), so every test below can rely on all three
# codes being represented without pinning brittle exact counts.
_FACILITY_COUNT = 30

#: Every key that exists only to AUTHOR a per-facility value. None of them is a
#: facility-profile key, so none may ever reach a compiled profile.
_AUTHORING_KEYS = ("rebate_by_code", "facilityRules", "facilityRulesWorld")


def _spec(rules=None, **kw):
    spec = {
        "name": "Rebate Test",
        "slug": "rebate_test",
        "simulationDays": 1,
        "orderCountUnit": "total",
        "agents": {
            "truck": {"count": 6},
            "order": {"count": 12},
            "facility": {"count": _FACILITY_COUNT},
        },
        "earlyOrderCount": 0,
    }
    if rules is not None:
        # `facilityRules` without a recorded world is refused at compile: facility
        # names/counts are GENERATED, so rules are only meaningful against the world
        # they were written for.
        spec["facilityRules"] = deepcopy(rules)
        spec["facilityRulesWorld"] = deepcopy(_world())
    spec.update(kw)
    return spec


_WORLD_CACHE: dict = {}


def _world() -> dict:
    """The baseline `facilityRulesWorld` block for this module's own facility world.

    Computed by compiling the rule-less spec once — i.e. exactly what
    `openride scenario rules-baseline` does — so the fixture can never drift from
    the generator.
    """
    if "w" not in _WORLD_CACHE:
        compiled = Preprocessor.compile(
            deepcopy(_spec()), domain=DOMAIN, reference_time=MIDNIGHT
        )
        sites = compiled.spec.facility_settings["profile"]["facilities"]
        _WORLD_CACHE["w"] = fr.world_snapshot(sites, seed=None)
    return _WORLD_CACHE["w"]


def _facility_types(result) -> dict:
    """{facility_type -> count of facilities in `result.facility` carrying a 'rebate'}."""
    out: dict = {}
    for f in result.facility.values():
        prof = f["profile"]
        for key in _AUTHORING_KEYS:
            assert key not in prof, (
                f"{key} is authoring sugar and must never reach a facility profile"
            )
        if "rebate" in prof:
            out[prof["facility_type"]] = out.get(prof["facility_type"], 0) + 1
    return out


#: R2-1 / review F2: a spec carrying a rebate schedule is REFUSED unless the simulation
#: epoch is midnight, because the order-demand curve's authored hour H is realised at
#: wall hour (H + reference_hour) % 24 while a rebate is priced on the true wall clock.
#: These fixtures therefore compile on the midnight axis, which is also what any real
#: rebate scenario must now do.
MIDNIGHT = "2020-01-01 00:00:00"


def _compile_and_generate(spec_raw):
    compiled = Preprocessor.compile(spec_raw, domain=DOMAIN, reference_time=MIDNIGHT)
    return ScenarioGenerator(compiled.spec).generate()


# --------------------------------------------------------------- §5 validation table


def test_hour_out_of_range_rejected():
    spec = _spec(overrides={"facility": {"rebate": {"points": [{"hour": 24, "amount": 1.0}]}}})
    with pytest.raises(SpecValidationError, match="hour"):
        Preprocessor.compile(spec, domain=DOMAIN, reference_time=MIDNIGHT)


def test_duplicate_hour_rejected():
    spec = _spec(
        overrides={
            "facility": {
                "rebate": {
                    "points": [{"hour": 5, "amount": 1.0}, {"hour": 5, "amount": 2.0}]
                }
            }
        }
    )
    with pytest.raises(SpecValidationError, match="duplicate"):
        Preprocessor.compile(spec, domain=DOMAIN, reference_time=MIDNIGHT)


def test_non_numeric_amount_rejected():
    spec = _spec(
        overrides={"facility": {"rebate": {"points": [{"hour": 5, "amount": "lots"}]}}}
    )
    with pytest.raises(SpecValidationError, match="number"):
        Preprocessor.compile(spec, domain=DOMAIN, reference_time=MIDNIGHT)


def test_nan_and_inf_rejected():
    for bad in (math.nan, math.inf, -math.inf):
        spec = _spec(
            overrides={"facility": {"rebate": {"points": [{"hour": 5, "amount": bad}]}}}
        )
        with pytest.raises(SpecValidationError, match="finite"):
            Preprocessor.compile(spec, domain=DOMAIN, reference_time=MIDNIGHT)


def test_unknown_code_rejected_lists_available():
    spec = _spec([{"match": {"code": "ZZ"}, "set": {"rebate": None}}])
    with pytest.raises(SpecValidationError) as exc_info:
        Preprocessor.compile(spec, domain=DOMAIN, reference_time=MIDNIGHT)
    msg = str(exc_info.value)
    assert "ZZ" in msg
    assert "Available:" in msg
    for code in ("CT", "CU", "MT"):
        assert code in msg


def test_yd_code_rejected():
    # YD is excluded from the active code set (datagen/defaults.py EXCLUDED_CODES)
    # — a YD-keyed schedule would otherwise be dead config (plan G21).
    spec = _spec([{"match": {"code": "YD"}, "set": {"rebate": None}}])
    with pytest.raises(SpecValidationError, match="YD"):
        Preprocessor.compile(spec, domain=DOMAIN, reference_time=MIDNIGHT)


def test_a_null_rebate_rule_suppresses_the_default():
    # `{"rebate": null}` at rank 10 beats a schedule at rank 0: the resolver tests
    # PRESENCE of the key in `set`, never its truthiness. This is the one precedence
    # trap carried over verbatim from `rebate_by_code[code] = None`.
    spec = _spec(
        [{"match": {"code": "MT"}, "set": {"rebate": None}}],
        overrides={"facility": {"rebate": {"points": [{"hour": 0, "amount": 1.0}]}}},
    )
    result = _compile_and_generate(spec)
    counts = _facility_types(result)
    # Depot (MT) is explicitly suppressed; Port (CT) and Warehouse (CU) fall
    # through to the scenario-wide default.
    assert "Depot" not in counts
    assert counts.get("Port", 0) > 0
    assert counts.get("Warehouse", 0) > 0


def test_a_code_rule_resolves_only_ports():
    spec = _spec([
        {"match": {"code": "CT"},
         "set": {"rebate": {"points": [{"hour": 3, "amount": -5.0}]}}},
    ])
    result = _compile_and_generate(spec)
    counts = _facility_types(result)
    assert set(counts) == {"Port"}
    assert counts["Port"] > 0
    for f in result.facility.values():
        if f["profile"]["facility_type"] == "Port":
            assert f["profile"]["rebate"]["points"][3]["amount"] == -5.0


def test_both_facility_builders_emit_identical_blocks():  # G22
    spec = _spec(
        [
            {"match": {"code": "CT"},
             "set": {"rebate": {"points": [{"hour": 7, "amount": 12.5}]}}},
            {"match": {"code": "MT"}, "set": {"rebate": None}},
        ],
        overrides={"facility": {"rebate": {"points": [{"hour": 5, "amount": 2.0}]}}},
    )
    compiled = Preprocessor.compile(spec, domain=DOMAIN, reference_time=MIDNIGHT)
    gspec = compiled.spec
    catalog = LocationCatalog(
        gspec.locations_csv, gspec.sg_mask_path, excluded=gspec.excluded_codes
    )

    live = FacilityAgent(gspec, catalog, random.Random(0), None, hauliers=[])
    live_out = live.generate(gspec.num_facilities)

    legacy = FacilityBuilder(gspec, catalog)
    agent_ids = list(live_out.keys())
    legacy_out = {
        aid: legacy.build(aid, facility_index=idx) for idx, aid in enumerate(agent_ids)
    }

    assert agent_ids
    saw_rebate = False
    saw_no_rebate = False
    for aid in agent_ids:
        live_profile = live_out[aid]["profile"]
        legacy_profile = legacy_out[aid]["profile"]
        for key in _AUTHORING_KEYS:
            assert key not in live_profile
            assert key not in legacy_profile
        assert live_profile.get("rebate") == legacy_profile.get("rebate"), aid
        if "rebate" in live_profile:
            saw_rebate = True
        else:
            saw_no_rebate = True
    # Exercise BOTH branches (some facility has a schedule, some doesn't) —
    # otherwise the equality check above would be vacuous.
    assert saw_rebate and saw_no_rebate


# ------------------------------------------------------------------------- R-I1a


def _generated_collections(spec_raw):
    compiled = Preprocessor.compile(spec_raw, domain=DOMAIN, reference_time=MIDNIGHT)
    result = ScenarioGenerator(compiled.spec).generate()
    return {"truck": result.truck, "order": result.order, "facility": result.facility}


class _CountingRandom(random.Random):
    """A `random.Random` that counts calls to `.random()` — used to catch a
    stray RNG draw that the JSON-diff below cannot observe (see comment there).
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    def random(self):
        self.calls += 1
        return super().random()


def test_rebate_is_inert_at_generation():
    base = _generated_collections(_spec())
    with_schedule = _generated_collections(
        _spec(
            [{"match": {"code": "MT"}, "set": {"rebate": None}}],
            overrides={"facility": {"rebate": {"points": [{"hour": 9, "amount": 7.5}]}}},
        )
    )
    stripped = deepcopy(with_schedule)
    for facility in stripped["facility"].values():
        facility["profile"].pop("rebate", None)

    # The resolved schedule is now stamped on the facility SITE, and an order behavior
    # embeds a snapshot of its pickup/dropoff facility — so a rebate-only rule COULD
    # have rewritten the whole order collection. `builders.order_facility_view`
    # excludes `rebate` from that snapshot precisely so it does not; verified here
    # rather than assumed, and separately from the whole-output diff below, which
    # would otherwise report a facility-shaped failure for an order-side leak.
    assert json.dumps(base["order"], sort_keys=True, default=str) == json.dumps(
        with_schedule["order"], sort_keys=True, default=str
    ), "the resolved rebate rode into the order collection's embedded facility snapshot"

    assert json.dumps(base, sort_keys=True, default=str) == json.dumps(
        stripped, sort_keys=True, default=str
    ), "a rebate schedule must perturb nothing in generation except the 'rebate' key itself (R-I1a)"

    # Anti-vacuity: the arms MUST differ before stripping, or the diff above is
    # satisfied by a resolver that never resolved anything.
    assert json.dumps(base["facility"], sort_keys=True, default=str) != json.dumps(
        with_schedule["facility"], sort_keys=True, default=str
    ), "the fixture stamped no schedule at all — this test would pass on a dead resolver"

    # The JSON diff above compares FULL generation output across roles, but each
    # role is seeded with its OWN independent `random.Random` instance
    # (`ScenarioGenerator._role_rng`), and nothing else in `FacilityAgent.generate()`
    # reads from the facility role's RNG stream today (site selection is drawn from
    # a separately, fixed-seeded RNG inside `LocationCatalog.facility_sites`). So a
    # stray `self.rng.random()` draw during rebate resolution would NOT show up as
    # a difference in the facility collection's own output, and — because each
    # role's RNG is independent — it cannot leak into truck/order output either.
    # The diff above is therefore structurally unable to catch that specific
    # mutation; verify the "zero RNG" half of R-I1a directly instead, by
    # instrumenting the RNG object handed to the live builder.
    compiled = Preprocessor.compile(
        _spec([{"match": {"code": "CT"},
                "set": {"rebate": {"points": [{"hour": 9, "amount": 7.5}]}}}]),
        domain=DOMAIN,
        reference_time=MIDNIGHT,
    )
    gspec = compiled.spec
    catalog = LocationCatalog(
        gspec.locations_csv, gspec.sg_mask_path, excluded=gspec.excluded_codes
    )
    counting_rng = _CountingRandom(0)
    FacilityAgent(gspec, catalog, counting_rng, None, hauliers=[]).generate(gspec.num_facilities)
    assert counting_rng.calls == 0, (
        f"facility rebate resolution drew from the per-role RNG {counting_rng.calls} "
        f"time(s) — a stray draw here would shift every downstream sample (R-I1a)"
    )
