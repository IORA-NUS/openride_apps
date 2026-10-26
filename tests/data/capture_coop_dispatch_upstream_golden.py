"""Record the PRISTINE upstream NetworkFlowSolver's output on the fixed golden instances.

Run ONLY against the untouched upstream copy (never the vendored, option-extended one):

    <venv with ortools>/bin/python tests/data/capture_coop_dispatch_upstream_golden.py \
        /home/user/solvers/dispatch_solvers/standalone

It refuses to run unless every upstream file's sha256 matches VENDOR.md, then writes
``tests/data/coop_dispatch_upstream_golden.json``: per instance the result and the stats
(timings removed), the solver's real constraint/variable counts, and the sha256 of the
model exported in LP format (variable names, constraints and their ORDER).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tests._coop_dispatch_instances import canonical, golden_instances, strip_timings  # noqa: E402

VENDOR_MD = REPO / "apps/container_logistics/policies/solver_lib/coop_dispatch/VENDOR.md"
OUT = Path(__file__).resolve().parent / "coop_dispatch_upstream_golden.json"


def _check_upstream(upstream: Path) -> dict[str, str]:
    table = dict(re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|", VENDOR_MD.read_text(), re.M))
    if not table:
        raise SystemExit(f"no sha256 table in {VENDOR_MD}")
    for rel, sha in table.items():
        got = hashlib.sha256((upstream / rel).read_bytes()).hexdigest()
        if got != sha:
            raise SystemExit(f"{upstream / rel} is not the pristine upstream file ({got} != {sha})")
    return table


def capture_model(nfs_module, base_module):
    """Wrap the module-level ``create_solver_with_fallback`` so the built model survives."""
    seen = []
    real = base_module.create_solver_with_fallback

    def spy(*args, **kwargs):
        s = real(*args, **kwargs)
        seen.append(s)
        return s

    nfs_module.create_solver_with_fallback = spy
    return seen


def model_fingerprint(solver) -> dict:
    lp = solver.ExportModelAsLpFormat(False)
    return {"lp_sha256": hashlib.sha256(lp.encode()).hexdigest(),
            "num_constraints_model": solver.NumConstraints(),
            "num_variables_model": solver.NumVariables()}


def main(upstream: str) -> None:
    upstream_path = Path(upstream).resolve()
    _check_upstream(upstream_path)
    sys.path.insert(0, str(upstream_path.parent))
    pkg = upstream_path.name
    nfs = importlib.import_module(f"{pkg}.network_flow_solver")
    base = importlib.import_module(f"{pkg}.base_solver")
    assert Path(nfs.__file__).resolve().parent == upstream_path
    out = {"upstream": str(upstream_path), "instances": []}
    for params in golden_instances():
        seen = capture_model(nfs, base)
        result, stats = nfs.NetworkFlowSolver().solve(params)
        out["instances"].append({
            "instance_id": params["instance_id"],
            "result": canonical(result),
            "stats": canonical(strip_timings(stats)),
            "model": model_fingerprint(seen[-1]),
        })
    OUT.write_text(json.dumps(out, indent=1, sort_keys=False) + "\n")
    print(f"wrote {OUT} ({len(out['instances'])} instances)")


if __name__ == "__main__":
    main(sys.argv[1])
