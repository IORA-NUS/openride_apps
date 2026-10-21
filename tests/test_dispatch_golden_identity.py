"""Dispatch golden identity: today's assignment outputs, locked byte for byte.

What is locked: for 68 stored cases (tick states A/B/C over the compiled
``collab_scale_500t_v2`` and ``multi_haulier_greedy_solver_500_trucks`` worlds,
plus the all-ties world T), the REAL ``AssignmentApp.assign`` + ``publish``
must reproduce, exactly, the ordered ``(truck, order)`` pairs, the share tags,
every published MQTT payload STRING, the pooled ``round_stats()``, and the next
draw of the ``random`` module after the tick (which locks how many module-RNG
draws the partitioned path consumes).

Why: the policy-slots refactor (docs/plans/policy_slots_p0_p3.md, Step G0)
moves the dispatch decision behind a new runner. Runs before and after must
stay comparable, so the refactor is required to be byte-identical on every
existing configuration. The fixture was captured from unmodified code at the
commit named in its header; regenerate it only with a stated comparability
boundary (``python -m tests.golden.capture_dispatch_golden``).

Every case is replayed through ``tests/golden/harness.py``, the same harness the
capture used, so a mismatch can only come from production code.

Verified to have teeth (each mutation reverted afterwards):
replacing ``self.rng.shuffle(orders)`` with an id sort in
``solver/random_assignment.py`` failed all 24 RandomAssignment cases; dropping
the legacy ``self.rng.shuffle(scored)`` in ``greedy_nearest.solve_pairs`` failed
all 22 partitioned Greedy cases; swapping the ``pair_tiebreak`` argument order
on the pooled branch failed all 22 pooled Greedy cases.
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.golden.capture_dispatch_golden import FIXTURE_PATH, FORMAT_VERSION, check_teeth
from tests.golden.harness import run_case

CAPTURE_COMMIT = "ec085f5"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_fixture() -> dict[str, Any]:
    with gzip.open(FIXTURE_PATH, "rb") as fh:
        return json.loads(fh.read())


_FIXTURE = _load_fixture()
_CASES = {c["case_id"]: c for c in _FIXTURE["cases"]}


def _first_difference(actual: Any, expected: Any) -> str:
    """Human-readable location of the first difference between two lists."""
    if isinstance(actual, list) and isinstance(expected, list):
        for i, (a, e) in enumerate(zip(actual, expected)):
            if a != e:
                return f"first differing index {i}: got {str(a)[:300]!r}, want {str(e)[:300]!r}"
        return f"length differs: got {len(actual)}, want {len(expected)}"
    return f"got {str(actual)[:300]!r}, want {str(expected)[:300]!r}"


@pytest.mark.parametrize("case_id", sorted(_CASES))
def test_golden_case_is_byte_identical(case_id):
    case = _CASES[case_id]
    # JSON round trip so tuples/lists and float reprs compare like the stored form.
    actual = json.loads(json.dumps(run_case(case, _FIXTURE["tick_states"])))
    expected = case["expected"]
    for field in ("pairs", "share_tags", "published", "round_stats", "rng_probe"):
        assert actual[field] == expected[field], (
            f"{case_id}: '{field}' is no longer byte-identical to the golden capture "
            f"({_FIXTURE['header']['capture_commit']}); {_first_difference(actual[field], expected[field])}"
        )


def test_golden_fixture_header_records_capture_commit():
    header = _FIXTURE["header"]
    assert header["format_version"] == FORMAT_VERSION
    assert header["capture_commit"] == CAPTURE_COMMIT, (
        f"fixture was captured on {header['capture_commit']!r}, the replay test pins "
        f"{CAPTURE_COMMIT!r}; a recapture is a comparability boundary and must update both"
    )
    assert re.fullmatch(r"[0-9a-f]{7,40}", header["capture_commit"])
    assert header["generator_command"] == "python -m tests.golden.capture_dispatch_golden"
    assert header["case_count"] == len(_FIXTURE["cases"])
    try:
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", header["capture_commit"], "HEAD"],
            cwd=REPO_ROOT, capture_output=True,
        )
    except FileNotFoundError:  # pragma: no cover - no git in this environment
        return
    if ancestor.returncode in (0, 1):
        assert ancestor.returncode == 0, (
            f"capture commit {header['capture_commit']} is not an ancestor of HEAD"
        )


def test_golden_fixture_has_teeth():
    """Re-runs the capture's teeth checks on the stored data, and checks they match."""
    teeth = check_teeth(_FIXTURE)
    assert teeth == _FIXTURE["header"]["teeth"], (
        f"stored teeth summary {_FIXTURE['header']['teeth']} != recomputed {teeth}"
    )
    assert 60 <= teeth["cases"] <= 80, f"case grid drifted to {teeth['cases']} cases"
