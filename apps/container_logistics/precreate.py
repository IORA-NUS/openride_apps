"""Bulk pre-create truck + facility REST documents before agents boot.

Each agent's manager runs ``init_resource``: an owner-scoped GET, then a POST when the
GET is empty. At fleet scale (~5000 trucks) those ~5000 simultaneous boot POSTs are the
"create stampede" — they saturate the API, drop connections, and lose facilities plus
thousands of trucks (CLAUDE.md §5 / assignment-scale work, and the boot-stampede
investigation). This module collapses that stampede into a few bulk ``insert_many`` calls:
every agent's document is written up-front, stamped with

  * ``run_id``           — taken from the resource URL by the agent's GET,
  * ``user``             — Eve's AUTH_FIELD; set to ``str(owner_user_id)`` so the agent's
                           owner-scoped GET (it authenticates as ``{agent_id}@test.com``)
                           returns exactly this document and *adopts* it (no POST),
  * ``statemachine.id``  — bound from the ``statemachine`` collection by ``{name, domain}``,
                           exactly as ``TruckView/FacilityView.on_insert`` would, so the
                           first login transition validates,
  * a faithful ``_etag`` — Eve validates the first login PATCH's ``If-Match`` against the
                           *stored* ``_etag`` (``document.get(ETAG, ...)`` in
                           ``eve/methods/common.py``) and returns that same stored value on
                           GET, so the agent's read→PATCH round-trip is self-consistent.
                           (Proven end-to-end against a live API: GET adopts, login PATCH
                           dormant→offline returns 200, no 412/428.)

It runs in the ``SimulationRuntime`` subprocess (so it picks up code edits without a
Celery restart), inside ``on_before_run`` — after the run-config and state machines exist
and *before* any agent spawns — so there is no GET/insert race. It is idempotent: any
``(run_id, user)`` that already has a document is skipped, so a re-invocation (or an agent
that somehow created first) never double-inserts.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from bson import ObjectId
from bson.json_util import dumps as _bson_dumps
from pymongo import MongoClient

from apps.common.resource_client_mixin import get_http_session
from apps.config import kpi_sink_settings, settings, simulation_domains
from apps.container_logistics.facility.service_time import resolve_service_time

logger = logging.getLogger(__name__)

DOMAIN = simulation_domains.get("container_logistics", "container-logistics-sim")

_TRUCK_COLLECTION = "container_logistics_truck"
_FACILITY_COLLECTION = "container_logistics_facility"
_USER_COLLECTION = "user"
_STATEMACHINE_COLLECTION = "statemachine"

# Each agent's manager creates its document in its state-machine's *initial* state; the
# agent then logs in (dormant->offline->online for trucks). Pre-created docs MUST match
# that initial state so the first login transition is valid.
_TRUCK_SM = "WorkflowStateMachine"
_TRUCK_STATE = "dormant"
_FACILITY_SM = "GateStateMachine"
_FACILITY_STATE = "closed"

# Order-lifecycle ``service`` mode only (see docs/order_lifecycle_service_plan.md §3.3):
# orders become pure data, bulk-created up front in ``created`` = "future demand, not yet
# requested". The lifecycle service publishes them onto the market (created -> unassigned)
# when the step reaches ``meta.request_time_step``. In ``agents`` mode nothing below runs.
_ORDER_COLLECTION = "container_logistics_order"
_ORDER_SM = "OrderStateMachine"
_ORDER_STATE = "created"
# 30k orders is a single bounded insert, but chunking keeps the BSON batch well inside
# Mongo's 48 MB write-command limit for very large demand scenarios.
_ORDER_INSERT_CHUNK = 10000


def _mongo_client() -> MongoClient:
    uri = kpi_sink_settings.get("mongo_uri")
    if uri:
        return MongoClient(uri)
    return MongoClient(kpi_sink_settings["mongo_host"], int(kpi_sink_settings["mongo_port"]))


def _eve_etag(doc: dict) -> str:
    """sha1 over ``bson.json_util.dumps(doc, sort_keys=True)`` — mirrors ``eve.utils.document_etag``.

    Computed on the document *without* the ``_etag`` key (Eve computes the etag before
    storing it). A divergent value would still be safe — Eve reads the stored ``_etag`` for
    both GET and the If-Match check — but matching keeps any recompute path consistent too.
    """
    return hashlib.sha1(_bson_dumps(doc, sort_keys=True).encode("utf-8")).hexdigest()


def _statemachine_ids(db, names=(_TRUCK_SM, _FACILITY_SM)) -> dict[str, ObjectId]:
    ids: dict[str, ObjectId] = {}
    for name in names:
        sm = db[_STATEMACHINE_COLLECTION].find_one({"name": name, "domain": DOMAIN}, {"_id": 1})
        if not sm:
            raise RuntimeError(
                f"statemachine {name}/{DOMAIN} not registered; cannot pre-create agent docs"
            )
        ids[name] = sm["_id"]
    return ids


def _resolve_user_ids(db, emails: list[str], roles: dict[str, str] | None = None) -> dict[str, str]:
    """Map ``email -> str(user _id)``, signing up any missing users.

    Users are persistent and deterministic (``{agent_id}@test.com``), so for any fleet size
    previously run they already exist and this is a single ``$in`` query. The signup branch
    is the rare cold-start path (a brand-new email set); it uses the canonical ``/auth/signup``
    so the password hash matches what the agents' later login verifies against.

    ``roles`` overrides the signup role for specific emails (default ``client``). Truck and
    facility agents are genuinely ``client`` — they are owner-scoped. The order-lifecycle
    owner is NOT: it must be ``admin`` or the agent inherits a client JWT and every admin-only
    aggregation read (notably the sweep's open-haul guard) silently 401s. Signup only ever
    happens for a *missing* user, so an existing admin is never downgraded.
    """
    roles = roles or {}
    found = {
        u["email"]: str(u["_id"])
        for u in db[_USER_COLLECTION].find({"email": {"$in": emails}}, {"email": 1})
    }
    missing = [e for e in emails if e not in found]
    if missing:
        logger.info("precreate: signing up %d missing users (cold-start)", len(missing))
        session = get_http_session()
        signup_url = f"{settings['OPENRIDE_SERVER_URL']}/auth/signup"
        headers = {"Content-Type": "application/json"}
        for email in missing:
            session.post(
                signup_url,
                headers=headers,
                data=json.dumps(
                    {
                        "email": email,
                        "password": "password",
                        "name": {"first_name": "Dummy", "last_name": "Dummy"},
                        "public_key": "000",
                        "role": roles.get(email, "client"),
                    }
                ),
            )
        for u in db[_USER_COLLECTION].find({"email": {"$in": missing}}, {"email": 1}):
            found[u["email"]] = str(u["_id"])
    return found


def _stamp_meta(doc: dict, sim_clock: datetime) -> dict:
    """Add the Eve-managed fields the API would have set on insert, then the etag.

    ``patch_timestamps`` (api/utils) sets ``_created == _updated == sim_clock`` on insert, so
    we replicate that. ``_etag`` is computed last, over the fully-assembled document.
    """
    doc["_id"] = ObjectId()
    doc["sim_clock"] = sim_clock
    doc["_created"] = sim_clock
    doc["_updated"] = sim_clock
    doc["_etag"] = _eve_etag(doc)
    return doc


def _truck_doc(run_id: str, behavior: dict, user_id: str, sm_id: ObjectId, sim_clock: datetime) -> dict:
    profile = dict(behavior.get("profile") or {})
    # Anchor the truck at its boot position on the REST document. The behavior carries
    # the home/initial location as a *top-level* ``init_loc`` (the truck agent reads it as
    # its starting ``current_loc``), but only the ``profile`` is written to the document the
    # assignment service reads. Without a position there, ``assignment.constraints.truck_anchor_loc``
    # finds nothing (current_loc is only set after the first haul, init_loc/last_known_loc are
    # never on the profile, and the home-facility fallback only knows the hardcoded
    # ``terminal_*`` set — not the real generated facilities trucks are homed at). The spatial
    # matcher then drops every fresh truck into its ``_unplaced`` bucket and never reaches them
    # once any trucks have hauled, so throughput collapses after day 1 (see CLAUDE.md §6.1).
    # Seeding ``current_loc`` here places every truck in the spatial grid from t=0; the same
    # key is maintained by TruckManager.set_last_dropoff after each haul, so it stays coherent.
    init_loc = behavior.get("init_loc")
    if init_loc and not profile.get("current_loc"):
        profile["current_loc"] = init_loc
        profile.setdefault("init_loc", init_loc)
    doc = {
        "run_id": run_id,
        "user": user_id,
        "profile": profile,
        "persona": {"role": "truck", **(behavior.get("persona") or {})},
        "statemachine": {"name": _TRUCK_SM, "domain": DOMAIN, "id": sm_id},
        "state": _TRUCK_STATE,
    }
    return _stamp_meta(doc, sim_clock)


def _facility_doc(run_id: str, behavior: dict, user_id: str, sm_id: ObjectId, sim_clock: datetime) -> dict:
    profile = behavior.get("profile", {})
    service_time = resolve_service_time(profile, profile)
    doc = {
        "run_id": run_id,
        "user": user_id,
        "profile": profile,
        "persona": {"role": "facility", **(behavior.get("persona") or {})},
        "num_gates": profile.get("gate_count", 1),
        "service_time": service_time,
        # Legacy leg-specific keys mirror FacilityManager (same value pre-unified queue model).
        "pickup_service_time": service_time,
        "dropoff_service_time": service_time,
        "statemachine": {"name": _FACILITY_SM, "domain": DOMAIN, "id": sm_id},
        "state": _FACILITY_STATE,
        # readonly default the Eve schema applies on insert.
        "feasible_transitions": [],
    }
    return _stamp_meta(doc, sim_clock)


def _order_doc(run_id: str, behavior: dict, user_id: str, sm_id: ObjectId, sim_clock: datetime) -> dict:
    """One precreated order row (order-lifecycle ``service`` mode).

    ``profile`` is copied verbatim from the behavior — it is exactly what
    ``order/unassigned_batch`` projects for the matcher (pickup/dropoff loc + facility names,
    service times, order size, haulier_id). ``meta.request_time_step`` moves the demand-curve
    timing onto the document so the lifecycle service can publish on schedule without ever
    seeing a behavior dict. No top-level ``pickup_loc``/``dropoff_loc``: the order agent never
    wrote those either (they live under ``profile``), and inventing them would diverge from
    the ``agents``-mode document shape.
    """
    doc = {
        "run_id": run_id,
        "user": user_id,
        "profile": dict(behavior.get("profile") or {}),
        "persona": {"role": "order", **(behavior.get("persona") or {})},
        "statemachine": {"name": _ORDER_SM, "domain": DOMAIN, "id": sm_id},
        "state": _ORDER_STATE,
        # readonly default the Eve schema applies on insert.
        "feasible_transitions": [],
        "meta": {"request_time_step": int(behavior.get("request_time_step", 0) or 0)},
    }
    return _stamp_meta(doc, sim_clock)


def precreate_agent_docs(
    run_id: str,
    truck_behaviors: dict,
    facility_behaviors: dict,
    sim_clock: datetime,
    order_behaviors: dict | None = None,
    order_owner_email: str | None = None,
) -> dict:
    """Insert truck + facility documents for ``run_id`` before its agents boot.

    Idempotent per ``(run_id, user)``. Returns a stats dict. Raising is fine for the caller
    to catch — a pre-create failure must not abort an otherwise-runnable simulation (agents
    would fall back to creating their own docs via the old path).

    ``order_behaviors`` is supplied **only** in order-lifecycle ``service`` mode: orders have
    no agent to create their own document, so the whole population is inserted here in
    ``created``. Every order doc is owned by ONE user (``order_owner_email``, the lifecycle
    service's account) rather than one user per order — the service is admin anyway, so
    ownership is bookkeeping, not access. Falsy ``order_behaviors`` leaves this function
    behaving exactly as before (the order stats simply read zero).
    """
    if not isinstance(sim_clock, datetime):
        raise TypeError(f"sim_clock must be a datetime, got {type(sim_clock)}")

    client = _mongo_client()
    stats = {
        "trucks_inserted": 0,
        "facilities_inserted": 0,
        "trucks_skipped": 0,
        "facilities_skipped": 0,
        "orders_inserted": 0,
        "orders_skipped": 0,
        "users_resolved": 0,
    }
    try:
        db = client[kpi_sink_settings["mongo_db"]]
        sm_names = (_TRUCK_SM, _FACILITY_SM) + ((_ORDER_SM,) if order_behaviors else ())
        sm_ids = _statemachine_ids(db, sm_names)

        order_owner_email = (order_owner_email or "").strip() if order_behaviors else None
        if order_behaviors and not order_owner_email:
            order_owner_email = "order_lifecycle_main@test.com"

        emails = sorted(
            {b.get("email") for b in truck_behaviors.values() if b.get("email")}
            | {b.get("email") for b in facility_behaviors.values() if b.get("email")}
            | ({order_owner_email} if order_owner_email else set())
        )
        # The lifecycle owner is normally already provisioned as admin by the run entrypoint
        # (order_lifecycle/users.py) before any agent boots; this role map is the belt to that
        # braces, so precreate can never be the one that creates it as a client.
        email_to_uid = _resolve_user_ids(
            db,
            emails,
            roles={order_owner_email: "admin"} if order_owner_email else None,
        )
        stats["users_resolved"] = len(email_to_uid)

        for coll_name, behaviors, build, sm_key, ins_key, skip_key in (
            (_TRUCK_COLLECTION, truck_behaviors, _truck_doc, _TRUCK_SM, "trucks_inserted", "trucks_skipped"),
            (_FACILITY_COLLECTION, facility_behaviors, _facility_doc, _FACILITY_SM, "facilities_inserted", "facilities_skipped"),
        ):
            coll = db[coll_name]
            existing_users = set(coll.distinct("user", {"run_id": run_id}))
            docs = []
            for behavior in behaviors.values():
                uid = email_to_uid.get(behavior.get("email"))
                if not uid:
                    logger.warning("precreate: no user id for %s; skipping", behavior.get("email"))
                    continue
                if uid in existing_users:
                    stats[skip_key] += 1
                    continue
                docs.append(build(run_id, behavior, uid, sm_ids[sm_key], sim_clock))
            if docs:
                coll.insert_many(docs, ordered=False)
                stats[ins_key] += len(docs)

        if order_behaviors:
            owner_uid = email_to_uid.get(order_owner_email)
            if not owner_uid:
                logger.warning(
                    "precreate: no user id for order owner %s; skipping order pre-create",
                    order_owner_email,
                )
            else:
                coll = db[_ORDER_COLLECTION]
                if owner_uid in set(coll.distinct("user", {"run_id": run_id})):
                    # Same (run_id, user) idempotency contract as trucks/facilities.
                    stats["orders_skipped"] += len(order_behaviors)
                else:
                    docs = [
                        _order_doc(run_id, behavior, owner_uid, sm_ids[_ORDER_SM], sim_clock)
                        for behavior in order_behaviors.values()
                    ]
                    for start in range(0, len(docs), _ORDER_INSERT_CHUNK):
                        chunk = docs[start:start + _ORDER_INSERT_CHUNK]
                        coll.insert_many(chunk, ordered=False)
                        stats["orders_inserted"] += len(chunk)

        logger.info("precreate run_id=%s: %s", run_id, stats)
        return stats
    finally:
        client.close()
