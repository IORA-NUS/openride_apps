"""Phase 1 — the pure pricing core (plan §4.2, §11 Phase 1).

These tests exercise ``apps/container_logistics/rebate.py`` directly. Nothing is
stubbed: the module is stdlib-only and every assertion runs the real parser and the
real pricing rule.
"""

import json
import logging
import math

import pytest

from apps.container_logistics.rebate import (
    DEFAULT_CURRENCY,
    HOURS_PER_DAY,
    RebateBook,
    RebateSchedule,
    RebateSpecError,
    coerce_when,
    parse_rebate_schedule,
    price_at,
    schedules_by_digest,
)

# A schedule that pays at night, charges in the morning rush, and leaves the rest of
# the day unauthored — so the gap rule and the sign rule are both live in one fixture.
NIGHT_PAY_MORNING_CHARGE = {
    "currency": "credit",
    "resolution": "hour",
    "points": [
        {"hour": 2, "amount": 20.0},
        {"hour": 3, "amount": 12.5},
        {"hour": 7, "amount": -30.0},
        {"hour": 16, "amount": 5.0},
        {"hour": 22, "amount": 20.0},
    ],
}


def _full_day(amount=1.0):
    return {"points": [{"hour": h, "amount": amount} for h in range(HOURS_PER_DAY)]}


# --------------------------------------------------------------- pricing the clock


def test_price_at_reads_the_hour_from_an_rfc1123_string():
    """The recorded stamp format, verified today against 785 real completed trips."""
    schedule = parse_rebate_schedule(NIGHT_PAY_MORNING_CHARGE)
    # Exactly the shape `stats.pickup_queue_arrival_time` holds in MongoDB.
    assert price_at(schedule, "Wed, 01 Jan 2020 16:00:00 GMT") == 5.0
    assert price_at(schedule, "Wed, 01 Jan 2020 02:59:59 GMT") == 20.0
    assert price_at(schedule, "Thu, 02 Jan 2020 07:30:00 GMT") == -30.0
    # Minutes/seconds never matter: pricing is per hour-of-day.
    assert price_at(schedule, "Wed, 01 Jan 2020 22:00:00 GMT") == price_at(
        schedule, "Sun, 15 Jun 2031 22:59:59 GMT"
    )


def test_price_at_refuses_a_step_index():
    """The G5 trap, made loud.

    Deriving hour-of-day from a step index is silently 4 or 8 hours wrong depending
    on which of the tree's three conflicting ``REFERENCE_TIME`` defaults won. A bare
    number must therefore raise rather than be treated as anything.
    """
    schedule = parse_rebate_schedule(_full_day())
    for bad in (0, 1260, 2520, 3600.0, True):
        with pytest.raises(ValueError, match="step index|numeric time"):
            price_at(schedule, bad)


def test_price_at_is_independent_of_reference_time(monkeypatch):
    """The G5 regression guard: one arrival string, three epochs, one answer.

    ``apps/orsim_config.py`` declares a 04:00 epoch, six other modules and the
    flagship scenario declare 08:00, and ``scenario_config.py`` declares 00:00. A
    price derived from the recorded clock string cannot see any of them. This test
    fails the moment someone reintroduces a step-based hour derivation.
    """
    import apps.orsim_config as orsim_config

    schedule = parse_rebate_schedule(NIGHT_PAY_MORNING_CHARGE)
    arrival = "Wed, 01 Jan 2020 07:12:00 GMT"

    answers = set()
    for epoch in ("2020-01-01 00:00:00", "2020-01-01 04:00:00", "2020-01-01 08:00:00"):
        patched = dict(orsim_config.orsim_settings)
        patched["REFERENCE_TIME"] = epoch
        monkeypatch.setattr(orsim_config, "orsim_settings", patched, raising=False)
        answers.add(price_at(schedule, arrival))

    assert answers == {-30.0}, (
        "price_at must read the hour out of the recorded timestamp, never derive it "
        f"from a step index against a REFERENCE_TIME; got {answers}"
    )


def test_coerce_when_accepts_a_datetime_and_rfc1123_equivalently():
    from datetime import datetime

    assert coerce_when("Wed, 01 Jan 2020 16:00:00 GMT").hour == 16
    assert coerce_when(datetime(2020, 1, 1, 16, 0, 0)).hour == 16


# ------------------------------------------------------------------- sign and gaps


def test_negative_amount_is_returned_verbatim():
    """A surcharge must survive.

    This is the whole reason ``parse_order_demand_curve`` cannot be reused:
    ``demand._coerce_points`` clamps with ``max(0.0, weight)`` and
    ``normalize_hourly_weights`` divides by the total.
    """
    schedule = parse_rebate_schedule(NIGHT_PAY_MORNING_CHARGE)
    assert schedule.value_at(7) == -30.0
    assert price_at(schedule, "Wed, 01 Jan 2020 07:00:00 GMT") == -30.0
    # And it is not normalised: absolute magnitudes are preserved.
    assert schedule.value_at(2) == 20.0
    assert sum(schedule.amounts) == pytest.approx(27.5)


def test_a_schedule_summing_to_zero_does_not_degrade_to_flat():
    """Equal rebates and surcharges sum to 0.0 — normalisation would divide by it."""
    schedule = parse_rebate_schedule(
        {"points": [{"hour": 2, "amount": 50.0}, {"hour": 14, "amount": -50.0}]}
    )
    assert sum(schedule.amounts) == 0.0
    assert schedule.value_at(2) == 50.0
    assert schedule.value_at(14) == -50.0


def test_unlisted_hour_prices_zero_not_interpolated():
    """Gaps are zero. Interpolating a price invents money nobody authored."""
    schedule = parse_rebate_schedule(
        {"points": [{"hour": 2, "amount": 20.0}, {"hour": 22, "amount": 20.0}]}
    )
    # Under the demand curve's densification these would all read ~20.0.
    for hour in range(3, 22):
        assert schedule.value_at(hour) == 0.0, f"hour {hour} was interpolated"
    assert schedule.value_at(2) == 20.0
    assert schedule.value_at(22) == 20.0


def test_sparse_schedule_warns_and_lists_missing_hours(caplog):
    with caplog.at_level(logging.WARNING, logger="apps.container_logistics.rebate"):
        schedule = parse_rebate_schedule(
            {"points": [{"hour": 2, "amount": 20.0}, {"hour": 22, "amount": 20.0}]}
        )
    assert schedule.missing_hours == tuple(
        h for h in range(HOURS_PER_DAY) if h not in (2, 22)
    )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "sparse" in text
    # The missing hours must be NAMED, not merely counted.
    assert "[0, 1, 3, 4" in text and "23]" in text


def test_a_dense_schedule_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="apps.container_logistics.rebate"):
        schedule = parse_rebate_schedule(_full_day(3.0))
    assert schedule.missing_hours == ()
    assert not [r for r in caplog.records if "sparse" in r.getMessage()]


# ---------------------------------------------------------------------- validation


@pytest.mark.parametrize("bad_hour", [24, -1, 25, 100, 1.5, "3", None, True])
def test_hour_out_of_range_or_wrong_type_is_rejected(bad_hour):
    """Stricter than the demand curve, which silently drops an out-of-range hour."""
    with pytest.raises(RebateSpecError, match="hour"):
        parse_rebate_schedule({"points": [{"hour": bad_hour, "amount": 1.0}]})


def test_duplicate_hour_is_rejected():
    with pytest.raises(RebateSpecError, match="duplicate"):
        parse_rebate_schedule(
            {"points": [{"hour": 5, "amount": 1.0}, {"hour": 5, "amount": 2.0}]}
        )


@pytest.mark.parametrize("bad", ["12.5", None, True, [], {}])
def test_non_numeric_amount_is_rejected(bad):
    with pytest.raises(RebateSpecError, match="amount"):
        parse_rebate_schedule({"points": [{"hour": 5, "amount": bad}]})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_amount_is_rejected(bad):
    with pytest.raises(RebateSpecError, match="finite"):
        parse_rebate_schedule({"points": [{"hour": 5, "amount": bad}]})


def test_empty_or_missing_points_is_rejected():
    with pytest.raises(RebateSpecError, match="points"):
        parse_rebate_schedule({"points": []})
    with pytest.raises(RebateSpecError, match="points"):
        parse_rebate_schedule({"currency": "credit"})
    with pytest.raises(RebateSpecError, match="points"):
        parse_rebate_schedule({"points": {"0": 1.0}})


def test_non_object_block_is_rejected():
    for bad in ([], "rebate", 5, None):
        with pytest.raises(RebateSpecError):
            parse_rebate_schedule(bad)


def test_unsupported_resolution_is_rejected():
    with pytest.raises(RebateSpecError, match="resolution"):
        parse_rebate_schedule({"resolution": "minute", "points": [{"hour": 0, "amount": 1.0}]})


def test_non_string_currency_is_rejected():
    with pytest.raises(RebateSpecError, match="currency"):
        parse_rebate_schedule({"currency": 5, "points": [{"hour": 0, "amount": 1.0}]})


def test_weight_is_accepted_as_an_alias_but_not_alongside_amount():
    schedule = parse_rebate_schedule({"points": [{"hour": 4, "weight": -7.0}]})
    assert schedule.value_at(4) == -7.0
    with pytest.raises(RebateSpecError, match="both 'amount' and 'weight'"):
        parse_rebate_schedule({"points": [{"hour": 4, "weight": 1.0, "amount": 2.0}]})


def test_currency_defaults_to_credit():
    assert parse_rebate_schedule(_full_day()).currency == DEFAULT_CURRENCY


# --------------------------------------------------------------------- RebateCurve


def test_rebate_curve_refuses_to_sample():
    """``Curve.sample`` is invalid under negative weights and must never run."""
    import random

    from apps.container_logistics.datagen.distributions.rebate_curve import RebateCurve

    curve = RebateCurve(parse_rebate_schedule(NIGHT_PAY_MORNING_CHARGE))
    assert curve.value_at(7) == -30.0
    assert curve.price_at("Wed, 01 Jan 2020 07:00:00 GMT") == -30.0
    with pytest.raises(TypeError, match="never be sampled"):
        curve.sample(random.Random(0))
    with pytest.raises(TypeError, match="never be sampled"):
        curve.sample_step(random.Random(0), simulation_end=10, simulation_days=1,
                          step_interval_seconds=240)


def test_curve_value_at_does_not_normalise():
    """``Curve`` still stores weights verbatim — the property the reuse rests on."""
    from apps.container_logistics.datagen.distributions.curve import Curve

    weights = [3.0, -1.0, 0.0, 7.5] + [0.0] * 20
    curve = Curve(weights)
    assert curve.bin_weights == weights
    assert curve.value_at(0) == 3.0
    assert curve.value_at(1) == -1.0
    assert curve.value_at(3) == 7.5
    assert sum(curve.bin_weights) == pytest.approx(9.5)  # NOT 1.0
    # And an existing normalised curve is untouched by the new accessor.
    normalised = Curve([1.0 / 24] * 24)
    assert normalised.value_at(11) == pytest.approx(1.0 / 24)


# ---------------------------------------------------------------------- RebateBook


def _facility_doc(fid, block):
    return {"_id": fid, "profile": {"name": f"fac_{fid}", "rebate": block}}


def test_book_prices_only_facilities_that_publish():
    book = RebateBook.from_facility_docs(
        [
            _facility_doc("port_a", NIGHT_PAY_MORNING_CHARGE),
            {"_id": "depot_b", "profile": {"name": "depot"}},
        ]
    )
    assert len(book) == 1
    assert book.price_at("port_a", "Wed, 01 Jan 2020 16:00:00 GMT") == 5.0
    # A facility with no schedule is UNPRICEABLE (None), not free (0.0) — settlement
    # counts it rather than imputing.
    assert book.price_at("depot_b", "Wed, 01 Jan 2020 16:00:00 GMT") is None
    assert book.price_at(None, "Wed, 01 Jan 2020 16:00:00 GMT") is None
    assert book.currency == "credit"


def test_book_stringifies_facility_ids():
    """Trip meta carries ObjectId-ish ids; the book keys on ``str``."""

    class _Oid:
        def __str__(self):
            return "6a82c961e30b9937b9db6f5e"

    book = RebateBook.from_facility_docs([_facility_doc(_Oid(), _full_day(2.0))])
    assert book.price_at("6a82c961e30b9937b9db6f5e", "Wed, 01 Jan 2020 03:00:00 GMT") == 2.0
    assert _Oid() in book


def test_book_skips_an_unparseable_block_rather_than_dying(caplog):
    with caplog.at_level(logging.WARNING, logger="apps.container_logistics.rebate"):
        book = RebateBook.from_facility_docs(
            [
                _facility_doc("good", _full_day(1.0)),
                _facility_doc("bad", {"points": [{"hour": 99, "amount": 1.0}]}),
            ]
        )
    assert len(book) == 1
    assert book.price_at("bad", "Wed, 01 Jan 2020 00:00:00 GMT") is None
    assert any("unparseable" in r.getMessage() for r in caplog.records)


def test_book_propagates_a_bad_timestamp_rather_than_pricing_zero():
    book = RebateBook.from_facility_docs([_facility_doc("port_a", _full_day(9.0))])
    with pytest.raises(ValueError):
        book.price_at("port_a", "not a timestamp")


def test_empty_book_is_falsy_and_has_no_currency():
    book = RebateBook()
    assert not book and len(book) == 0 and book.currency is None


# ----------------------------------------------------------------- provenance bits


def test_identical_schedules_collapse_to_one_digest():
    """Two different authorings that MEAN the same schedule share a digest."""
    sparse = parse_rebate_schedule({"points": [{"hour": 3, "amount": 4.0}]})
    explicit = parse_rebate_schedule(
        {"points": [{"hour": h, "amount": 4.0 if h == 3 else 0.0} for h in range(24)]}
    )
    assert sparse.digest() == explicit.digest()
    assert sparse.amounts == explicit.amounts
    # ...and a different schedule does not.
    assert parse_rebate_schedule(_full_day(4.0)).digest() != sparse.digest()
    # The currency is part of the identity — same numbers, different unit label.
    other = parse_rebate_schedule({"currency": "eur", "points": [{"hour": 3, "amount": 4.0}]})
    assert other.digest() != sparse.digest()


def test_schedules_by_digest_collapses_and_counts():
    port = parse_rebate_schedule({"points": [{"hour": 3, "amount": 4.0}]})
    depot = parse_rebate_schedule({"points": [{"hour": 3, "amount": -4.0}]})
    grouped = schedules_by_digest(
        [
            ("port_000", "Port", port),
            ("port_001", "Port", port),
            ("depot_000", "Depot", depot),
        ]
    )
    assert len(grouped) == 2
    by_count = {b["facility_count"]: b for b in grouped.values()}
    assert by_count[2]["facility_types"] == ["Port"]
    assert by_count[2]["example_facility"] == "port_000"
    assert by_count[1]["facility_types"] == ["Depot"]
    assert len(by_count[2]["points"]) == 24


def test_as_block_round_trips_through_the_parser():
    """A compiled block re-parses to the same schedule — the settlement path's input."""
    original = parse_rebate_schedule(NIGHT_PAY_MORNING_CHARGE)
    block = original.as_block()
    assert json.loads(json.dumps(block)) == block  # JSON-serialisable as stored
    reparsed = parse_rebate_schedule(block)
    assert reparsed.amounts == original.amounts
    assert reparsed.currency == original.currency
    assert reparsed.missing_hours == ()  # the compiled form is dense


def test_schedule_rejects_a_wrong_length_amount_vector():
    with pytest.raises(RebateSpecError):
        RebateSchedule(amounts=(1.0, 2.0))
    assert RebateSchedule.zero().is_all_zero
    assert not math.isnan(RebateSchedule.zero().value_at(0))


# ===========================================================================
# Revision 2 — R2-12 / review F12: same-facility double credit is REACHABLE
# ===========================================================================


def test_same_facility_both_legs_pays_twice():
    """Both arrivals earn independently, even at the SAME facility. Intent, pinned.

    Plan §13.3 asserted this "cannot occur within one trip", reasoning from
    ``_pick_facility``'s call site rather than its body. The body has two escape
    hatches — ``if exclude is not None and len(pool) > 1`` and the ``or pool``
    fallback — so on a scenario where a code has a single facility, pickup and dropoff
    CAN be the same place. That plan sentence is wrong and is corrected here.

    The resulting BEHAVIOUR is correct per the settled spec: a facility pays whoever
    shows up, there is no pickup/dropoff role logic anywhere, and two arrivals are two
    arrivals. This test exists so that intent is pinned rather than rediscovered as a
    suspected bug — which is exactly how it surfaced in review.

    (Empirically it did not occur in run_20260817_164156: 0 of 785 completed trips had
    matching facility ids, because that scenario has 6 ports, 60 depots and 234
    warehouses. It is a small-scenario reachability, not a live defect.)
    """
    schedule = parse_rebate_schedule({"points": [{"hour": 9, "amount": 15.0}]})
    book = RebateBook({"only_facility": schedule})

    pickup = book.price_at("only_facility", "Wed, 01 Jan 2020 09:00:00 GMT")
    dropoff = book.price_at("only_facility", "Wed, 01 Jan 2020 09:45:00 GMT")

    assert pickup == 15.0 and dropoff == 15.0
    assert pickup + dropoff == 30.0, (
        "both arrivals at one facility must earn independently — a facility pays "
        "whoever arrives, and role logic is exactly what the design excludes"
    )


def test_pick_facility_exclusion_really_has_the_two_escape_hatches():
    """The source fact behind the correction, asserted rather than described.

    If someone later makes the exclusion unconditional, plan §13.3's original sentence
    becomes true and this test should be revisited — it is the tripwire for that.
    """
    import inspect

    from apps.container_logistics.datagen import builders

    src = inspect.getsource(builders._BaseBuilder.__mro__[0])
    body = inspect.getsource(builders.OrderBuilder._pick_facility)
    assert "len(pool) > 1" in body, "the size guard is gone; §13.3 may now be true"
    assert "or pool" in body, "the fallback is gone; §13.3 may now be true"
