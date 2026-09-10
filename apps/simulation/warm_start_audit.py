#!/usr/bin/env python3
"""Phase 00 of warm start: audit what a killed run actually left in MongoDB.

The whole warm-start plan rests on one claim -- *a run that was killed mid-flight
leaves the database in a state coherent enough to restart from*. That claim is
plausible from reading the code (``ORSimManager.init_resource`` is get-or-create,
agents write through to Eve on every transition) but plausible is not verified,
and building four phases on an unverified premise is how a week gets lost.

This script answers the question with data instead. It reads Mongo DIRECTLY --
no API, no celery, no running simulation -- so it works on any historical run,
including the interrupted ones already sitting in the database.

Each check prints PASS / WARN / FAIL and, more importantly, a NUMBER. The
numbers are the deliverable: "10 trucks would deadlock on resume" is what sizes
phase 02, and no amount of code reading produces it.

Usage:
    python -m apps.simulation.warm_start_audit --run-id run_20260816_041004
    python -m apps.simulation.warm_start_audit --interrupted        # audit every non-success run
    python -m apps.simulation.warm_start_audit --run-id <id> --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

from pymongo import MongoClient

DB_NAME = os.environ.get("MONGODB_NAME", "OpenRoadDB")
MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017")

TRUCKS = "container_logistics_truck"
ORDERS = "container_logistics_order"
FACILITIES = "container_logistics_facility"
TRIPS = "container_logistics_haul_trip"
RUN_CONFIG = "run_config"

TERMINAL_TRIP_STATES = {"completed", "cancelled"}

# Trip states whose progress depends on the facility's IN-MEMORY gate queue.
# A truck sitting in one of these is waiting to be called forward by a facility
# agent whose queue does not survive a restart -- so on resume nothing will ever
# call it. These are the deadlock candidates, and counting them is the entire
# point of this audit.
QUEUE_BLOCKED_STATES = {"queued_for_pickup", "queued_for_dropoff"}

# Trip states where the truck is already AT a gate being served. The queue entry
# is gone too, but these also need the in-flight gate service to be re-armed.
GATE_SERVICE_STATES = {"at_pickup_gate", "at_dropoff_gate"}


class Report:
    """Collects check outcomes so the script can exit non-zero on a real failure."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.checks: list[dict] = []
        self.facts: dict = {}

    def add(self, key: str, status: str, headline: str, detail: str = "") -> None:
        self.checks.append(
            {"key": key, "status": status, "headline": headline, "detail": detail}
        )

    def fact(self, key: str, value) -> None:
        self.facts[key] = value

    @property
    def failed(self) -> bool:
        return any(c["status"] == "FAIL" for c in self.checks)

    def render(self) -> str:
        icon = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "INFO": "····"}
        lines = [f"\n{'=' * 78}", f"warm-start audit · {self.run_id}", "=" * 78]
        for c in self.checks:
            lines.append(f"  [{icon[c['status']]}]  {c['key']:<22} {c['headline']}")
            if c["detail"]:
                for d in c["detail"].splitlines():
                    lines.append(f"           {d}")
        return "\n".join(lines)


def _derive_stop_step(db, run_id: str, report: Report) -> None:
    """Can we recover WHICH STEP the run died on, from the data alone?

    This decides something bigger than it looks. If the step is derivable from
    persisted documents, then a *crashed* run (no checkpoint written) is still
    resumable -- the checkpoint document is only needed for the in-memory
    residue, not for the clock. If it is NOT derivable, crash recovery is
    impossible without periodic checkpointing, which changes the answer to the
    'pause-only vs every N steps' question.
    """
    rc = db[RUN_CONFIG].find_one({"run_id": run_id}) or {}
    settings = (rc.get("meta") or {}).get("simulation_settings") or {}
    ref_raw = settings.get("REFERENCE_TIME")
    interval = settings.get("STEP_INTERVAL")
    horizon = settings.get("SIMULATION_LENGTH_IN_STEPS")

    if not ref_raw or not interval:
        report.add(
            "clock-recovery",
            "FAIL",
            "cannot derive the stop step: run_config lacks REFERENCE_TIME or STEP_INTERVAL",
        )
        return

    reference = (
        datetime.fromisoformat(ref_raw) if isinstance(ref_raw, str) else ref_raw
    )

    latest = None
    for coll in (TRUCKS, ORDERS, TRIPS):
        doc = list(db[coll].find({"run_id": run_id}, {"sim_clock": 1}).sort("sim_clock", -1).limit(1))
        if doc and doc[0].get("sim_clock"):
            stamp = doc[0]["sim_clock"]
            latest = stamp if latest is None or stamp > latest else latest

    if latest is None:
        report.add("clock-recovery", "FAIL", "no sim_clock stamps found on any entity document")
        return

    elapsed = (latest - reference).total_seconds()
    exact_step = elapsed / float(interval)
    step = int(round(exact_step))
    integral = abs(exact_step - step) < 1e-9

    report.fact("reference_time", str(reference))
    report.fact("last_sim_clock", str(latest))
    report.fact("stop_step", step)
    report.fact("horizon_steps", horizon)

    detail = (
        f"reference   {reference}\n"
        f"last clock  {latest}\n"
        f"elapsed     {elapsed:,.0f} s  ÷ {interval} s/step  =  {exact_step:,.4f}"
    )
    if integral:
        pct = f" ({step / horizon:.1%} of {horizon})" if horizon else ""
        report.add(
            "clock-recovery",
            "PASS",
            f"stop step recovered exactly: step {step}{pct}",
            detail,
        )
    else:
        report.add(
            "clock-recovery",
            "WARN",
            f"stop step is NOT an exact multiple ({exact_step:,.4f}) — rounded to {step}",
            detail,
        )


def _check_identity(db, run_id: str, report: Report) -> None:
    """One document per (role, user), or get-or-create did not hold.

    If this fails, resume would attach agents to the wrong record -- or create
    yet another duplicate -- and nothing downstream can be trusted.
    """
    problems = []
    for coll, label in ((TRUCKS, "truck"), (FACILITIES, "facility")):
        by_user = Counter(
            d.get("user") for d in db[coll].find({"run_id": run_id}, {"user": 1})
        )
        dupes = {u: n for u, n in by_user.items() if n > 1}
        report.fact(f"{label}_docs", sum(by_user.values()))
        report.fact(f"{label}_distinct_users", len(by_user))
        if dupes:
            problems.append(f"{label}: {len(dupes)} users hold more than one document")

    if problems:
        report.add("identity", "FAIL", "duplicate entity documents found", "\n".join(problems))
    else:
        report.add(
            "identity",
            "PASS",
            f"exactly one document per user "
            f"({report.facts.get('truck_docs', 0)} trucks, "
            f"{report.facts.get('facility_docs', 0)} facilities)",
        )


def _check_inflight(db, run_id: str, report: Report) -> None:
    """Inventory the work that was in progress when the run died."""
    trips = list(
        db[TRIPS].find(
            {"run_id": run_id},
            {"state": 1, "truck": 1, "order": 1, "routes": 1, "stats": 1},
        )
    )
    inflight = [t for t in trips if t.get("state") not in TERMINAL_TRIP_STATES]
    states = Counter(t.get("state") for t in inflight)

    report.fact("trips_total", len(trips))
    report.fact("trips_inflight", len(inflight))
    report.fact("inflight_states", dict(states))

    detail = "\n".join(f"{n:>5}  {s}" for s, n in states.most_common()) or "none"
    report.add(
        "in-flight",
        "INFO",
        f"{len(inflight)} trips still open out of {len(trips)}",
        detail,
    )

    queue_blocked = sum(n for s, n in states.items() if s in QUEUE_BLOCKED_STATES)
    at_gate = sum(n for s, n in states.items() if s in GATE_SERVICE_STATES)
    report.fact("queue_blocked", queue_blocked)
    report.fact("at_gate", at_gate)

    if queue_blocked or at_gate:
        report.add(
            "queue-dependence",
            "WARN",
            f"{queue_blocked + at_gate} trips depend on facility gate state that is NOT persisted",
            f"{queue_blocked:>5}  waiting in a queue  -> would deadlock on resume\n"
            f"{at_gate:>5}  being served at a gate -> needs gate service re-armed",
        )
    else:
        report.add("queue-dependence", "PASS", "no trip depends on unpersisted gate state")

    # Route readiness: an in-flight trip can only resume driving if the leg it is
    # on still carries its planned route. Routes are planned once at assignment
    # (USE_OSRM_AT_ASSIGNMENT) and are authoritative for movement, KPI distance
    # and replay -- so a missing one is not cosmetic.
    missing = [
        t for t in inflight
        if not ((t.get("routes") or {}).get("planned"))
    ]
    report.fact("inflight_missing_routes", len(missing))
    if missing:
        report.add(
            "route-readiness",
            "FAIL",
            f"{len(missing)} of {len(inflight)} in-flight trips have no planned route to resume onto",
        )
    else:
        report.add(
            "route-readiness",
            "PASS",
            f"all {len(inflight)} in-flight trips carry their planned routes",
        )


def _check_references(db, run_id: str, report: Report) -> None:
    """Do in-flight trips point at entities that actually exist?"""
    truck_ids = {str(d["_id"]) for d in db[TRUCKS].find({"run_id": run_id}, {"_id": 1})}
    order_ids = {str(d["_id"]) for d in db[ORDERS].find({"run_id": run_id}, {"_id": 1})}

    dangling_truck = dangling_order = 0
    for t in db[TRIPS].find({"run_id": run_id}, {"state": 1, "truck": 1, "order": 1}):
        if t.get("state") in TERMINAL_TRIP_STATES:
            continue
        if str(t.get("truck")) not in truck_ids:
            dangling_truck += 1
        if str(t.get("order")) not in order_ids:
            dangling_order += 1

    report.fact("dangling_truck_refs", dangling_truck)
    report.fact("dangling_order_refs", dangling_order)

    if dangling_truck or dangling_order:
        report.add(
            "references",
            "FAIL",
            "in-flight trips reference entities that do not exist",
            f"{dangling_truck} dangling truck refs, {dangling_order} dangling order refs",
        )
    else:
        report.add("references", "PASS", "every in-flight trip resolves to a real truck and order")


def _check_agreement(db, run_id: str, report: Report) -> None:
    """The two halves of the world should tell the same story.

    The invariant is ONE-DIRECTIONAL, and getting the direction wrong turns a
    healthy run into a false alarm. ``TruckManager.is_assignable`` reads
    ``state == online and active_trip is None`` -- so an online truck with no
    trip is not a defect, it is a truck between hauls waiting for its next
    assignment. Measured across the interrupted runs, that gap is 2-16 trucks
    and is entirely legitimate.

    What must NEVER be true is the converse: a truck holding an open trip while
    its own document says it is offline. That is genuine split-brain, and on
    resume it would surface as a truck that owns work it will never do.
    """
    truck_states = Counter(
        d.get("state") for d in db[TRUCKS].find({"run_id": run_id}, {"state": 1})
    )
    state_by_truck = {
        str(d["_id"]): d.get("state")
        for d in db[TRUCKS].find({"run_id": run_id}, {"state": 1})
    }

    inflight_trucks = set()
    contradictions = Counter()
    for t in db[TRIPS].find({"run_id": run_id}, {"state": 1, "truck": 1}):
        if t.get("state") in TERMINAL_TRIP_STATES:
            continue
        tid = str(t.get("truck"))
        inflight_trucks.add(tid)
        owner_state = state_by_truck.get(tid)
        if owner_state != "online":
            contradictions[owner_state] += 1

    online = truck_states.get("online", 0)
    idle_online = online - len(inflight_trucks)

    report.fact("truck_states", dict(truck_states))
    report.fact("trucks_with_open_trip", len(inflight_trucks))
    report.fact("trucks_online_without_trip", idle_online)
    report.fact("open_trip_offline_owner", dict(contradictions))

    detail = (
        f"{online:>5}  trucks online\n"
        f"{len(inflight_trucks):>5}  of those hold an open trip\n"
        f"{idle_online:>5}  online between hauls (legitimate — is_assignable requires no active trip)"
    )
    if contradictions:
        report.add(
            "agreement",
            "FAIL",
            f"{sum(contradictions.values())} open trips are owned by a truck that is not online",
            detail + f"\nowner states seen: {dict(contradictions)}",
        )
    else:
        report.add(
            "agreement",
            "PASS",
            f"every one of the {len(inflight_trucks)} open trips is owned by an online truck",
            detail,
        )


def _check_order_states(db, run_id: str, report: Report) -> None:
    states = Counter(d.get("state") for d in db[ORDERS].find({"run_id": run_id}, {"state": 1}))
    report.fact("order_states", dict(states))
    non_terminal = sum(n for s, n in states.items() if s not in ("completed", "cancelled"))
    report.add(
        "orders",
        "INFO",
        f"{non_terminal} orders still live out of {sum(states.values())}",
        "\n".join(f"{n:>5}  {s}" for s, n in states.most_common()),
    )


def audit(db, run_id: str) -> Report:
    report = Report(run_id)
    _derive_stop_step(db, run_id, report)
    _check_identity(db, run_id, report)
    _check_agreement(db, run_id, report)
    _check_inflight(db, run_id, report)
    _check_references(db, run_id, report)
    _check_order_states(db, run_id, report)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", help="run to audit")
    ap.add_argument("--interrupted", action="store_true", help="audit every non-success run")
    ap.add_argument("--json", action="store_true", help="emit machine-readable facts")
    ap.add_argument("--mongo-uri", default=MONGO_URI)
    args = ap.parse_args()

    db = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=5000)[DB_NAME]

    if args.interrupted:
        run_ids = [
            d["run_id"]
            for d in db[RUN_CONFIG].find({"status": {"$ne": "success"}}, {"run_id": 1}).sort("run_id", -1)
            if d.get("run_id")
        ]
    elif args.run_id:
        run_ids = [args.run_id]
    else:
        ap.error("pass --run-id or --interrupted")

    reports = [audit(db, r) for r in run_ids]

    if args.json:
        print(json.dumps(
            [{"run_id": r.run_id, "facts": r.facts, "checks": r.checks} for r in reports],
            indent=2, default=str,
        ))
    else:
        for r in reports:
            print(r.render())
        print()

    return 1 if any(r.failed for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
