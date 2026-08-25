from __future__ import annotations

import hashlib
import json
import logging
import random
from typing import Any, Dict, List, Tuple

# The greedy solver builds every feasible (truck, order) pair — O(trucks x orders).
# Past ~500 trucks/haulier that quadratic dominates and a single assign() call can
# no longer finish inside the scheduler step barrier, so nothing gets assigned at
# all (observed: a 5000-truck fleet produced 0 haul trips while 500 produced ~464).
# We bound the trucks fed to the solver *per haulier bucket* — a randomly sampled
# subset each tick, so every truck rotates into consideration across ticks without
# the matching cost growing with fleet size. Tunable via the assignment behavior
# profile (``max_trucks_per_haulier``).
#
# Default 500 keeps the worst case (one haulier holding the whole order batch) at
# 500 trucks x 500 orders = 250K pairs — the same envelope as the proven-fast
# 500-truck runs (which produced ~464 haul trips). Above the cap, sampling means a
# 5000-truck fleet costs the same per tick as a 500-truck one; trucks just rotate
# in over successive assignment ticks. Raise it to widen the candidate pool (better
# match quality) at the cost of per-tick solve time.
_DEFAULT_MAX_TRUCKS_PER_HAULIER = 500

# How often the pooled market's round provenance is pushed onto run_config.
# Cheap (two HTTP calls, a few dozen times per run) and bounded: the recorded
# stats can lag the true tick count by at most this many ticks, but a truncation
# event is pushed immediately, so `ticks_truncated_by_backstop` is never stale.
_ROUND_STATS_PATCH_EVERY = 5

from apps.common.user_registry import UserRegistry
from apps.container_logistics.message_data_models import AssignedHaulTripPayload
from apps.container_logistics.rebate import RebateBook
from apps.container_logistics.statemachine import ContainerLogisticsActions
from .solver import get_solver
from orsim.lifecycle import ORSimApp

from . import constraints as assign_constraints
from . import spatial
from .arbitration import get_arbitration_rule
from .manager import AssignmentManager
from .policy.claim import get_claim_policy
from .policy.offer import get_offer_policy
from .pooled_planner import PooledPlannerContext, resolve_market_config, run_pooled_market
from .pools import PoolMarket

# Optional: run the (CPU-bound) per-haulier solve in a real worker thread so it
# yields the eventlet hub instead of blocking the other agents sharing the worker
# process. Off by default — with spatial matching the solve is small enough that
# this rarely matters; enable via the assignment profile (``solve_in_thread``) for
# very heavy scenarios. Guarded so non-eventlet contexts (tests, smoke) still work.
try:  # pragma: no cover - depends on runtime pool
    from eventlet import tpool as _eventlet_tpool
except Exception:  # pragma: no cover
    _eventlet_tpool = None


class AssignmentApp(ORSimApp):
    """Periodic assignment: match unassigned orders to feasible online trucks, then notify trucks."""

    def __init__(self, run_id: str, sim_clock: str, behavior: Dict[str, Any], messenger, agent_helper=None):
        super().__init__(
            run_id=run_id,
            sim_clock=sim_clock,
            behavior=behavior,
            messenger=messenger,
            agent_helper=agent_helper,
        )
        # Plug-and-play solver selection: ``strategy`` names a solver in the
        # registry, ``solver_params`` tunes it (e.g. dual-cycle bonus). Unknown
        # strategy falls back to the default (logged once) rather than crashing.
        prof = self.behavior.get("profile") or {}
        self._solver = get_solver(prof.get("strategy"), prof.get("solver_params"))
        self._solver_params = prof.get("solver_params") or {}
        # Logged once if haulier scoping blocks all matches (likely missing haulier_id).
        self._warned_haulier_block = False
        # (truck_id, order_id) -> collaboration tag for the current tick's matches.
        self._share_tags: Dict[Tuple[str, str], Dict[str, Any]] = {}

    @property
    def managed_statemachine(self):
        return None

    @property
    def interaction_ground_truth_list(self):
        return []

    @property
    def runtime_behavior_schema(self):
        return {
            "steps_per_action": {"type": "integer", "required": False},
            "response_rate": {"type": "number", "required": False},
            "step_only_on_events": {"type": "boolean", "required": False},
            "profile": {"type": "dict", "required": False, "allow_unknown": True},
        }

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials, role="admin")

    def _create_manager(self):
        return AssignmentManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get("profile") or {},
            persona=self.behavior.get("persona") or {},
        )

    def launch(self, sim_clock):
        super().launch(sim_clock)

    def handle_app_topic_messages(self, payload):
        pass

    def _assignment_profile(self) -> Dict[str, Any]:
        return self.behavior.get("profile") or {}

    def _order_publish_payload(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Shape expected by ``TruckApp.handle_assignment`` / smoke tests."""
        prof = order.get("profile") or {}
        pickup_facility_resource_id = self.manager.resolve_facility_resource_id(prof.get("pickup_facility_name"))
        dropoff_facility_resource_id = self.manager.resolve_facility_resource_id(prof.get("dropoff_facility_name"))
        return {
            "_id": order.get("_id"),
            "pickup_loc": order.get("pickup_loc") or prof.get("pickup_loc"),
            "dropoff_loc": order.get("dropoff_loc") or prof.get("dropoff_loc"),
            "pickup_service_time": order.get("pickup_service_time") or prof.get("pickup_service_time", 120),
            "dropoff_service_time": order.get("dropoff_service_time") or prof.get("dropoff_service_time", 120),
            "profile": prof,
            "pickup_facility_resource_id": pickup_facility_resource_id,
            "dropoff_facility_resource_id": dropoff_facility_resource_id,
        }

    # --- shared-pool cooperation (plan §6.6) ---------------------------------

    @staticmethod
    def _resolved_topology(prof: Dict[str, Any]) -> str:
        """Effective planner topology, with ``two-stage`` as a deprecated alias.

        Read from the profile on every call rather than cached in ``__init__`` so a
        behavior patched after construction (and the tests that build the app via
        ``__new__``) resolve correctly. Unknown values fail soft to ``partitioned``.
        """
        planner = (prof or {}).get("planner")
        topology = str((planner or {}).get("topology") or "partitioned")
        if topology == "two-stage":
            return "pooled"
        return topology if topology in ("partitioned", "pooled") else "partitioned"

    # --- the solver seam (plan §3.2, §7 R-I1b) --------------------------------

    @staticmethod
    def _rebate_aware(prof: Dict[str, Any]) -> bool:
        """Whether the compiled profile opts a solver into rebate pricing.

        Read from the profile on every call, matching ``_resolved_topology``'s
        deliberate re-read policy, so a behavior patched after construction takes
        effect. Defaults ``false`` — and no shipped scenario sets it — so the book
        below is constructed, and ``set_rebate_book`` is called, on **no** shipped
        path today (plan §3.2).
        """
        planner = (prof or {}).get("planner") or {}
        return bool(planner.get("rebate_aware", False))

    def _inject_rebate_book(self, prof: Dict[str, Any]) -> None:
        """Build and inject a :class:`RebateBook`, but ONLY when opted in.

        With ``planner.rebate_aware`` false (the default, and every shipped
        scenario's value), this is a no-op: no facility documents are fetched, no
        book is constructed, and ``set_rebate_book`` is never called — so this
        injection can never be the thing that perturbs an allocation (R-I1b). The
        existing truck/order candidate projections (``assign()`` above) are
        untouched by this method; it reads facility documents over a SEPARATE,
        narrowly-projected request, only when opted in.
        """
        if not self._rebate_aware(prof):
            return
        book = getattr(self, "_rebate_book_cache", None)
        if book is None:
            # Cached for the life of the app, and that is a correctness-neutral
            # choice: `profile.rebate` is COMPILED data stamped at generation and
            # never mutated during a run (unlike `avg_queue_wait_seconds`, which the
            # facility agent patches onto the same documents). Without the cache this
            # pages the whole facility collection over HTTP on EVERY assignment tick
            # — 300 documents × ~2500 ticks — which is precisely the per-arrival
            # HTTP cost the plan cited when rejecting a truck-side lookup (§3.1
            # option (b)). An opt-in seam must not smuggle that back in.
            try:
                docs = self.manager._paged_where(
                    self.manager._facility_url(),
                    {"run_id": self.manager.run_id},
                    projection={"_id": 1, "profile.rebate": 1},
                )
                book = RebateBook.from_facility_docs(docs)
            except Exception:  # pragma: no cover - never block a tick on this opt-in read
                # R2-9 / review F10: fall through to CLEARING the book, never `return`.
                # Returning left whatever book was injected on a previous tick attached
                # to the solver, so a mid-run read failure would silently keep pricing
                # from stale data — a solver would go on believing a schedule it can no
                # longer read. An explicit None is a legible "I do not know", and the
                # seam's own contract already defines None as "never injected".
                logging.exception(
                    "AssignmentApp: failed to build the opt-in RebateBook — clearing "
                    "any previously injected book rather than pricing from stale data"
                )
                book = None
            # Only a SUCCESSFUL read is cached (mirroring `_haulier_roster_cache`):
            # facilities may not be created yet on the first tick, and freezing an
            # empty book would silently disable pricing for the whole run. An empty
            # book from a successful read of a rebate-less scenario is still cached —
            # re-paging 300 documents every tick to rediscover "nothing" is the bug
            # this cache exists to prevent.
            if book is not None and docs:
                self._rebate_book_cache = book
        try:
            self._solver.set_rebate_book(book)
        except AttributeError:  # pragma: no cover - a third-party solver without it
            pass

    @staticmethod
    def _tick_seed(run_id: str, time_step: int) -> int:
        """Stable 64-bit per-tick seed. Identical inputs => identical awards (I-P5).

        Scope honesty: this makes the PLANNER deterministic for a given tick input.
        It does NOT make a whole run reproducible — agent scheduling is async.
        """
        digest = hashlib.blake2b(f"{run_id}|{time_step}".encode("utf-8"), digest_size=8)
        return int.from_bytes(digest.digest(), "big")

    def _market_components(self, prof: Dict[str, Any]):
        """Resolve (offer, claim, arbitration, max_rounds) from ``planner.market``.

        Cached per app instance. Algorithm names are FAIL-SOFT (the registries log
        once and fall back) — a typo must degrade a run, never abort it. ``market``
        is optional so a bundle compiled before it existed still runs pooled.
        """
        # ONE resolver, shared with the run stamp, so a run can never record a
        # market block different from the one it executed (plan §13.4 FIX-7).
        effective = resolve_market_config((prof or {}).get("planner"))
        # Cache KEYED ON the resolved config rather than "forever": this matches
        # `_resolved_topology`'s deliberate re-read policy, so a behavior patched
        # after construction takes effect in both places instead of one (review
        # F16 — two adjacent resolvers arguing opposite staleness positions). The
        # key is cheap; instantiating three policy objects per tick is not.
        cache_key = json.dumps(effective, sort_keys=True, default=str)
        cached_key, cached = getattr(self, "_market_cache", (None, None))
        if cached is not None and cached_key == cache_key:
            return cached
        cached = (
            get_offer_policy(effective["offer"]["type"], effective["offer"]["params"]),
            get_claim_policy(effective["claim"]["type"], effective["claim"]["params"]),
            get_arbitration_rule(
                effective["arbitration"]["type"], effective["arbitration"]["params"]
            ),
            effective["max_rounds"],
        )
        self._market_cache = (cache_key, cached)
        return cached

    def round_stats(self) -> Dict[str, Any]:
        """Per-run round provenance for the pooled market (plan §14.4 R3-5/R3-6).

        ``ticks_truncated_by_backstop > 0`` means the auction was cut short on at
        least one tick, so that tick under-served and the run's throughput and
        deadhead numbers are suspect. A trustworthy cooperation result needs 0.

        Two round-2 corrections are baked in here:

        * ``rounds_mean`` averages over **auction ticks only** (``rounds_used >= 1``).
          Averaging in the no-op ticks — where the market opened with nothing free to
          allocate — diluted the figure that is quoted as *auction depth* and made a
          deeper auction look shallower (review MEDIUM-9). Idle ticks are still
          reported, as ``ticks_idle``, because losing them would hide the opposite
          error.
        * ``ticks`` is split into ``ticks_in_horizon`` / ``ticks_drain`` so the count
          sits on the same horizon as the KPI block beside it (review HIGH-4).
        """
        s = getattr(self, "_round_stats", None)
        if not s or not s.get("ticks"):
            return {
                "ticks": 0, "ticks_in_horizon": 0, "ticks_drain": 0,
                "ticks_idle": 0, "auction_ticks": 0,
                "rounds_min": None, "rounds_mean": None, "rounds_max": None,
                "ticks_truncated_by_backstop": 0, "max_rounds_backstop": None,
                "candidate_pairs_round1": 0, "candidate_pairs_total": 0,
            }
        auction = s["auction_ticks"]
        return {
            "ticks": s["ticks"],
            "ticks_in_horizon": s["ticks_in_horizon"],
            "ticks_drain": s["ticks_drain"],
            "ticks_idle": s["ticks_idle"],
            "auction_ticks": auction,
            # min/mean over ticks where an auction actually ran; max over all.
            "rounds_min": s["rounds_min"],
            "rounds_mean": (round(s["rounds_sum"] / auction, 3) if auction else None),
            "rounds_max": s["rounds_max"],
            "ticks_truncated_by_backstop": s["truncated"],
            "max_rounds_backstop": s.get("backstop"),
            "candidate_pairs_round1": s["cand_round1"],
            "candidate_pairs_total": s["cand_total"],
        }

    def _patch_round_stats_to_run_config(self, *, force: bool = False) -> bool:
        """Write the market's round provenance onto ``run_config.meta.market``.

        **Why it is done from the AGENT and not at finalize.** The pooled market runs
        inside the long-lived celery workers (CLAUDE.md §8) and the run record is
        written by a different process, so there is no in-process handoff. The wire
        cannot carry it either: ``trip.meta.collaboration`` only rides along on
        SHARED assignments (``truck/app.py`` attaches ``_collaboration`` only when
        ``shared`` is true), so a tick that truncated while awarding nothing
        cross-haulier would leave no trace at all — and truncation is exactly the
        condition that must never be silent.

        So the agent PATCHes the run record itself, using the admin REST client it
        already holds. ``run_config.meta`` is an unschema'd dict in the Eve model, so
        this needs **no new resource and no api container rebuild** (CLAUDE.md §8).

        Best-effort by construction: any failure is logged at debug and swallowed —
        provenance must never be able to break an assignment tick.
        """
        stats = getattr(self, "_round_stats", None)
        if not stats or not stats.get("ticks"):
            return False
        # Push on a schedule, and additionally whenever the round profile grows —
        # a new maximum is new information and there may be few ticks in total, so
        # a pure modulo cadence can leave the final snapshot stale.
        grew = stats.get("rounds_max") != stats.get("_last_pushed_max")
        if not force and not grew and stats["ticks"] % _ROUND_STATS_PATCH_EVERY:
            return False
        stats["_last_pushed_max"] = stats.get("rounds_max")
        try:
            from apps.common.resource_client_mixin import get_http_session
            from apps.config import settings

            base = settings["OPENRIDE_SERVER_URL"]
            timeout = settings.get("NETWORK_REQUEST_TIMEOUT", 10)
            session = get_http_session()
            found = session.get(
                f"{base}/run-config",
                headers=self.user.get_headers(),
                params={"where": json.dumps({"run_id": self.run_id})},
                timeout=timeout,
            )
            items = (found.json() or {}).get("_items") or []
            if not items:
                return False
            doc = items[0]
            # Dotted patch: leaves every other meta key untouched, and mirrors how
            # SimulationRuntime.update_status patches `step_metrics.<k>`.
            response = session.patch(
                f"{base}/run-config/{doc['_id']}",
                headers=self.user.get_headers(etag=doc["_etag"]),
                data=json.dumps({"meta.market": self.round_stats()}),
                timeout=timeout,
            )
            return response.status_code in (200, 201)
        except Exception:
            logging.debug("Round-stats provenance patch failed (non-fatal).", exc_info=True)
            return False

    def _record_round_stats(self, result, max_rounds=None, time_step=None) -> None:
        s = getattr(self, "_round_stats", None)
        if s is None:
            s = {"ticks": 0, "ticks_in_horizon": 0, "ticks_drain": 0, "ticks_idle": 0,
                 "auction_ticks": 0, "rounds_sum": 0, "rounds_min": None,
                 "rounds_max": None, "truncated": 0, "backstop": None, "warned": False,
                 "cand_round1": 0, "cand_total": 0}
            self._round_stats = s
        r = int(result.rounds_used)
        # Candidate provenance (plan §14.4 R3-2): round-1 parity with `partitioned`
        # is the guarantee; the per-tick total legitimately exceeds it in a
        # multi-round auction. Recording both makes the ratio auditable instead of
        # arguable.
        s["cand_round1"] += int(getattr(result, "candidate_pairs_round1", 0) or 0)
        s["cand_total"] += int(getattr(result, "candidate_pairs_total", 0) or 0)
        if max_rounds is not None:
            s["backstop"] = int(max_rounds)
        s["ticks"] += 1

        # Horizon split: a tick past the simulation horizon is post-horizon DRAIN,
        # not part of the measured window the KPI block covers.
        horizon = getattr(self, "_sim_horizon_steps", None)
        if horizon and time_step is not None and int(time_step) >= int(horizon):
            s["ticks_drain"] += 1
        else:
            s["ticks_in_horizon"] += 1

        if r >= 1:
            s["auction_ticks"] += 1
            s["rounds_sum"] += r
            s["rounds_min"] = r if s["rounds_min"] is None else min(s["rounds_min"], r)
        else:
            # The market opened with nothing free to allocate. Real, but not depth.
            s["ticks_idle"] += 1
        s["rounds_max"] = r if s["rounds_max"] is None else max(s["rounds_max"], r)

        if not result.converged:
            s["truncated"] += 1
            # Push immediately: truncation is the one condition that must never be
            # silent, and the run may end before the next scheduled patch.
            self._patch_round_stats_to_run_config(force=True)
            if not s["warned"]:
                # Loud, once, and greppable in celery_log.txt — agent-side logs do
                # not reach simulation_log.txt (CLAUDE.md §8).
                logging.error(
                    "POOLED MARKET TRUNCATED BY BACKSTOP on tick %d (rounds_used=%d). "
                    "The auction had not converged; this run under-serves and its "
                    "cooperation numbers are NOT trustworthy. Raise "
                    "planner.market.max_rounds.",
                    s["ticks"], r,
                )
                s["warned"] = True

    def close(self, sim_clock):
        """FINAL FLUSH of the market provenance before the agent leaves (R3-5).

        Without this the stamp was whatever the last scheduled push happened to
        catch — the review measured ``ticks: 15`` against a true 17. A provenance
        stamp that stops early is worse than none, because it reads exact.
        """
        try:
            self._patch_round_stats_to_run_config(force=True)
        except Exception:  # pragma: no cover - never block shutdown
            logging.debug("final market-provenance flush failed", exc_info=True)
        return super().close(sim_clock)

    def _assign_pooled(
        self,
        candidates: List[Dict[str, Any]],
        orders: List[Dict[str, Any]],
        prof: Dict[str, Any],
        time_step: int,
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """One planner per company, cooperating through shared order pools.

        Seeds the tick RNG, builds the :class:`PoolMarket` from the active
        structure, and hands off to :func:`run_pooled_market` (plan §4.1 steps 2-7).
        """
        max_pickup_travel = prof.get("max_travel_time_pickup")

        def _cost(t: Dict[str, Any], o: Dict[str, Any]) -> float:
            return assign_constraints.assignment_cost(t, o, self._solver_params)

        def _pair_allowed_own(t: Dict[str, Any], o: Dict[str, Any]) -> bool:
            return assign_constraints.pair_allowed(
                t, o, max_travel_time_pickup=max_pickup_travel
            )

        def _pair_allowed_pooled(t: Dict[str, Any], o: Dict[str, Any]) -> bool:
            return assign_constraints.pair_allowed_ignoring_haulier(
                t, o, max_travel_time_pickup=max_pickup_travel
            )

        tick_seed = self._tick_seed(str(self.run_id), int(time_step or 0))
        rng = random.Random(tick_seed)
        # Inject the seeded RNG so the solver's equal-cost shuffle is reproducible
        # for this tick. Solvers default to the `random` MODULE when unset, so the
        # legacy path is completely unaffected by this (plan §6.7).
        try:
            self._solver.set_rng(rng)
        except AttributeError:  # pragma: no cover - a third-party solver without it
            pass
        # Order-independent tie-breaking for the pooled path (plan §13.4 FIX-3).
        # Never set on the legacy path, so `partitioned` keeps its shuffle exactly.
        try:
            self._solver.set_tiebreak(tick_seed)
        except AttributeError:  # pragma: no cover
            pass

        structure = assign_constraints.active_cooperation_structure(prof)
        market = PoolMarket.from_structure(structure)

        trucks_by_haulier: Dict[str, List[Dict[str, Any]]] = {}
        for t in candidates:
            hid = assign_constraints.haulier_of(t)
            if hid:  # a truck with no haulier can never match (fail-closed)
                trucks_by_haulier.setdefault(hid, []).append(t)
        orders_by_haulier: Dict[str, List[Dict[str, Any]]] = {}
        for o in orders:
            hid = assign_constraints.haulier_of(o)
            if hid:
                orders_by_haulier.setdefault(hid, []).append(o)

        offer, claim, arbitration, max_rounds = self._market_components(prof)
        ctx = PooledPlannerContext(
            solver=self._solver,
            offer=offer,
            claim=claim,
            arbitration=arbitration,
            max_rounds=max_rounds,
            pair_allowed_own=_pair_allowed_own,
            pair_allowed_pooled=_pair_allowed_pooled,
            cost=_cost,
            rng=rng,
            tick_seed=tick_seed,
            spatial_params={
                "cell_deg": float(prof.get("spatial_cell_deg", spatial.DEFAULT_CELL_DEG)),
                "per_order": max(1, int(prof.get(
                    "spatial_candidates_per_order", spatial.DEFAULT_PER_ORDER_CANDIDATES
                ))),
                "max_rings": max(0, int(prof.get("spatial_max_rings", spatial.DEFAULT_MAX_RINGS))),
            },
            use_spatial=bool(prof.get("use_spatial_matching", True)),
            max_trucks_per_haulier=max(
                1, int(prof.get("max_trucks_per_haulier", _DEFAULT_MAX_TRUCKS_PER_HAULIER))
            ),
        )

        result = run_pooled_market(
            trucks_by_haulier=trucks_by_haulier,
            orders_by_haulier=orders_by_haulier,
            market=market,
            ctx=ctx,
        )
        assignment, share_tags = result.assignment, result.share_tags
        self._share_tags = share_tags
        self._record_round_stats(result, max_rounds, time_step=time_step)
        self._patch_round_stats_to_run_config()
        if share_tags:
            logging.info(
                "Pooled cooperation: %d cross-haulier award(s) this tick "
                "(structure %s, round cap %d).",
                len(share_tags),
                (structure or {}).get("id"),
                max_rounds,
            )
        return assignment

    def assign(self, sim_clock: str, time_step: int = 0) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        # Reset the collaboration tags up front so a mid-assign exception can never
        # leave a previous tick's tags to be paired with a later tick's matches.
        self._share_tags = {}
        prof = self._assignment_profile()
        # Opt-in solver seam (plan §3.2): a no-op unless planner.rebate_aware is
        # true, which no shipped scenario sets. See `_inject_rebate_book`.
        self._inject_rebate_book(prof)
        respect_online = bool(prof.get("respect_truck_online_state", True))
        reject_busy = bool(prof.get("reject_if_active_haul_trip", True))
        max_pickup_travel = prof.get("max_travel_time_pickup")
        max_trucks_per_haulier = max(
            1, int(prof.get("max_trucks_per_haulier", _DEFAULT_MAX_TRUCKS_PER_HAULIER))
        )
        # Spatial matching: only score trucks near each order's pickup instead of
        # the full per-haulier cross product. Bounds cost *and* favours low-deadhead
        # (nearest) trucks. On by default; the random-sample cap above is the
        # fallback when disabled or when the solver can't take pre-built pairs.
        use_spatial = bool(prof.get("use_spatial_matching", True))
        cell_deg = float(prof.get("spatial_cell_deg", spatial.DEFAULT_CELL_DEG))
        per_order = max(1, int(prof.get("spatial_candidates_per_order", spatial.DEFAULT_PER_ORDER_CANDIDATES)))
        max_rings = max(0, int(prof.get("spatial_max_rings", spatial.DEFAULT_MAX_RINGS)))
        solve_in_thread = bool(prof.get("solve_in_thread", False))

        projection_truck = {"state": 1, "profile": 1, "persona": 1, "_id": 1, "meta": 1}
        projection_order = {
            "state": 1,
            "profile": 1,
            "pickup_loc": 1,
            "dropoff_loc": 1,
            "pickup_service_time": 1,
            "dropoff_service_time": 1,
            "order_size": 1,
            "_id": 1,
        }

        trucks = self.manager.list_trucks(projection=projection_truck, online_only=respect_online)
        orders = self.manager.list_unassigned_orders(projection=projection_order)
        # Eve ``order.state`` can lag behind haul creation (MQTT updates the order). Exclude any
        # order already referenced by an open haul so we do not assign the same order twice.
        claimed_order_ids = self.manager.order_ids_with_open_haul()
        orders = [o for o in orders if str(o.get("_id")) not in claimed_order_ids]
        busy_ids = {str(x) for x in self.manager.active_haul_truck_ids()} if reject_busy else set()

        candidates = assign_constraints.filter_assignable_trucks(
            trucks,
            busy_ids,
            respect_truck_online_state=respect_online,
            reject_if_active_haul_trip=reject_busy,
            online_state_name=self.manager.online_state_name(),
        )

        # Shared-pool cooperation (plan §6.6): ONE planner per company, cooperating
        # through shared order pools. Selected per run via ORSIM_PLANNER_TOPOLOGY;
        # the DEFAULT IS STILL 'partitioned' and everything below this branch is the
        # legacy path, byte-for-byte unchanged — it is the regression guard for I-P7.
        if self._resolved_topology(prof) == "pooled":
            return self._assign_pooled(candidates, orders, prof, time_step)

        # Cooperation (collaboration plan §3): the baked structure decides which
        # hauliers may share jobs. No structure / no edges => adjacency is empty and
        # everything below degenerates to today's own-haulier-only behavior.
        structure = assign_constraints.active_cooperation_structure(prof)
        adjacency = assign_constraints.share_adjacency(structure)
        component_of: Dict[str, int] = {}
        if adjacency and isinstance(structure, dict):
            for idx, members in enumerate(structure.get("components") or []):
                if isinstance(members, (list, tuple)) and len(members) > 1:
                    for hid in members:
                        component_of[str(hid)] = idx

        def _pair_allowed(t: Dict[str, Any], o: Dict[str, Any]) -> bool:
            # Own-haulier pairs keep the strict rule; cross pairs require a
            # structure edge (share_eligible) + physical feasibility. Own and
            # partner trucks compete ON EQUAL FOOTING — the deadhead cost decides,
            # never fleet availability.
            if assign_constraints.haulier_matches(t, o):
                return assign_constraints.pair_allowed(
                    t, o, max_travel_time_pickup=max_pickup_travel
                )
            if adjacency and assign_constraints.share_eligible(t, o, adjacency):
                return assign_constraints.pair_allowed_ignoring_haulier(
                    t, o, max_travel_time_pickup=max_pickup_travel
                )
            return False

        def _cost(t: Dict[str, Any], o: Dict[str, Any]) -> float:
            # Soft objective (deadhead km, dual-cycle aware). Cost-blind solvers
            # (e.g. RandomAssignment) ignore this; greedy minimises it.
            return assign_constraints.assignment_cost(t, o, self._solver_params)

        # Partition both sides by the structure's connected component — one logical
        # PLANNER per component. A haulier with no partners (or no structure at all)
        # is its own singleton partition, which is exactly the pre-cooperation
        # haulier bucket, so matching stays structural and fleet-scale-safe.
        def _partition_key(haulier_id):
            if haulier_id is None:
                return None
            hid = str(haulier_id)
            if hid in component_of:
                return ("component", component_of[hid])
            return ("haulier", hid)

        truck_buckets: Dict[Any, List[Dict[str, Any]]] = {}
        for t in candidates:
            truck_buckets.setdefault(
                _partition_key(assign_constraints.haulier_of(t)), []
            ).append(t)

        order_buckets: Dict[Any, List[Dict[str, Any]]] = {}
        for o in orders:
            order_buckets.setdefault(
                _partition_key(assign_constraints.haulier_of(o)), []
            ).append(o)

        def _serves(truck_haulier: str, order_haulier: str) -> bool:
            return truck_haulier == order_haulier or (
                order_haulier in adjacency.get(truck_haulier, ())
            )

        def _solve_bucket(
            bucket_trucks: List[Dict[str, Any]],
            bucket_orders: List[Dict[str, Any]],
        ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
            if use_spatial and hasattr(self._solver, "solve_pairs"):
                # Candidates are generated PER ORDER-HAULIER over only the trucks
                # eligible to serve that haulier (own + edge partners). A single
                # component-wide index let share-INELIGIBLE trucks (in the bucket
                # but with no edge — e.g. C's trucks for A's orders in a chain
                # A-B-C) consume the per-order candidate cap by pure proximity and
                # starve orders while their own haulier had free trucks in range.
                # ONE solve still runs over the union of pairs, so trucks/orders
                # are never double-assigned across the groups.
                orders_by_haulier: Dict[str, List[Dict[str, Any]]] = {}
                for o in bucket_orders:
                    oh = assign_constraints.haulier_of(o)
                    if oh:
                        orders_by_haulier.setdefault(oh, []).append(o)
                pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
                for oh, o_group in orders_by_haulier.items():
                    eligible = [
                        t for t in bucket_trucks
                        if (assign_constraints.haulier_of(t) or "") and _serves(
                            assign_constraints.haulier_of(t), oh
                        )
                    ]
                    if not eligible:
                        continue
                    pairs.extend(
                        spatial.iter_candidate_pairs(
                            eligible,
                            o_group,
                            pair_allowed=_pair_allowed,
                            cell_deg=cell_deg,
                            per_order=per_order,
                            max_rings=max_rings,
                        )
                    )
                return list(self._solver.solve_pairs(pairs, cost=_cost))
            # Fallback (spatial disabled, or a solver without solve_pairs): bound
            # the cross product by random-sampling the truck pool per partition.
            bt = bucket_trucks
            if len(bt) > max_trucks_per_haulier:
                bt = random.sample(bt, max_trucks_per_haulier)
            return list(
                self._solver.solve(bt, bucket_orders, pair_allowed=_pair_allowed, cost=_cost)
            )

        assignment: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        share_tags: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for partition_key, bucket_orders in order_buckets.items():
            # ``None`` haulier never matches (fail-closed in haulier_matches); skip it.
            if partition_key is None:
                continue
            bucket_trucks = truck_buckets.get(partition_key)
            if not bucket_trucks:
                continue
            if solve_in_thread and _eventlet_tpool is not None:
                result = _eventlet_tpool.execute(_solve_bucket, bucket_trucks, bucket_orders)
            else:
                result = _solve_bucket(bucket_trucks, bucket_orders)
            # benefit_km = owner's best own truck STILL FREE after this solve minus
            # the chosen cost — computed by a direct scan of the partition's own-fleet
            # trucks (bounded: cross matches are few), NOT from the capped candidate
            # list, so a crowded-out own truck can't fabricate "unserveable" (F2).
            # SIGNED on purpose (F3): cost-blind solvers (RandomAssignment) and
            # cap-limited greedy can genuinely pick a worse-than-own partner truck —
            # a negative benefit is real information, not an error. None = the owner
            # truly had no free feasible truck after the solve.
            used_truck_ids = {str(t.get("_id")) for t, _o in result}
            for truck, order in result:
                t_haulier = assign_constraints.haulier_of(truck)
                o_haulier = assign_constraints.haulier_of(order)
                if t_haulier and o_haulier and t_haulier != o_haulier:
                    oid = str(order.get("_id"))
                    own_costs = [
                        _cost(t, order)
                        for t in bucket_trucks
                        if str(t.get("_id")) not in used_truck_ids
                        and assign_constraints.haulier_of(t) == o_haulier
                        and _pair_allowed(t, order)
                    ]
                    finite = [c for c in own_costs if c != float("inf")]
                    best = min(finite) if finite else None
                    share_tags[(str(truck.get("_id")), oid)] = {
                        "shared": True,
                        "owner_haulier_id": o_haulier,
                        "carrier_haulier_id": t_haulier,
                        "benefit_km": (round(best - _cost(truck, order), 3)
                                       if best is not None else None),
                    }
            assignment.extend(result)
        self._share_tags = share_tags
        if share_tags:
            logging.info(
                "Cooperation: %d cross-haulier assignment(s) this tick (structure %s).",
                len(share_tags),
                (structure or {}).get("id"),
            )

        # Haulier scoping is enforced (no fallback). If there were assignable trucks AND
        # unassigned orders but nothing matched, the data is almost certainly missing or
        # mismatched haulier_id — surface it loudly instead of silently running dead.
        if candidates and orders and not assignment and not self._warned_haulier_block:
            missing_truck = sum(1 for t in candidates if assign_constraints.haulier_of(t) is None)
            missing_order = sum(1 for o in orders if assign_constraints.haulier_of(o) is None)
            logging.error(
                "Assignment produced 0 matches with %d assignable trucks and %d unassigned "
                "orders — haulier scoping blocked everything (trucks missing haulier_id: %d, "
                "orders missing haulier_id: %d). Regenerate behaviors so every truck/order "
                "carries a haulier_id.",
                len(candidates), len(orders), missing_truck, missing_order,
            )
            self._warned_haulier_block = True
        return assignment

    def publish(self, assignment: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
        share_tags = getattr(self, "_share_tags", {}) or {}
        for truck, order in assignment:
            tid = truck.get("_id")
            if not tid:
                continue
            tag = share_tags.get((str(tid), str(order.get("_id")))) or {}
            payload = AssignedHaulTripPayload(
                action=ContainerLogisticsActions.ASSIGNED_HAUL_TRIP,
                order=self._order_publish_payload(order),
                truck_id=tid,
                shared=bool(tag.get("shared", False)),
                owner_haulier_id=tag.get("owner_haulier_id"),
                carrier_haulier_id=tag.get("carrier_haulier_id"),
                benefit_km=tag.get("benefit_km"),
                # Shared-pool audit trail (plan §D6): these make benefit_km a
                # CHECKABLE identity (benefit == reserve - awarded) instead of a
                # bare difference. All optional — absent on the legacy path.
                pool_id=tag.get("pool_id"),
                awarded_cost_km=tag.get("awarded_cost_km"),
                owner_reserve_km=tag.get("owner_reserve_km"),
                market_round=tag.get("market_round"),
            )
            topic = f"{self.run_id}/{tid}"
            try:
                self.messenger.client.publish(topic, json.dumps(payload.__dict__, default=str))
            except Exception:
                logging.exception("Failed to publish assignment to %s", topic)
