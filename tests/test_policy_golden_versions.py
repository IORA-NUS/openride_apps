"""Per-policy golden heads: a dispatch policy's output may change only with a version bump.

What is locked: every registered dispatch policy has an entry in
``tests/golden/policy_golden.json.gz``, and its heads on the synthetic fixtures
(``policies.testing.fixtures``, with and without the pooled tiebreak) equal the
recorded ones while its ``@policy(version=...)`` equals the recorded version (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P3.8).

Why: runs are compared across policy versions. A behaviour change that kept its
version would silently make "GreedyNearest v1" mean two different things; the
version is the comparability boundary for a policy (CLAUDE.md §6.3 / §6.16 class).

Verified to have teeth: changing GreedyNearest's tie-break key to sort by
``pair_tiebreak(seed, truck, order)`` (arguments swapped) failed
``test_policy_output_unchanged_or_version_bumped[GreedyNearest]`` with the bump
message; removing RandomAssignment's entry failed
``test_every_dispatch_policy_has_a_golden_entry``.
"""

from __future__ import annotations

import pytest

from apps.container_logistics.policies.framework.registry import list_policies
from tests.golden.capture_policy_golden import GENERATOR_COMMAND, golden_cases, load_golden

_NAMES = [spec.name for spec in list_policies("dispatch")]


def test_every_dispatch_policy_has_a_golden_entry():
    missing = sorted(set(_NAMES) - set(load_golden()))
    assert not missing, (
        f"dispatch policies {missing} have no golden entry; capture one with "
        f"{GENERATOR_COMMAND} --policy <NAME>"
    )


@pytest.mark.parametrize("name", _NAMES)
def test_policy_output_unchanged_or_version_bumped(name):
    entry = load_golden().get(name)
    if entry is None:
        pytest.skip(f"{name} has no golden entry (test_every_dispatch_policy_has_a_golden_entry fails)")
    spec = next(s for s in list_policies("dispatch") if s.name == name)
    if entry["version"] != spec.version:
        pytest.fail(
            f"{name} is v{spec.version} but its golden entry is v{entry['version']}: regenerate with "
            f"{GENERATOR_COMMAND} --policy {name}"
        )
    recorded = {(c["fixture"], c["tiebreak"]): c["heads"] for c in entry["cases"]}
    for case in golden_cases(name):
        key = (case["fixture"], case["tiebreak"])
        if recorded.get(key) != case["heads"]:
            fixture = case["fixture"] if case["tiebreak"] is None else f"{case['fixture']} (tiebreak {case['tiebreak']})"
            pytest.fail(
                f"{name} v{spec.version} output changed on fixture {fixture} but its version did not: "
                f"bump version= in @policy and regenerate with {GENERATOR_COMMAND} --policy {name}"
            )
