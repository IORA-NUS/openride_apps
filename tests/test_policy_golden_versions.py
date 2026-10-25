"""Per-policy golden heads: a dispatch policy's output may change only with a version bump.

What is locked: ``tests/golden/policy_golden_v2.json.gz`` is format 2 and has an entry
for every registered dispatch policy; each policy's heads on every whole-tick
synthetic fixture (``policies.testing.fixtures``, decided with nothing injected) equal
the recorded ones while its ``@policy(version=...)`` equals the recorded version (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P3.8; round 2 §5 C5). GreedyNearest and
RandomAssignment are still at "1" (the dispatch golden proves their behaviour
unchanged; format 2 changed only the harness), and Partitioned and PooledMarket have
"1" entries. The format-1 file is gone.

Why: runs are compared across policy versions. A behaviour change that kept its
version would silently make "GreedyNearest v1" mean two different things; the
version is the comparability boundary for a policy (CLAUDE.md §6.3 / §6.16 class).

Verified to have teeth (each mutation restored after, files compared by hash):
swapping the arguments of ``pair_tiebreak`` in GreedyNearest's tie-break key failed
``test_policy_output_unchanged_or_version_bumped[GreedyNearest]`` with the bump
message; making ``Partitioned.decide`` iterate its groups in reverse failed
``test_policy_output_unchanged_or_version_bumped[Partitioned]``; making
``PooledMarket`` default ``max_rounds`` 1 failed
``test_policy_output_unchanged_or_version_bumped[PooledMarket]``; dropping
``Partitioned`` from the golden file failed ``test_every_dispatch_policy_has_a_golden_entry``
and ``test_builtin_versions_in_the_v2_golden``.
"""

from __future__ import annotations

import pytest

from apps.container_logistics.policies.framework.registry import list_policies
from tests.golden.capture_policy_golden import (
    GENERATOR_COMMAND,
    GOLDEN_FORMAT,
    GOLDEN_PATH,
    golden_cases,
    load_golden,
    load_golden_file,
)

_NAMES = [spec.name for spec in list_policies("planner")]


def test_golden_file_is_format_2_and_format_1_is_deleted():
    assert GOLDEN_PATH.name == "policy_golden_v2.json.gz" and GOLDEN_PATH.exists()
    assert load_golden_file()["format"] == GOLDEN_FORMAT == 2
    assert not (GOLDEN_PATH.parent / "policy_golden.json.gz").exists(), "the format-1 golden must be deleted"


def test_builtin_versions_in_the_v2_golden():
    golden = load_golden()
    assert {n: golden[n]["version"] for n in ("GreedyNearest", "RandomAssignment", "Partitioned", "PooledMarket")} \
        == {"GreedyNearest": "1", "RandomAssignment": "1", "Partitioned": "1", "PooledMarket": "1"}


def test_every_dispatch_policy_has_a_golden_entry():
    missing = sorted(set(_NAMES) - set(load_golden()))
    assert not missing, (
        f"planner policies {missing} have no golden entry; capture one with "
        f"{GENERATOR_COMMAND} --policy <NAME>"
    )


@pytest.mark.parametrize("name", _NAMES)
def test_policy_output_unchanged_or_version_bumped(name):
    entry = load_golden().get(name)
    if entry is None:
        pytest.skip(f"{name} has no golden entry (test_every_dispatch_policy_has_a_golden_entry fails)")
    spec = next(s for s in list_policies("planner") if s.name == name)
    if entry["version"] != spec.version:
        pytest.fail(
            f"{name} is v{spec.version} but its golden entry is v{entry['version']}: regenerate with "
            f"{GENERATOR_COMMAND} --policy {name}"
        )
    missing = spec.missing_requirements()
    if missing:
        # The golden was captured with the policy's own requirements; without them
        # it cannot be reproduced (cf. the battery's ``performance.plan`` SKIP).
        pytest.skip(f"{name} requires {missing}, not installed in this interpreter")
    recorded = {c["fixture"]: c["heads"] for c in entry["cases"]}
    cases = golden_cases(name)
    assert sorted(recorded) == sorted(c["fixture"] for c in cases), (
        f"{name}: the golden's fixtures differ from the current fixture set; regenerate with "
        f"{GENERATOR_COMMAND} --policy {name}"
    )
    assert any(c["heads"] for c in cases), f"{name} commits nothing on any fixture; the comparison would be vacuous"
    for case in cases:
        if recorded.get(case["fixture"]) != case["heads"]:
            pytest.fail(
                f"{name} v{spec.version} output changed on fixture {case['fixture']} but its version did not: "
                f"bump version= in @policy and regenerate with {GENERATOR_COMMAND} --policy {name}"
            )
