"""Locks ``tests/golden/compare.published_mismatch`` (review F9, round-2 spec C0).

The golden compares published MQTT payload strings exactly, except that the main
tree's Kafka-transport work appends ``sim_clock`` and ``time_step``. These tests pin
that the tolerance is exactly that pair, at exactly the harness tick, appended to an
otherwise byte-identical payload -- and nothing wider.
"""

from __future__ import annotations

import gzip
import json

from tests.golden.capture_dispatch_golden import FIXTURE_PATH
from tests.golden.compare import published_mismatch
from tests.golden.harness import TIME_STEP

with gzip.open(FIXTURE_PATH, "rb") as _fh:
    _FX = json.loads(_fh.read())
_CASE = next(c for c in _FX["cases"] if c["expected"]["published"])
_TOPIC, _PAYLOAD = _CASE["expected"]["published"][0]
_CLOCK = _FX["tick_states"][_CASE["tick_state"]]["sim_clock"]


def _with(**extra) -> str:
    """The stored payload re-dumped with ``extra`` keys appended at the end."""
    doc = json.loads(_PAYLOAD)
    doc.update(extra)
    return json.dumps(doc, default=str)


def _check(payload: str) -> str | None:
    return published_mismatch([[_TOPIC, payload]], [[_TOPIC, _PAYLOAD]], sim_clock=_CLOCK, time_step=TIME_STEP)


def test_identical_strings_pass():
    """Verified to have teeth: making the exact-match branch return a message failed it."""
    assert _check(_PAYLOAD) is None


def test_appended_tick_keys_equal_to_the_harness_tick_pass():
    """Verified to have teeth: deleting the key-removal loop failed it."""
    assert _check(_with(sim_clock=_CLOCK, time_step=TIME_STEP)) is None


def test_wrong_time_step_fails():
    """Verified to have teeth: stripping the keys without checking the step failed it."""
    assert "time_step" in (_check(_with(sim_clock=_CLOCK, time_step=TIME_STEP + 1)) or "")


def test_bool_time_step_fails():
    """``True == 1`` in Python; a bool must never stand in for the step number.

    Verified to have teeth: replacing ``type(...) is not int`` with ``isinstance(..., int)``
    failed it.
    """
    doc = _with(sim_clock=_CLOCK, time_step=True)
    assert published_mismatch([[_TOPIC, doc]], [[_TOPIC, _PAYLOAD]], sim_clock=_CLOCK, time_step=1)


def test_only_one_tick_key_fails():
    """Verified to have teeth: disabling the one-key branch failed it."""
    assert "only" in (_check(_with(sim_clock=_CLOCK)) or "")


def test_any_other_added_key_fails():
    """Verified to have teeth: dropping keys absent from the stored payload before re-dumping
    let it pass (failed the test)."""
    assert _check(_with(sim_clock=_CLOCK, time_step=TIME_STEP, extra=1))


def test_changed_benefit_value_fails():
    """Verified to have teeth: copying the stored benefit_km back before re-dumping failed it."""
    doc = json.loads(_PAYLOAD)
    doc["benefit_km"] = 1.234
    doc.update(sim_clock=_CLOCK, time_step=TIME_STEP)
    assert _check(json.dumps(doc, default=str))


def test_reordered_payload_keys_fail():
    """Key order is part of the wire string. Verified to have teeth: comparing parsed dicts
    instead of the re-dumped string failed it."""
    doc = json.loads(_PAYLOAD)
    reordered = dict(reversed(list(doc.items())))
    reordered.update(sim_clock=_CLOCK, time_step=TIME_STEP)
    assert _check(json.dumps(reordered, default=str))


def test_tick_keys_anywhere_but_the_end_fail():
    """The transport APPENDS the pair; the same keys elsewhere are a different payload.

    Verified to have teeth: removing the last-two-keys check let the keys-first payload pass.
    """
    doc = {"sim_clock": _CLOCK, "time_step": TIME_STEP, **json.loads(_PAYLOAD)}
    assert "last two keys" in (_check(json.dumps(doc, default=str)) or "")
