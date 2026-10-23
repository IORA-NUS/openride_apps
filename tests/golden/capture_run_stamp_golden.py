"""Capture the RUN-STAMP golden: the sim main's stamp, run settings and run name per golden case.

Responsibility: final review round 2, G3. The dispatch goldens pin what a tick DOES; the
stamp (``run_config.meta.policies``), the run settings a solver's ``stamp_settings``
add, and the run-name segments were only ever compared between a run and its RENAMED
copy (the rename litmus), so a branch keyed on the class or the MRO (the reviewer's B1:
``world_reads`` tampered in ``policies_stamp``; B4: ``planner.timing`` -> ``"plan"``;
B2/B3/C2: an extra run-name segment) changes both sides alike and survived the full
suite. This fixture STORES them, captured on unmodified code, for every case of
``dispatch_golden_v1`` (68, the legacy-profile partitioned / pooled tops) and of
``central_top_v1`` (34, every built-in leaf as a central top).

How a case runs: :func:`drive_main` calls the sim main's own ``main()``
(``apps/simulation/run_container_logistics_simulation.py``) with the case's profile as the
only assignment behavior, up to the ``RUNNING`` ``push_run_status`` (where the run name
and the settings are final), so the stamp comes from ``_stamp_effective_policies`` ->
``policies_stamp``, the declaration from ``_apply_run_declaration``, and the run name
from ``main()``'s own builder -- nothing here reimplements them. Only the I/O edges are
replaced (the scenario manager, Kafka, the dependency preflight, the run id), and the
environment is pinned: every ``ORSIM_*`` variable removed, then ``ORSIM_RUN_NAME=base``
and ``ORSIM_ORDER_LIFECYCLE=agents`` (the service mode provisions a user over HTTP; its
only run-name effect is the absence of the ``lifecycle:agents`` segment).

What is stored per case: ``exit`` (``None``, or the exit code and the message tail),
``run_name`` and its ``segments``, ``stamp`` (the ``POLICIES`` setting, in full),
``stamp_settings`` (every run setting except ``POLICIES`` and the host-runtime knobs of
``runtime_settings.apply_long_run_orsim_settings``), ``declared_stamp_settings`` and
``declared_run_name_segments`` (the bound tree's declaration, as ``main()`` bound it).
The profiles are NOT copied: they are read from the two frozen golden files.

Run from the repo root on an UNMODIFIED tree (it refuses otherwise)::

    venv/bin/python -m tests.golden.capture_run_stamp_golden

Captured at ``6647aaf`` (feat/policy-slots after the round-2 fix merges), whose stamps and
run names are byte-identical to ``44922b4`` (the round-2 review's stamp / run-name
diff). This module is shared by the capture and the replay
(``tests/test_solver_contract_run_stamp.py``), so a difference between them can only
come from production code.
"""

from __future__ import annotations

import contextlib
import copy
import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.golden.capture_central_top_golden import CENTRAL_PATH, load_central, load_v1  # noqa: E402
from tests.golden.capture_dispatch_golden import FIXTURE_PATH as V1_PATH  # noqa: E402

STAMP_PATH = Path(__file__).resolve().parent / "run_stamp_v1.json.gz"
FORMAT_VERSION = 1
GENERATOR_COMMAND = "python -m tests.golden.capture_run_stamp_golden"
RUN_NAME = "base"
SEGMENT_SEPARATOR = " · "
#: The pinned environment of every case (every other ``ORSIM_*`` variable is removed).
CASE_ENV = {"ORSIM_RUN_NAME": RUN_NAME, "ORSIM_ORDER_LIFECYCLE": "agents"}

__all__ = ["STAMP_PATH", "drive_main", "golden_cases", "load_stamp_golden", "build_stamp_fixture"]


class _ReachedRunning(Exception):
    """Raised by the stubbed ``push_run_status``: the run name and the settings are final."""


def load_stamp_golden() -> dict[str, Any]:
    with gzip.open(STAMP_PATH, "rb") as fh:
        return json.loads(fh.read())


def golden_cases() -> list[dict[str, Any]]:
    """``[{case_id, source, profile}]``: every ``dispatch_golden_v1`` case, then every central-top case."""
    out = [{"case_id": c["case_id"], "source": V1_PATH.name, "profile": c["profile"]} for c in load_v1()["cases"]]
    out += [{"case_id": c["case_id"], "source": CENTRAL_PATH.name, "profile": c["profile"]}
            for c in load_central()["cases"]]
    return out


@contextlib.contextmanager
def _pinned_environment() -> Iterator[None]:
    saved = {k: v for k, v in os.environ.items() if k.startswith("ORSIM_")}
    for key in saved:
        del os.environ[key]
    os.environ.update(CASE_ENV)
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith("ORSIM_")]:
            del os.environ[key]
        os.environ.update(saved)


@contextlib.contextmanager
def _patched(obj: Any, **attrs: Any) -> Iterator[None]:
    saved = {name: getattr(obj, name) for name in attrs}
    for name, value in attrs.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(obj, name, value)


def drive_main(profile: dict[str, Any]) -> dict[str, Any]:
    """Run the sim main's ``main()`` on ``profile`` up to ``RUNNING``; what it stamped and named (JSON-ready)."""
    import apps.simulation.run_container_logistics_simulation as sim_main
    from apps.container_logistics.scenario.runtime_settings import apply_long_run_orsim_settings
    from apps.utils import kafka_utils

    collections = {"assignment": {"assignment_main": {"profile": copy.deepcopy(profile)}},
                   "truck": {}, "order": {}, "facility": {}}
    held: dict[str, Any] = {}

    class _ScenarioManager:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self.orsim_settings: dict[str, Any] = {}
            self.reference_time = None
            held["sm"] = self

        def get_agent_collection(self, role: str) -> dict[str, Any]:
            return collections[role]

        def get_scenario_display_name(self) -> str:
            return "run-stamp golden"

        def get_run_config_meta(self) -> dict[str, Any]:
            return {}

    real_stamp = sim_main._stamp_effective_policies

    def stamp_and_hold(scenario_manager: Any, overridden: bool) -> Any:
        tree = real_stamp(scenario_manager, overridden)   # the sim main's own function, unchanged
        held["tree"] = tree
        return tree

    def push_run_status(_topic: Any, _run_id: Any, status: Any, **kw: Any) -> None:
        held["status"], held["run_name"] = status, kw.get("run_name")
        raise _ReachedRunning

    out = io.StringIO()
    exit_info = None
    with tempfile.TemporaryDirectory() as tmp, _pinned_environment(), contextlib.redirect_stdout(out), \
            _patched(sim_main, ScenarioManager=_ScenarioManager, get_datahub_dir=lambda: tmp,
                     _resolve_run_id=lambda: "run_stamp_golden",
                     _ensure_transport_lane_topics=lambda run_id: None,
                     assert_simulation_dependencies=lambda *a, **k: None,
                     _stamp_effective_policies=stamp_and_hold), \
            _patched(kafka_utils, bootstrap_kafka_for_run=lambda run_id: "run_status",
                     push_run_status=push_run_status, flush_producer=lambda *a, **k: None):
        try:
            sim_main.main()
            raise AssertionError("main() returned without pushing RUNNING")
        except _ReachedRunning:
            pass
        except SystemExit as exc:
            exit_info = [exc.code, out.getvalue()[-400:]]
    if exit_info is not None:
        return json.loads(json.dumps({"exit": exit_info}, default=str))
    settings = dict(held["sm"].orsim_settings)
    host_knobs = set(apply_long_run_orsim_settings({}))
    tree = held.get("tree")
    declaration = getattr(tree, "declaration", None)
    run_name = held["run_name"]
    return json.loads(json.dumps({
        "exit": None,
        "status": held["status"],
        "run_name": run_name,
        "segments": run_name.split(SEGMENT_SEPARATOR) if run_name else [],
        "stamp": settings.get("POLICIES"),
        "stamp_settings": {k: v for k, v in sorted(settings.items()) if k != "POLICIES" and k not in host_knobs},
        "declared_stamp_settings": None if declaration is None else dict(declaration.stamp_settings),
        "declared_run_name_segments": None if declaration is None else list(declaration.run_name_segments),
    }, default=str))


def build_stamp_fixture() -> dict[str, Any]:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()
    cases = [{"case_id": c["case_id"], "source": c["source"], "expected": drive_main(c["profile"])}
             for c in golden_cases()]
    return {"format": FORMAT_VERSION,
            "header": {"commit": head, "generator": GENERATOR_COMMAND, "env": CASE_ENV,
                       "profiles_from": [V1_PATH.name, CENTRAL_PATH.name]},
            "cases": cases}


def encode(doc: dict[str, Any]) -> bytes:
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
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
    doc = build_stamp_fixture()
    STAMP_PATH.write_bytes(encode(doc))
    exits = sum(1 for c in doc["cases"] if c["expected"]["exit"] is not None)
    print(f"wrote {STAMP_PATH} ({len(doc['cases'])} cases, {exits} exits)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
