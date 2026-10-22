"""Capture the per-policy golden heads (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.8).

Responsibility: run every registered dispatch policy (or one, ``--policy NAME``) at
its defaults on every whole-tick synthetic fixture of ``policies.testing.fixtures``
and record its heads in ``tests/golden/policy_golden_v2.json.gz`` as format 2:
``{"format": 2, "policies": {name: {version, cases: [{fixture, heads}]}}}``.
``tests/test_policy_golden_versions.py`` recomputes the cases with
:func:`golden_cases` and fails when a policy's output moved but its ``version=`` did
not.

Format 2 (round 2, ``docs/plans/policy_slots_round2_whole_tick.md`` §5 C5) is a
HARNESS change only: each case is one whole tick (``Fixture.context`` ->
``runner.tick_context``), decided with nothing injected (the context's
``Random(tick_seed)`` is the randomness input, the module RNG seeded with the
fixture's ``seed``). The format-1 file (``policy_golden.json.gz``: one partition scope
per case, with and without an injected tiebreak) is deleted. GreedyNearest and
RandomAssignment keep version "1": the dispatch golden (``dispatch_golden_v1``)
proves their behaviour unchanged; only how this file exercises them changed.

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

GOLDEN_PATH = Path(__file__).resolve().parent / "policy_golden_v2.json.gz"
GOLDEN_FORMAT = 2
GENERATOR_COMMAND = "tests/golden/capture_policy_golden.py"

__all__ = ["GOLDEN_PATH", "GOLDEN_FORMAT", "golden_cases", "load_golden", "load_golden_file", "encode_golden", "main"]


def golden_cases(name: str) -> list[dict[str, Any]]:
    """The cases recorded for dispatch policy ``name``: its heads on each whole-tick fixture."""
    cases = []
    for make in fx.ROBUSTNESS_FIXTURES:
        fixture = make()
        _ctx, plan = decide_seeded(build_runner_for_policy(name), fixture)
        cases.append({"fixture": fixture.id, "heads": [list(h) for h in plan.heads()]})
    return cases


def load_golden_file(path: Path = GOLDEN_PATH) -> dict[str, Any]:
    """The decoded golden file, ``{"format": 2, "policies": {}}`` when it does not exist yet.

    Raises ``ValueError`` for another format.
    """
    if not path.exists():
        return {"format": GOLDEN_FORMAT, "policies": {}}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict) or doc.get("format") != GOLDEN_FORMAT:
        raise ValueError(f"{path}: policy golden format {doc.get('format') if isinstance(doc, dict) else doc!r}, "
                         f"expected {GOLDEN_FORMAT}")
    return doc


def load_golden(path: Path = GOLDEN_PATH) -> dict[str, Any]:
    """``{name: {version, cases}}`` of the golden file (``{}`` when it does not exist yet)."""
    return dict(load_golden_file(path)["policies"])


def encode_golden(golden: dict[str, Any]) -> bytes:
    """Canonical bytes: sorted keys, compact separators, gzip with mtime 0."""
    payload = json.dumps(golden, sort_keys=True, separators=(",", ":")).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(payload)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy", action="append", help="planner policy to (re)capture; default: all")
    args = parser.parse_args(argv)
    names = args.policy or [spec.name for spec in list_policies("planner")]
    golden = load_golden()
    for name in names:
        spec = get_policy_spec("planner", name)
        golden[name] = {"version": spec.version, "cases": golden_cases(name)}
        print(f"captured {name} v{spec.version}: {len(golden[name]['cases'])} cases")
    GOLDEN_PATH.write_bytes(encode_golden({"format": GOLDEN_FORMAT, "policies": golden}))
    print(f"wrote {GOLDEN_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
