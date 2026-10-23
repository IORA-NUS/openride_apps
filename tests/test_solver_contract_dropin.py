"""Litmus (b): a solver file DROPPED into the library is discovered and served, with zero framework edits.

Plan ``plan_contract_final.md`` §7.12 (WS8), litmus (b); closes G11 (discovery scans
the built-in packages, so a new file in ``policies/dispatch/library/`` is all a new
built-in needs).

The test copies the CHECKED-OUT working tree into ``tmp_path`` -- uncommitted edits
included, which is why it is ``git ls-files --cached --others --exclude-standard`` and
not ``git archive HEAD`` (that would test the last commit, plan L2). ``--others``
brings in new, not-yet-committed files too; the ``venv`` symlink (untracked, not
ignored) is filtered out explicitly. It then writes the litmus toy leaf as
``apps/container_logistics/policies/dispatch/library/litmus_toy.py`` IN THE COPY and runs,
in a subprocess with ``cwd`` = the copy (and ``PYTHONPATH`` = the copy, so ``apps.``
resolves to the copy and never to this checkout or the main tree):

- ``python -m openride policy validate planner LitmusToy`` -> exit 0, ``PASS``;
- ``python -m openride policy validate planner Partitioned`` -> exit 0, and its
  output carries the derived arm ``inner=LitmusToy``: the battery derives one arm per
  ref target (``battery.policy_arms`` / ``_ref_arm_targets``), so the new leaf is
  exercised as the inner of a built-in composite without anyone listing it.

- an in-subprocess ``AssignmentApp.assign`` / ``publish`` through the frozen golden
  harness's stubs (``tests/golden/harness.py::build_app``, in the copy) on the stored
  v1 tick state ``B`` (500 trucks x 120 orders; final review M10: litmus (b) used to stop at the battery):
  ``LitmusToy`` as a CENTRAL top, and as the ``inner`` of ``Partitioned`` on the
  ``chain`` arm. Every committed pair is published once, in order, naming its truck and
  order; the shared pairs (only under ``Partitioned``, the toy declares no partner)
  carry the toy's ``solver_notes``.

Nothing outside the copy is written. ~10 s per validate. The suite registers no
``slow`` marker, so the test is unmarked (plan §7.12).

Verified to have teeth (WS8): writing the toy file under a NON-discovered directory
of the copy (``policies/dispatch/litmus_toy.py``, one level up) failed the first
validate (unknown policy 'LitmusToy', exit 2) -- the PASS really comes from discovery.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIBRARY = Path("apps/container_logistics/policies/dispatch/library")

#: The litmus toy leaf (the leaf of litmus (a), as a real library module): the nearest
#: permitted free truck per order, deterministic (orders by id, ties by truck id), no RNG.
TOY_SOURCE = '''\
"""``LitmusToy``: a dropped-in planner leaf (litmus (b), tests/test_solver_contract_dropin.py)."""

from __future__ import annotations

from dataclasses import dataclass

from apps.container_logistics.policies.dispatch.bind import Declaration
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyParams, policy


@policy(slot="planner", name="LitmusToy", version="1",
        description="Litmus leaf: the nearest permitted free truck per order.")
class LitmusToy:
    @dataclass(frozen=True)
    class Params(PolicyParams):
        pass

    def __init__(self, params=None):
        self.params = params

    def bind(self, params, world, *, top):
        return Declaration()

    def solve_tick(self, tick, params):
        used = set()
        pairs = []
        for o in sorted(tick.raw_orders, key=lambda d: str(d.get("_id"))):
            options = [t for t in tick.raw_trucks if id(t) not in used and tick.permitted(t, o)]
            if options:
                best = min(options, key=lambda t: (tick.measure.deadhead_km(t, o), str(t.get("_id"))))
                used.add(id(best))
                pairs.append((best, o))
        return Result.from_pairs(pairs, solver_notes={(str(t["_id"]), str(o["_id"])): {"litmus_reason": "nearest"}
                                                      for t, o in pairs})
'''


#: Run in the copy: the dropped-in toy through the real assign/publish (harness stubs).
ASSIGN_SCRIPT = r'''
import copy, json, random
from apps.container_logistics.assignment.app import AssignmentApp
from tests.golden.capture_central_top_golden import load_v1
from tests.golden.harness import MODULE_SEED, TIME_STEP, build_app

v1 = load_v1()
state = v1["tick_states"]["B"]
base = next(c for c in v1["cases"] if c["case_id"] == "B-greedy-partitioned-chain-spatial-cap500")["profile"]
out = {}
for label, top in (("central", {"type": "LitmusToy"}),
                   ("inner", {"type": "Partitioned", "params": {"inner": {"type": "LitmusToy"}}})):
    prof = copy.deepcopy(base)
    planner = prof.pop("planner")
    prof.pop("use_spatial_matching", None)
    prof["policies"] = {"planner": top}
    prof["policies_format"] = 2
    prof["planner"] = {k: v for k, v in planner.items() if k not in ("topology", "market", "deployment")}
    app = build_app(prof, "GreedyNearest", state)
    app._solver = AssignmentApp._build_dispatch_policy(app.behavior["profile"])
    random.seed(MODULE_SEED)
    pairs = app.assign(state["sim_clock"], TIME_STEP)
    app.publish(pairs)
    out[label] = {"pairs": [[str(t["_id"]), str(o["_id"])] for t, o in pairs],
                  "tags": sorted([list(k), v] for k, v in app._share_tags.items()),
                  "published": [list(p) for p in app.messenger.published]}
print("ASSIGN-JSON " + json.dumps(out))
'''


def _copy_working_tree(dest: Path) -> None:
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            cwd=REPO, check=True, capture_output=True).stdout.split(b"\0")
    files = [f for f in listed if f and f != b"venv" and not f.startswith(b"venv/")
             and (REPO / os.fsdecode(f)).is_file()]
    subprocess.run(["xargs", "-0", "cp", "--parents", "-t", str(dest)], cwd=REPO, check=True,
                   input=b"\0".join(files))


def _run(copy: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(copy), "COLUMNS": "200", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, *args], cwd=copy, env=env, capture_output=True, text=True, timeout=600)


def test_a_dropped_in_library_file_is_discovered_validated_and_derived_as_an_arm(tmp_path):
    copy = tmp_path / "tree"
    copy.mkdir()
    _copy_working_tree(copy)
    assert not (copy / LIBRARY / "litmus_toy.py").exists()
    (copy / LIBRARY / "litmus_toy.py").write_text(TOY_SOURCE)

    probe = _run(copy, "-c", "import apps, openride; print(apps.__file__); print(openride.__file__)")
    assert probe.returncode == 0, probe.stderr
    assert all(Path(p).resolve().is_relative_to(copy.resolve()) for p in probe.stdout.split()), probe.stdout

    toy = _run(copy, "-m", "openride", "policy", "validate", "planner", "LitmusToy")
    assert toy.returncode == 0, toy.stdout + toy.stderr
    assert toy.stdout.strip().splitlines()[-1].startswith("PASS"), toy.stdout

    part = _run(copy, "-m", "openride", "policy", "validate", "planner", "Partitioned")
    assert part.returncode == 0, part.stdout + part.stderr
    assert part.stdout.strip().splitlines()[-1].startswith("PASS"), part.stdout
    assert "inner=LitmusToy" in part.stdout, part.stdout

    run = _run(copy, "-c", ASSIGN_SCRIPT)
    assert run.returncode == 0, run.stdout + run.stderr
    line = next(ln for ln in run.stdout.splitlines() if ln.startswith("ASSIGN-JSON "))
    out = json.loads(line[len("ASSIGN-JSON "):])
    for label in ("central", "inner"):
        got = out[label]
        assert got["pairs"], f"{label}: the toy committed nothing (the check would be vacuous)"
        assert [t for t, _ in got["published"]] == [f"run_golden/{tid}" for tid, _ in got["pairs"]], (label, got)
        tags = {tuple(k): v for k, v in got["tags"]}
        for (tid, oid), (_topic, payload) in zip(got["pairs"], got["published"]):
            doc = json.loads(payload)
            assert (doc["truck_id"], doc["order"]["_id"]) == (tid, oid), (label, doc)
            assert doc["shared"] is ((tid, oid) in tags), (label, doc)
            if doc["shared"]:
                assert doc["solver_notes"] == {"litmus_reason": "nearest"}, (label, doc)
            else:
                assert "solver_notes" not in doc, (label, doc)
    assert not out["central"]["tags"], "a top that declares no partner commits no share"
    assert out["inner"]["tags"], "the chain arm under Partitioned must share (else item 1 is vacuous)"
