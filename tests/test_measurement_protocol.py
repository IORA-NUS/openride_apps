"""R0-R7 made mechanical (plan §14.3 / R3-1).

The round-2 CRITICAL was that the gate certified a +0.583 pp effect whose control
arm spanned 1.00 pp. Every correctness item passed. These tests make the rule that
would have caught it executable.
"""

import pytest

from apps.simulation.measurement_protocol import assess, paired_bound_caveat


# The real round-1 numbers, so this is a regression test against history.
_REAL_CONTROL = [10.451, 11.449, 10.986, 10.702, 11.203, 10.870]   # arm C, six runs
_REAL_B = [11.054, 10.298, 10.397]
_REAL_A = [10.000, 10.000, 10.000]


def test_gate_reports_control_spread_beside_effect():
    """R2: the control's own spread must be reported next to the effect, and when it
    exceeds the effect the contrast must be refused."""
    r = assess(_REAL_B, _REAL_A, _REAL_CONTROL)
    assert r.control_spread is not None
    assert r.effect is not None
    assert r.control_spread == pytest.approx(0.998, abs=0.001)
    assert not r.resolvable, "the historical n=3 contrast must be refused, not quoted"
    assert any("noise exceeds the signal" in x for x in r.reasons)
    # And the report SHOWS both, so a reader cannot see the effect without the noise.
    text = r.report()
    assert "control spread" in text and "effect" in text
    assert "do not quote" in text


def test_a_clean_effect_well_above_the_control_spread_is_resolvable():
    r = assess([20.0, 20.1, 19.9] * 4, [10.0, 10.1, 9.9] * 4, [10.0, 10.05, 9.95] * 4)
    assert r.resolvable, r.report()
    assert r.effect == pytest.approx(10.0, abs=0.1)


def test_required_n_follows_the_control_sd_not_a_folklore_constant():
    """R7: the replicate count is derived from the control's OWN measured spread.

    Note on the fixture: the plan quotes sd ~= 0.394 pp, which it derived from the
    published RANGE (0.998 pp over n=6, i.e. range/d2 with d2~=2.534) because the six
    individual values were never published. `_REAL_CONTROL` reproduces that range,
    so its *sample* sd (~0.355) legitimately differs from the range-based estimate.
    Both land in the same place for the decision that matters — n ~= 7-9 per arm —
    which is what is asserted, rather than a number the fixture cannot produce.
    """
    r = assess(_REAL_B, _REAL_A, _REAL_CONTROL)
    assert r.control_spread == pytest.approx(0.998, abs=0.001)
    assert r.control_sd is not None and 0.30 <= r.control_sd <= 0.45, r.control_sd
    # sd/effect ~ 0.6-0.7 -> n ~ 7-9, well above the n=3 that was actually run.
    assert r.required_n is not None and 6 <= r.required_n <= 10, r.required_n
    assert r.required_n > 3, "the historical n=3 must be shown to be insufficient"


def test_a_single_control_run_is_never_resolvable():
    """R2 needs a spread; one control run has none, so nothing may be quoted."""
    r = assess([2.0, 2.0, 2.0], [1.0, 1.0, 1.0], [1.0])
    assert not r.resolvable
    assert any("fewer than 2 replicates" in x for x in r.reasons)


def test_n_below_the_standing_floor_is_flagged_even_when_arithmetically_resolvable():
    r = assess([20.0, 20.0], [10.0, 10.0], [10.0, 10.0001])
    assert any("standing unpaired floor" in x for x in r.reasons)


def test_paired_caveat_states_the_direction_of_the_bias():
    """The caveat the review did not raise: the common set is a collider, and the
    bias flatters the pooled arm, so the paired figure is a LOWER BOUND."""
    text = paired_bound_caveat(a_only=71, b_only=62, common=722)
    assert "LOWER BOUND" in text
    assert "post-treatment" in text or "collider" in text
    assert "71" in text and "62" in text and "722" in text


def test_required_n_reproduces_the_plans_published_arithmetic():
    """Pins the formula against §14.0's worked example: sd 0.394, effect 0.583 -> n>=8.

    Dropping the two-sample factor of 2 (easy, and I did it once) understates the
    requirement ~2x and returns n=3 — certifying exactly the under-powered contrast
    this module exists to refuse. So the constant is pinned, not trusted.
    """
    import math

    from apps.simulation.measurement_protocol import _TWO_SAMPLE_FACTOR, _Z_SUM

    n = math.ceil(_TWO_SAMPLE_FACTOR * (_Z_SUM * 0.394 / 0.583) ** 2)
    assert n == 8, f"formula drifted from the plan's published n>=8: got {n}"
