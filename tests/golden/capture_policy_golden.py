"""Capture the per-policy golden heads (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.8).

Responsibility: run every registered dispatch policy (or one, ``--policy NAME``) on
the synthetic fixtures of ``policies.testing.fixtures`` and record its heads in
``tests/golden/policy_golden.json.gz`` as ``{name: {version, cases: [{fixture,
tiebreak, heads}]}}``. ``tests/test_policy_golden_versions.py`` recomputes the cases
with :func:`golden_cases` and fails when a policy's output moved but its
``version=`` did not.

Run from the repo root (either form)::

    venv/bin/python tests/golden/capture_policy_golden.py --policy GreedyNearest
    venv/bin/python -m tests.golden.capture_policy_golden

``--policy`` replaces that entry only and keeps the others. Byte-stable: sorted
keys, compact separators, gzip ``mtime=0``. Unlike the dispatch golden (Step G0),
this one is MEANT to be regenerated, together with a version bump.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # run as a script: `python tests/golden/capture_policy_golden.py`
    sys.path.insert(0, str(REPO_ROOT))

from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.framework.registry import get_policy_spec, list_policies
from apps.container_logistics.policies.testing import fixtures as fx
from apps.container_logistics.policies.testing.battery import decide_seeded

GOLDEN_PATH = Path(__file__).resolve().parent / "policy_golden.json.gz"
GENERATOR_COMMAND = "tests/golden/capture_policy_golden.py"

__all__ = ["GOLDEN_PATH", "golden_cases", "load_golden", "encode_golden", "main"]

# (fixture factory, tiebreak seed): every synthetic fixture on the legacy partitioned
# path (no tiebreak), plus the two where the pooled tiebreak decides the outcome.
_TIEBREAK = 4242
_CASES = [(make, None) for make in fx.ROBUSTNESS_FIXTURES] + [(fx.tied_costs, _TIEBREAK), (fx.scale, _TIEBREAK)]


def golden_cases(name: str) -> list[dict[str, Any]]:
    """The cases recorded for dispatch policy ``name``: heads per (fixture, tiebreak)."""
    cases = []
    for make, tiebreak in _CASES:
        fixture = make()
        _ctx, plan = decide_seeded(build_runner_for_policy(name), fixture, seed=fixture.seed, tiebreak=tiebreak)
        cases.append({"fixture": fixture.id, "tiebreak": tiebreak, "heads": [list(h) for h in plan.heads()]})
    return cases


def load_golden(path: Path = GOLDEN_PATH) -> dict[str, Any]:
    """The decoded golden file; ``{}`` when it does not exist yet."""
    if not path.exists():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def encode_golden(golden: dict[str, Any]) -> bytes:
    """Canonical bytes: sorted keys, compact separators, gzip with mtime 0."""
    payload = json.dumps(golden, sort_keys=True, separators=(",", ":")).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(payload)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy", action="append", help="dispatch policy to (re)capture; default: all")
    args = parser.parse_args(argv)
    names = args.policy or [spec.name for spec in list_policies("dispatch")]
    golden = load_golden()
    for name in names:
        spec = get_policy_spec("dispatch", name)
        golden[name] = {"version": spec.version, "cases": golden_cases(name)}
        print(f"captured {name} v{spec.version}: {len(golden[name]['cases'])} cases")
    GOLDEN_PATH.write_bytes(encode_golden(golden))
    print(f"wrote {GOLDEN_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
