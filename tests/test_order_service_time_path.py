"""R2-6 (F5, F12) — the ORDER-side causal path of a `service_time` rule.

A `service_time` rule has **two** independent effects and only one of them was tested.

* **Facility side** — governs actual gate occupancy. Covered since the feature shipped.
* **Order side** — `profile.pickup_service_time` / `dropoff_service_time`, flattened
  onto every matched order from the same site. These DO reach Mongo and have three hot
  readers: the assignment publish payload, `gate_service_seconds()` feeding the
  `MIN_HAUL_TRIP_SECONDS` floor (four call sites), and the trip-stats seed. **This
  path was entirely untested.**

Not to be confused with the `pickup_facility`/`dropoff_facility` snapshot, which sits
one level up on the behavior, never reaches Mongo, and has zero runtime readers — the
grep-guard below is what stops that claim from rotting.

R2-3 makes the parametrisation over BOTH authoring layers possible, which is a
stronger claim than testing the rule path alone.
"""

from __future__ import annotations

import re
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from apps.container_logistics.datagen import facility_rules as fr
from apps.container_logistics.datagen.generator import ScenarioGenerator
from apps.container_logistics.datagen.preprocess import Preprocessor
from apps.container_logistics.duration_constants import (
    MIN_HAUL_TRIP_SECONDS,
    MIN_LEG_DROPOFF_SECONDS,
    MIN_LEG_PICKUP_SECONDS,
)
from apps.container_logistics.haul_trip_duration import (
    apply_haul_trip_duration_floors,
    gate_service_seconds,
)

DOMAIN = "container-logistics-sim-test"
MIDNIGHT = "2020-01-01 00:00:00"
_DEFAULT_SERVICE = 1800
_RULED_SERVICE = 600


def _base(**kw):
    spec = {
        "name": "Order Path", "slug": "order_path", "simulationDays": 1,
        "orderCountUnit": "total", "referenceTime": MIDNIGHT,
        "agents": {"truck": {"count": 6}, "order": {"count": 60},
                   "facility": {"count": 30}},
        "earlyOrderCount": 0,
    }
    spec.update(kw)
    return spec


def _compile(raw):
    return Preprocessor.compile(deepcopy(raw), domain=DOMAIN, reference_time=MIDNIGHT)


def _generate(raw):
    return ScenarioGenerator(_compile(raw).spec).generate()


def _world():
    sites = _compile(_base()).spec.facility_settings["profile"]["facilities"]
    return fr.world_snapshot(sites, seed=None)


def _via_rule(value):
    return _base(facilityRules=[{"match": {"code": "CT"}, "set": {"service_time": value}}],
                 facilityRulesWorld=_world())


def _via_blanket(value):
    return _base(overrides={"facility": {"service_time": value}})


# --------------------------------------------------------------------------- #
# the untested path
# --------------------------------------------------------------------------- #

def test_service_time_rule_rewrites_only_matched_orders_profile_service_time():
    """The flattening site (`builders.py` / `agents/order.py`), which no test reached.

    A CT rule must rewrite `profile.pickup_service_time` on exactly the orders whose
    PICKUP facility is a port, `profile.dropoff_service_time` on exactly those whose
    DROPOFF facility is one, and neither on any other order.
    """
    orders = _generate(_via_rule(_RULED_SERVICE)).order
    assert orders

    matched_pickup = matched_dropoff = unmatched = 0
    for behavior in orders.values():
        prof = behavior["profile"]
        pickup_is_port = str(prof["pickup_facility_name"]).startswith("port_")
        dropoff_is_port = str(prof["dropoff_facility_name"]).startswith("port_")

        if pickup_is_port:
            assert prof["pickup_service_time"] == _RULED_SERVICE
            matched_pickup += 1
        else:
            assert prof["pickup_service_time"] == _DEFAULT_SERVICE
            unmatched += 1
        if dropoff_is_port:
            assert prof["dropoff_service_time"] == _RULED_SERVICE
            matched_dropoff += 1
        else:
            assert prof["dropoff_service_time"] == _DEFAULT_SERVICE

    # Both arms must occur or the per-order assertions above are vacuous.
    assert matched_pickup > 0 and matched_dropoff > 0 and unmatched > 0, (
        f"fixture did not exercise both sides: pickup={matched_pickup} "
        f"dropoff={matched_dropoff} unmatched={unmatched}"
    )


@pytest.mark.parametrize("build", [_via_rule, _via_blanket], ids=["rule", "blanket"])
def test_both_authoring_layers_produce_the_same_order_side_effect(build):
    """Available only after R2-3: one validator, two doors, and now the same
    downstream effect asserted through both."""
    orders = _generate(build(_RULED_SERVICE)).order
    ports = [b["profile"] for b in orders.values()
             if str(b["profile"]["pickup_facility_name"]).startswith("port_")]
    assert ports
    assert all(p["pickup_service_time"] == _RULED_SERVICE for p in ports)


def test_the_order_side_value_is_the_facilitys_value_not_a_default():
    """Anti-vacuity: the numbers must actually come from the rule, so a resolver that
    stopped reaching the order side would not pass by coincidence."""
    for value in (300, 900, 2400):
        orders = _generate(_via_rule(value)).order
        ports = [b["profile"] for b in orders.values()
                 if str(b["profile"]["pickup_facility_name"]).startswith("port_")]
        assert ports and all(p["pickup_service_time"] == value for p in ports)


# --------------------------------------------------------------------------- #
# MEASURE the floor interaction — do not assert it
# --------------------------------------------------------------------------- #

def _haul_total(service_time, eta_pickup=300.0, eta_dropoff=300.0):
    order = {"pickup_service_time": service_time, "dropoff_service_time": service_time}
    p, d = apply_haul_trip_duration_floors(eta_pickup, eta_dropoff, order=order)
    gp, gd = gate_service_seconds(order)
    return p + d + gp + gd


def test_service_time_rule_effect_on_haul_duration_is_measured_not_assumed():
    """**Measure the floor interaction; do not assume its direction.**

    The hypothesis under test was that `MIN_HAUL_TRIP_SECONDS` (35 min) partially
    ABSORBS a `service_time` reduction, so the order-side and facility-side effects of
    one rule do not simply add. **That is false at every realistic service time, and
    this test records why.**

    `apply_haul_trip_duration_floors` first clamps each leg to its own minimum
    (`MIN_LEG_PICKUP_SECONDS` 900 s + `MIN_LEG_DROPOFF_SECONDS` 960 s = 1860 s) and
    only then tops up to the 2100 s haul floor. So the floor has just **240 s** of
    headroom to absorb anything, and it binds only once TOTAL gate service falls below
    that — i.e. below ~120 s per gate. At the scenario default of 1800 s, gate service
    alone is 3600 s and the floor is nowhere near binding.

    **Consequence for the feature, and it is the opposite of the hypothesis:** in the
    realistic range a `service_time` rule passes through to haul duration at FULL
    strength on the order side, on top of its facility-side effect on gate occupancy.
    The two paths add, and an author lowering `service_time` gets more effect than a
    gate-occupancy-only reading predicts, not less.
    """
    naive_gate_delta = 2 * (_DEFAULT_SERVICE - _RULED_SERVICE)
    actual = _haul_total(_DEFAULT_SERVICE) - _haul_total(_RULED_SERVICE)
    assert actual == naive_gate_delta, (
        f"in the realistic range the floor must NOT bind: expected the full "
        f"{naive_gate_delta}s, measured {actual}s"
    )

    # Where does it actually start to bind? Found empirically, not asserted from the
    # constants, so a change to any of the three constants surfaces here.
    headroom = MIN_HAUL_TRIP_SECONDS - (MIN_LEG_PICKUP_SECONDS + MIN_LEG_DROPOFF_SECONDS)
    binding = [
        st for st in range(0, 400, 10)
        if _haul_total(st) - _haul_total(max(0, st - 10)) < 20
    ]
    threshold = min(binding) if binding else None
    print(
        f"\nfloor interaction, MEASURED:\n"
        f"  MIN_HAUL_TRIP_SECONDS      = {MIN_HAUL_TRIP_SECONDS}s\n"
        f"  leg minimums               = {MIN_LEG_PICKUP_SECONDS} + "
        f"{MIN_LEG_DROPOFF_SECONDS} = {MIN_LEG_PICKUP_SECONDS + MIN_LEG_DROPOFF_SECONDS}s\n"
        f"  headroom the floor can absorb = {headroom}s TOTAL gate service\n"
        f"  => binds only below ~{headroom // 2}s per gate "
        f"(empirically from ~{threshold}s)\n"
        f"  at the scenario default {_DEFAULT_SERVICE}s: gate service is "
        f"{2 * _DEFAULT_SERVICE}s, i.e. {2 * _DEFAULT_SERVICE / headroom:.0f}x the "
        f"headroom — the floor does not bind\n"
        f"  {_DEFAULT_SERVICE}s -> {_RULED_SERVICE}s reduces haul duration by "
        f"{actual:.0f}s of a predicted {naive_gate_delta:.0f}s (0% absorbed)"
    )
    assert headroom == 240, (
        "the headroom changed — re-measure the threshold before trusting the "
        "'the two paths add' conclusion above"
    )


def test_the_floor_DOES_bind_below_the_measured_threshold():
    """The other side of the measurement: the floor is real, it is just out of range.

    Without this, the test above reads as "the floor does nothing", which would be the
    wrong lesson — it would invite deleting a guard that binds for a sub-120 s gate.
    """
    assert _haul_total(0) == MIN_HAUL_TRIP_SECONDS
    assert _haul_total(60) == MIN_HAUL_TRIP_SECONDS, "120s total is inside the headroom"
    # ... and just above the headroom it tracks gate service one-for-one again.
    assert _haul_total(200) > MIN_HAUL_TRIP_SECONDS


# --------------------------------------------------------------------------- #
# the grep guard — stops the corrected docstring from rotting
# --------------------------------------------------------------------------- #

def test_order_facility_snapshot_has_no_runtime_reader():
    """The corrected `order_facility_view` docstring claims the snapshot has no
    runtime reader. That claim is load-bearing — it is why `gate_count` is kept
    rather than cut — so it gets a guard rather than trust.
    """
    root = Path(__file__).resolve().parents[1] / "apps"
    out = subprocess.run(
        ["grep", "-rn", "-E", r"\b(pickup_facility|dropoff_facility)\b",
         "--include=*.py", str(root)],
        capture_output=True, text=True,
    ).stdout.splitlines()
    offenders = [
        line for line in out
        if "/datagen/" not in line
        and not re.search(r"(pickup|dropoff)_facility_(name|resource_id)", line)
        and "__pycache__" not in line
    ]
    assert not offenders, (
        "the order-embedded facility snapshot has acquired a reader outside datagen:\n"
        + "\n".join(offenders)
        + "\n\nIf that is intended, the `order_facility_view` docstring and the "
          "reasoning for keeping gate_count in the snapshot must be revisited — the "
          "snapshot would no longer be provenance-only."
    )
