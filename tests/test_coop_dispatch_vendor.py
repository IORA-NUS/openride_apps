"""The vendored cooperative-dispatch solver is upstream's, byte for byte where marked verbatim,
and with every OpenRide option off it builds the SAME model and returns the SAME output as the
pristine upstream (plan N2: ``tests/data/coop_dispatch_upstream_golden.json``).

Skipped cleanly where OR-Tools is absent (the main venv): the package imports ``ortools``
at module top, as upstream does.

Verified to have teeth (mutate / see it fail / restore):

- swapping the order of the two ``in == a`` / ``out == a`` constraints in
  ``network_flow_solver.py`` failed ``test_options_off_is_upstream`` (LP sha256) on all
  8 instances, both arms;
- de-duplicating the edge index (``dict.fromkeys(edges)``) failed it on ``inst8``, the
  golden instance with duplicate ``edges_input`` entries;
- a one-byte edit to ``edge_builder.py`` failed ``test_verbatim_files_match_vendor_md``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("ortools")

from apps.container_logistics.policies.solver_lib.coop_dispatch import (  # noqa: E402
    base_solver,
    network_flow_solver as nfs,
)
from tests._coop_dispatch_instances import canonical, golden_instances, strip_timings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "apps/container_logistics/policies/solver_lib/coop_dispatch"
GOLDEN = json.loads((REPO / "tests/data/coop_dispatch_upstream_golden.json").read_text())


def _vendor_table():
    rows = re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \| (\w+) \|", (PKG / "VENDOR.md").read_text(), re.M)
    return {f: (sha, status) for f, sha, status in rows}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_verbatim_files_match_vendor_md():
    table = _vendor_table()
    assert len(table) == 8
    verbatim = {f for f, (_, st) in table.items() if st == "verbatim"}
    assert verbatim == {"__init__.py", "edge_builder.py", "fairness_solver.py",
                        "lib/__init__.py", "lib/api.py", "lib/schema.py"}
    for f in verbatim:
        assert _sha(PKG / f) == table[f][0], f"{f} differs from the upstream copy"
    for f, (sha, st) in table.items():
        assert st in {"verbatim", "modified"}
        if st == "modified":
            assert _sha(PKG / f) != sha, f"{f} is marked modified but is byte-identical to upstream"


def test_vendor_table_names_every_vendored_module():
    new = {"time_window_edges.py", "chains.py", "min_cost_flow.py"}
    present = {str(p.relative_to(PKG)) for p in PKG.rglob("*.py")}
    assert present - new == set(_vendor_table())


def _solve_capturing(params):
    seen = []
    real = base_solver.create_solver_with_fallback

    def spy(*a, **kw):
        s = real(*a, **kw)
        seen.append(s)
        return s

    orig = nfs.create_solver_with_fallback
    nfs.create_solver_with_fallback = spy
    try:
        result, stats = nfs.NetworkFlowSolver().solve(params)
    finally:
        nfs.create_solver_with_fallback = orig
    s = seen[-1]
    model = {"lp_sha256": hashlib.sha256(s.ExportModelAsLpFormat(False).encode()).hexdigest(),
             "num_constraints_model": s.NumConstraints(),
             "num_variables_model": s.NumVariables()}
    return result, stats, model


@pytest.mark.parametrize("k", range(len(GOLDEN["instances"])), ids=[g["instance_id"] for g in GOLDEN["instances"]])
@pytest.mark.parametrize("explicit_off", [False, True], ids=["absent", "explicit-off"])
def test_options_off_is_upstream(k, explicit_off):
    params = golden_instances()[k]
    gold = GOLDEN["instances"][k]
    assert params["instance_id"] == gold["instance_id"]
    if explicit_off:
        params = dict(params, start_nodes=None, defer_penalty=None, eligible=None,
                      time_limit_s=None, mip_gap=None, accept_feasible=False)
    result, stats, model = _solve_capturing(params)
    assert model == gold["model"], "the exported LP model (constraints, order, variables) changed"
    assert canonical(result) == gold["result"]
    assert canonical(strip_timings(stats)) == gold["stats"]
    # The formula count upstream reports is unchanged, and so is the real one.
    assert stats["num_constraints"] == gold["stats"]["num_constraints"]
    assert model["num_constraints_model"] == gold["model"]["num_constraints_model"]


def test_golden_covers_both_paths_and_duplicate_edges():
    statuses = [g["result"]["status"] for g in GOLDEN["instances"]]
    assert len(statuses) >= 6 and 0 in statuses and 2 in statuses
    assert any(len(p["edges_input"]) != len(set(p["edges_input"])) for p in golden_instances())


def test_upstream_typed_api_still_runs():
    from datetime import datetime, timedelta

    from apps.container_logistics.policies.solver_lib.coop_dispatch.lib import Order, solve_network_flow

    t = datetime(2020, 1, 1, 8)
    orders = [Order("o1", "A", "p1", "p2", t, t + timedelta(minutes=30)),
              Order("o2", "A", "p2", "p3", t + timedelta(hours=1), t + timedelta(hours=2))]
    lookup = {(a, b): {"distance": 1000.0, "duration": 600.0}
              for a in ("p1", "p2", "p3") for b in ("p1", "p2", "p3")}
    result, stats = solve_network_flow(orders, {"A": 1}, {("A", "A")}, lookup, use_trailer_cost=False)
    assert result["status"] == 0 and stats["objective"] == 1000.0
