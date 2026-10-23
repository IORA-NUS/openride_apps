"""The run stamp, run settings and run name of every golden case equal the STORED golden.

Final review round 2, G3: the stamp (``run_config.meta.policies``), the run settings a
solver's ``stamp_settings`` add and the run-name segments were only compared between a
run and its renamed copy, so a branch keyed on the class or the MRO moved both sides
alike: the reviewer's B1 (``world_reads`` tampered in ``policies_stamp`` for a central
``RandomAssignment``) and B4 (``planner.timing`` -> ``"plan"`` in
``_stamp_effective_policies``) survived the full suite, and B2/B3/C2 (an extra run-name
segment in ``_apply_run_declaration``) failed only incidentally. These tests compare
against ``tests/golden/run_stamp_v1.json.gz``: 102 cases (every ``dispatch_golden_v1``
case and every central-top case), captured on unmodified code at ``6647aaf`` and
byte-identical to a capture at ``44922b4``, by the shared module
``tests/golden/capture_run_stamp_golden.py``, which drives the sim main's own ``main()``
(so its ``_stamp_effective_policies`` -> ``policies_stamp``, ``_apply_run_declaration``
and run-name builder) and replaces only the I/O edges.

One dedicated test per surface, each over all 102 cases:

- :func:`test_the_run_stamp_is_the_golden` -- ``POLICIES`` in full (B1, B4);
- :func:`test_the_run_settings_are_the_golden` -- every other run setting ``main()``
  wrote (``COOPERATION``, a solver's ``stamp_settings``), and the bound declaration's
  ``stamp_settings``;
- :func:`test_the_run_name_is_the_golden` -- the run name, its segments, and the bound
  declaration's ``run_name_segments`` (B2, B3, C2).

Verified to have teeth (R3B; the reviewer's scripts ``r2_fb/batch*.sh`` replayed in a
private copy of the tree, each mutation alone, ``git diff`` clean after): B1 failed
:func:`test_the_run_stamp_is_the_golden` on the 12 central ``RandomAssignment`` cases;
B4 failed it on the same 12; B2, B3 and C2 each failed
:func:`test_the_run_name_is_the_golden` on those 12. See the R3B hand-back for the runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.golden.capture_run_stamp_golden import (
    CASE_ENV,
    drive_main,
    golden_cases,
    load_stamp_golden,
)

_GOLDEN = load_stamp_golden()
_EXPECTED = {c["case_id"]: c for c in _GOLDEN["cases"]}
_PROFILES = {c["case_id"]: c["profile"] for c in golden_cases()}
_GOT: dict[str, dict[str, Any]] = {}


def _got(case_id: str) -> dict[str, Any]:
    """``drive_main`` once per case per session (three tests read it)."""
    if case_id not in _GOT:
        _GOT[case_id] = drive_main(_PROFILES[case_id])
    return _GOT[case_id]


def _want(case_id: str) -> dict[str, Any]:
    return _EXPECTED[case_id]["expected"]


def test_the_run_stamp_fixture_is_what_it_claims():
    assert _GOLDEN["format"] == 1 and _GOLDEN["header"]["commit"] == "6647aaf"
    assert _GOLDEN["header"]["env"] == CASE_ENV
    assert set(_EXPECTED) == set(_PROFILES) and len(_EXPECTED) == 102
    sources = [c["source"] for c in _GOLDEN["cases"]]
    assert sources.count("dispatch_golden_v1.json.gz") == 68 and sources.count("central_top_v1.json.gz") == 34
    tops = {c["expected"]["stamp"]["planner"]["name"] for c in _GOLDEN["cases"]}
    assert tops == {"Partitioned", "PooledMarket", "GreedyNearest", "RandomAssignment"}, tops
    # Not vacuous: no case exits, each stores a declaration, and the declared segments are used.
    assert all(c["expected"]["exit"] is None for c in _GOLDEN["cases"])
    assert all(c["expected"]["declared_run_name_segments"] for c in _GOLDEN["cases"])
    assert {seg for c in _GOLDEN["cases"] for seg in c["expected"]["segments"]} == {
        "base", "chain", "no-coop", "lifecycle:agents"}


@pytest.mark.parametrize("case_id", sorted(_EXPECTED))
def test_the_run_stamp_is_the_golden(case_id):
    got, want = _got(case_id), _want(case_id)
    assert got["exit"] == want["exit"], f"{case_id}: main() exit differs"
    assert got["stamp"] == want["stamp"], (
        f"{case_id}: run_config.meta.policies differs from the run-stamp golden: "
        + ", ".join(sorted(k for k in set(got["stamp"]["planner"]) | set(want["stamp"]["planner"])
                           if got["stamp"]["planner"].get(k) != want["stamp"]["planner"].get(k))))


@pytest.mark.parametrize("case_id", sorted(_EXPECTED))
def test_the_run_settings_are_the_golden(case_id):
    got, want = _got(case_id), _want(case_id)
    assert got["stamp_settings"] == want["stamp_settings"], f"{case_id}: the run settings differ"
    assert got["declared_stamp_settings"] == want["declared_stamp_settings"], (
        f"{case_id}: the bound declaration's stamp_settings differ")


@pytest.mark.parametrize("case_id", sorted(_EXPECTED))
def test_the_run_name_is_the_golden(case_id):
    got, want = _got(case_id), _want(case_id)
    assert got["run_name"] == want["run_name"], f"{case_id}: {got['run_name']!r} != {want['run_name']!r}"
    assert got["segments"] == want["segments"]
    assert got["declared_run_name_segments"] == want["declared_run_name_segments"], (
        f"{case_id}: the bound declaration's run_name_segments differ")


def test_drive_main_runs_the_sim_mains_own_functions(monkeypatch):
    """The replay goes through ``policies_stamp`` and ``_apply_run_declaration``, not around them."""
    import apps.container_logistics.policies.framework as framework
    import apps.simulation.run_container_logistics_simulation as sim_main

    calls: list[str] = []
    real_stamp, real_declare = framework.policies_stamp, sim_main._apply_run_declaration

    def stamp(*a: Any, **k: Any) -> Any:
        calls.append("policies_stamp")
        return real_stamp(*a, **k)

    def declare(*a: Any, **k: Any) -> Any:
        calls.append("_apply_run_declaration")
        return real_declare(*a, **k)

    monkeypatch.setattr(framework, "policies_stamp", stamp)
    monkeypatch.setattr(sim_main, "_apply_run_declaration", declare)
    case_id = "central:T-random-partitioned-chain-spatial-cap500"
    assert drive_main(_PROFILES[case_id]) == _want(case_id)
    assert calls == ["policies_stamp", "_apply_run_declaration"], calls
