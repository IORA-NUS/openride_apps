"""Published-payload comparison for the dispatch golden (review F9).

Responsibility: decide whether the MQTT payloads a golden replay published equal the
stored ones. Exact string equality is the rule. The single tolerated difference is
the main tree's Kafka-transport work, which appends ``sim_clock`` and ``time_step``
to every ``AssignedHaulTripPayload``: when BOTH keys are present and both equal the
harness tick, they are removed and the remainder must then re-dump to the stored
string exactly (key order, float repr and escaping included). Anything else -- one
key alone, a wrong value, a ``bool`` time step, any other added key, a reordered or
changed field -- is a mismatch.

Tests only; imports nothing from production code.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

__all__ = ["TRANSPORT_TICK_KEYS", "published_mismatch"]

TRANSPORT_TICK_KEYS = ("sim_clock", "time_step")


def _payload_mismatch(actual: str, expected: str, *, sim_clock: str, time_step: int) -> str | None:
    if actual == expected:
        return None
    try:
        doc = json.loads(actual)
    except ValueError as exc:
        return f"payload is not JSON ({exc})"
    present = [k for k in TRANSPORT_TICK_KEYS if isinstance(doc, dict) and k in doc]
    if not present:
        return "payload string differs and carries no transport tick keys"
    if len(present) == 1:
        return f"payload carries only {present[0]!r} of the transport tick keys {TRANSPORT_TICK_KEYS}"
    if doc["sim_clock"] != sim_clock or not isinstance(doc["sim_clock"], str):
        return f"sim_clock {doc['sim_clock']!r} != harness tick {sim_clock!r}"
    if type(doc["time_step"]) is not int or doc["time_step"] != time_step:
        return f"time_step {doc['time_step']!r} != harness step {time_step!r}"
    for key in TRANSPORT_TICK_KEYS:
        del doc[key]
    if json.dumps(doc, default=str) != expected:
        return "payload differs from the stored string after removing the transport tick keys"
    return None


def published_mismatch(
    actual: Sequence[Sequence[Any]],
    expected: Sequence[Sequence[Any]],
    *,
    sim_clock: str,
    time_step: int,
) -> str | None:
    """First mismatch between two ``[(topic, payload_string), ...]`` lists, or ``None``."""
    if len(actual) != len(expected):
        return f"published {len(actual)} payloads, want {len(expected)}"
    for i, ((a_topic, a_payload), (e_topic, e_payload)) in enumerate(zip(actual, expected)):
        if a_topic != e_topic:
            return f"index {i}: topic {a_topic!r} != {e_topic!r}"
        problem = _payload_mismatch(a_payload, e_payload, sim_clock=sim_clock, time_step=time_step)
        if problem:
            return f"index {i} ({e_topic}): {problem}"
    return None
