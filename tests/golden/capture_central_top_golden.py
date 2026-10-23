"""Capture the CENTRAL-TOP dispatch golden: every built-in leaf selected at top level.

Responsibility: the dispatch golden (``dispatch_golden_v1``) and the rename litmus only
ever run a tick topped by a decomposition (``Partitioned`` / ``PooledMarket``), so a
framework branch that only fires when a LEAF is the top -- e.g. ``publish()`` dropping
an assignment when the top is ``RandomAssignment`` (final review H3) -- passed every one
of them. This fixture records what the REAL ``AssignmentApp.assign`` / ``publish`` do
with each built-in leaf as a central top, on the stored v1 tick states.

Cases: every ``partitioned`` case of ``dispatch_golden_v1`` retargeted to its own leaf
(the profile's legacy selection is resolved, the ``inner`` of the resolved
``Partitioned`` -- the leaf with the case's params -- becomes the compiled top, the
legacy selection keys are dropped). Same tick states, same cooperation arms, same
candidate kinds and caps: 34 cases (22 ``GreedyNearest``, 12 ``RandomAssignment``).

What is stored per case: the committed pairs (ids, in order), the share tags (in full),
the module-RNG probe, and the published payloads as ``count`` + ``sha256`` of the
canonical list. Canonical = each payload with the main tree's two transport tick keys
removed under exactly the rule of ``tests/golden/compare.py`` (both present, last, in
order, equal to the harness tick), re-dumped with ``json.dumps(default=str)``; on this
branch the canonical string IS the published string. The tick states are NOT copied:
they are read from ``dispatch_golden_v1`` (frozen).

Run from the repo root on an UNMODIFIED tree (it refuses otherwise, like the v1
capture)::

    venv/bin/python -m tests.golden.capture_central_top_golden

Captured at ``44922b4`` (feat/policy-slots, all nine merges) before any fix-loop edit.
This module is shared by the capture and the replay
(``tests/test_solver_contract_central_top.py``), so a difference between them can only
come from production code. It imports the frozen harness read-only.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.golden.capture_dispatch_golden import FIXTURE_PATH as V1_PATH  # noqa: E402
from tests.golden.compare import TRANSPORT_TICK_KEYS  # noqa: E402
from tests.golden.harness import MODULE_SEED, TIME_STEP, build_app  # noqa: E402

CENTRAL_PATH = Path(__file__).resolve().parent / "central_top_v1.json.gz"
FORMAT_VERSION = 1
GENERATOR_COMMAND = "python -m tests.golden.capture_central_top_golden"
#: The legacy selection keys a retargeted profile drops (no OLD name survives anywhere).
LEGACY_SELECTION_PLANNER_KEYS = ("topology", "market", "deployment")

__all__ = ["CENTRAL_PATH", "central_profile", "canonical_published", "load_v1", "load_central",
           "run_central", "build_central_fixture"]


def load_v1() -> dict[str, Any]:
    with gzip.open(V1_PATH, "rb") as fh:
        return json.loads(fh.read())


def load_central() -> dict[str, Any]:
    with gzip.open(CENTRAL_PATH, "rb") as fh:
        return json.loads(fh.read())


def central_profile(v1_profile: dict[str, Any]) -> dict[str, Any]:
    """The v1 (legacy, partitioned) profile with its own LEAF compiled as the top."""
    from apps.container_logistics.policies.dispatch.profile import resolve_profile_policies

    profile = copy.deepcopy(v1_profile)
    resolved, source = resolve_profile_policies(profile)
    assert source == "legacy-profile", source
    block = resolved["planner"].to_profile()
    leaf = block["params"]["inner"]
    assert isinstance(leaf, dict) and "inner" not in (leaf.get("params") or {}), block
    planner = profile.pop("planner")
    profile.pop("use_spatial_matching", None)
    profile["policies"] = {"planner": leaf}
    profile["policies_format"] = 2
    profile["strategy"] = leaf["type"]
    profile["planner"] = {k: v for k, v in planner.items() if k not in LEGACY_SELECTION_PLANNER_KEYS}
    return profile


def canonical_published(published: list[list[str]], *, sim_clock: str) -> list[list[str]]:
    """``published`` with the transport tick keys removed under ``compare.py``'s rule."""
    out = []
    for topic, payload in published:
        doc = json.loads(payload)
        keys = tuple(doc)
        if keys[-2:] == TRANSPORT_TICK_KEYS and doc["sim_clock"] == sim_clock \
                and type(doc["time_step"]) is int and doc["time_step"] == TIME_STEP:
            for key in TRANSPORT_TICK_KEYS:
                del doc[key]
            payload = json.dumps(doc, default=str)
        out.append([topic, payload])
    return out


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=False, separators=(",", ":")).encode()).hexdigest()


def run_central(profile: dict[str, Any], tick_state: dict[str, Any]) -> dict[str, Any]:
    """Run one central-top case through the real ``assign`` / ``publish``; the raw outputs (JSON-ready)."""
    app = build_app(profile, profile["strategy"], tick_state)
    random.seed(MODULE_SEED)
    result = app.assign(tick_state["sim_clock"], TIME_STEP)
    app.publish(result)
    rng_probe = random.random()
    share_tags = sorted(([list(k), t] for k, t in (app._share_tags or {}).items()),
                        key=lambda row: (row[0][0], row[0][1]))
    return json.loads(json.dumps({
        "pairs": [[str(t.get("_id")), str(o.get("_id"))] for t, o in result],
        "share_tags": share_tags,
        "published": [list(p) for p in app.messenger.published],
        "rng_probe": rng_probe,
    }))


def summarise(outputs: dict[str, Any], *, sim_clock: str) -> dict[str, Any]:
    """What the fixture stores for one case's outputs."""
    published = canonical_published(outputs["published"], sim_clock=sim_clock)
    return {
        "pairs": outputs["pairs"],
        "share_tags": outputs["share_tags"],
        "rng_probe": outputs["rng_probe"],
        "published_count": len(published),
        "published_sha256": _sha(published),
    }


def central_cases(v1: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{case_id, tick_state, profile}]``: every partitioned v1 case retargeted to its leaf."""
    cases = []
    for case in v1["cases"]:
        if case["profile"]["planner"]["topology"] != "partitioned":
            continue
        cases.append({"case_id": f"central:{case['case_id']}", "tick_state": case["tick_state"],
                      "profile": central_profile(case["profile"])})
    return cases


def build_central_fixture() -> dict[str, Any]:
    v1 = load_v1()
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()
    out = []
    for case in central_cases(v1):
        state = v1["tick_states"][case["tick_state"]]
        outputs = run_central(case["profile"], state)
        out.append({**case, "expected": summarise(outputs, sim_clock=state["sim_clock"])})
    return {"format": FORMAT_VERSION, "header": {"commit": head, "generator": GENERATOR_COMMAND,
                                                 "tick_states_from": V1_PATH.name}, "cases": out}


def encode(doc: dict[str, Any]) -> bytes:
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(payload)
    return buf.getvalue()


def main() -> int:
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "apps"], cwd=REPO_ROOT, check=True,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print(f"refusing: apps/ differs from HEAD:\n{dirty}", file=sys.stderr)
        return 2
    doc = build_central_fixture()
    CENTRAL_PATH.write_bytes(encode(doc))
    shares = sum(1 for c in doc["cases"] if c["expected"]["share_tags"])
    print(f"wrote {CENTRAL_PATH} ({len(doc['cases'])} cases, {shares} with shares)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
