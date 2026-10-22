from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

# How often the pooled market's round provenance is pushed onto run_config.
# Cheap (two HTTP calls, a few dozen times per run) and bounded: the recorded
# stats can lag the true tick count by at most this many ticks, but a truncation
# event is pushed immediately, so `ticks_truncated_by_backstop` is never stale.
_ROUND_STATS_PATCH_EVERY = 5

# Appended to an UnknownPolicyError raised agent-side (celery journal, not sim.log):
# a name the worker does not know is the one failure a stale worker explains.
_STALE_WORKER_HINT = (
    " If you just added this policy, restart the celery workers "
    "(systemctl --user restart openride-celery): agents import policy code once at worker start."
)

from apps.common.user_registry import UserRegistry
from apps.container_logistics.message_data_models import AssignedHaulTripPayload
from apps.container_logistics.rebate import RebateBook
from apps.container_logistics.statemachine import ContainerLogisticsActions
from apps.container_logistics.policies.compat.wire import wire_fields
from apps.container_logistics.policies.framework import UnknownPolicyError
from orsim.lifecycle import ORSimApp

from . import accounting
from . import constraints as assign_constraints
from .manager import AssignmentManager

# Optional: run each (CPU-bound) per-group solve in a real worker thread so it yields
# the eventlet hub instead of blocking the other agents sharing the worker process.
# Off by default — with spatial matching the solve is small enough that this rarely
# matters; enable via the assignment profile (``solve_in_thread``) for very heavy
# scenarios. It reaches the solver as ``ctx.execute``. Guarded so non-eventlet
# contexts (tests, smoke) still work.
try:  # pragma: no cover - depends on runtime pool
    from eventlet import tpool as _eventlet_tpool
except Exception:  # pragma: no cover
    _eventlet_tpool = None


def _direct_call(fn, *args):
    """``ctx.execute`` without ``solve_in_thread``: a plain call."""
    return fn(*args)


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
        # Plug-and-play dispatch selection: the compiled ``policies.planner`` block,
        # or the legacy keys (``strategy``/``solver_params``/``planner.topology``/
        # ``planner.market``), resolves to a selection TREE of registered dispatch
        # policies (e.g. Partitioned/GreedyNearest). An unknown name raises (no
        # fallback); only that error gets the stale-worker suffix -- a param or timing
        # error is not explained by it.
        prof = self.behavior.get("profile") or {}
        try:
            self._solver = self._build_dispatch_policy(prof)
        except UnknownPolicyError as exc:
            raise type(exc)(f"{exc}{_STALE_WORKER_HINT}") from exc
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

    # --- the dispatch slot (round 2: docs/plans/policy_slots_round2_whole_tick.md §2) ---
    #
    # Import-cycle rule (round-2 §4): ``policies.dispatch`` modules are imported ONLY
    # locally: in the two accessors below (``_build_dispatch_policy``,
    # ``_dispatch_runner``), in ``_tick_seed`` (the one seed formula,
    # ``dispatch.tick.tick_seed``) and in ``_dispatch_tick`` (the clock value).
    # ``policies.dispatch`` reaches back into this package (constraints, spatial,
    # pooled_planner), so a module-top import here would cycle through
    # ``assignment/__init__`` (tests/test_policy_import_cycles.py pins it). The leaf
    # ``policies.framework`` and ``assignment.accounting`` are safe at module top.

    @staticmethod
    def _build_dispatch_policy(prof: Dict[str, Any]):
        """Resolve the profile's dispatch selection and instantiate its tree (raises)."""
        from apps.container_logistics.policies.dispatch.profile import (  # local: cycle rule
            build_policy_tree,
            resolve_dispatch_for_profile,
        )

        return build_policy_tree(resolve_dispatch_for_profile(prof))

    def _dispatch_runner(self, prof: Dict[str, Any]):
        """The ``DispatchRunner`` for ``self._solver`` under ``prof``, cached on its inputs.

        Rebuilt (and re-bound) when ``_solver`` is swapped, a profile key the selection
        reads (``policies``, ``strategy``, ``solver_params``, ``use_spatial_matching``,
        ``planner``) is patched after construction (the tests that build the app via
        ``__new__`` rely on it), or a WORLD key the bound tree's ``bind`` read
        (``BoundTree.world_reads``, e.g. ``cooperation``) changed. ``_solver`` is fitted
        into the selection (``bind_instance``: reused as the top, or wrapped as the
        innermost leaf). When a reused top's composites were stale (a patched profile)
        and ``bind_instance`` rebuilt them around the same leaf, the rebuilt top becomes
        ``_solver``, so later injections (``_inject_rebate_book``) reach the tree that
        runs (review C3 #4). Raises ``PolicyError`` on a mismatch or a refused
        declaration.
        """
        from apps.container_logistics.policies.dispatch.runner import build_runner_for_app  # local: cycle rule

        def _key(runner):
            return (
                id(self._solver),
                json.dumps(prof.get("policies"), sort_keys=True, default=str),
                prof.get("strategy"),
                json.dumps(prof.get("solver_params"), sort_keys=True, default=str),
                prof.get("use_spatial_matching", True),
                json.dumps(prof.get("planner"), sort_keys=True, default=str),
                runner.tree.digest(prof),
            )

        cached_key, cached = getattr(self, "_dispatch_runner_cache", (None, None))
        if cached is not None and cached_key == _key(cached):
            return cached
        runner = build_runner_for_app(self._solver, prof)
        if runner.policy is not self._solver and runner.tree.leaf.instance is not self._solver:
            self._solver = runner.policy  # the stale top was rebuilt (bind_instance)
        self._dispatch_runner_cache = (_key(runner), runner)
        return runner

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

    # --- the solver seam (plan §3.2, §7 R-I1b) --------------------------------

    @staticmethod
    def _rebate_aware(prof: Dict[str, Any]) -> bool:
        """Whether the compiled profile opts a solver into rebate pricing.

        Read from the profile on every call (like the dispatch runner's cache key),
        so a behavior patched after construction takes effect. Defaults ``false`` — and no shipped scenario sets it — so the book
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
        """Stable 64-bit per-tick seed (``dispatch.tick.tick_seed``). Identical inputs => identical awards (I-P5).

        Scope honesty: this makes the PLANNER deterministic for a given tick input.
        It does NOT make a whole run reproducible — agent scheduling is async.
        """
        from apps.container_logistics.policies.dispatch.tick import tick_seed  # local: cycle rule

        return tick_seed(run_id, time_step)

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

    def _record_round_stats(self, market_notes: Dict[str, Any], time_step=None) -> None:
        """Fold one tick's market provenance (``Plan.notes["market"]``) into ``_round_stats``."""
        s = getattr(self, "_round_stats", None)
        if s is None:
            s = {"ticks": 0, "ticks_in_horizon": 0, "ticks_drain": 0, "ticks_idle": 0,
                 "auction_ticks": 0, "rounds_sum": 0, "rounds_min": None,
                 "rounds_max": None, "truncated": 0, "backstop": None, "warned": False,
                 "cand_round1": 0, "cand_total": 0}
            self._round_stats = s
        r = int(market_notes["rounds_used"])
        # Candidate provenance (plan §14.4 R3-2): round-1 parity with `partitioned`
        # is the guarantee; the per-tick total legitimately exceeds it in a
        # multi-round auction. Recording both makes the ratio auditable instead of
        # arguable.
        s["cand_round1"] += int(market_notes.get("candidate_pairs_round1", 0) or 0)
        s["cand_total"] += int(market_notes.get("candidate_pairs_total", 0) or 0)
        if market_notes.get("max_rounds") is not None:
            s["backstop"] = int(market_notes["max_rounds"])
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

        if not market_notes["converged"]:
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

        assignment = self._dispatch_tick(prof, candidates, orders, sim_clock, time_step, solve_in_thread)

        # Haulier scoping is enforced (no fallback). If there were assignable trucks AND
        # unassigned orders but nothing matched, the data is almost certainly missing or
        # mismatched haulier_id — surface it loudly instead of silently running dead.
        # Not on a tick whose plan (or a scope's) the framework discarded or whose market
        # company crashed: that was already logged (message 18/19/20), the haulier text
        # would misdiagnose it, and the one-shot warning must stay armed (review C3 #6).
        if candidates and orders and not assignment and not self._warned_haulier_block \
                and not getattr(self, "_tick_contract_events", 0):
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

    def _dispatch_tick(
        self,
        prof: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        orders: List[Dict[str, Any]],
        sim_clock: str,
        time_step: int,
        solve_in_thread: bool,
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Decide the whole tick with the selected policy; account the shares; record provenance.

        The FRAMEWORK half of a tick (plan ``plan_contract_final.md`` §2.7): the tick,
        the runner (solve -> check against ownership-or-declared and physical
        feasibility -> commit the pairs), then the solver-independent share accounting
        (priced with the tick's measurement, dials ``self._solver_params``) and, when
        the solver reports a market, its round provenance. Returns the committed pairs.
        """
        from apps.container_logistics.policies.dispatch.tick import DispatchClock  # local: cycle rule

        runner = self._dispatch_runner(prof)
        use_tpool = solve_in_thread and _eventlet_tpool is not None
        tick = runner.tick(
            candidates=candidates,
            orders=orders,
            profile=prof,
            clock=DispatchClock(
                sim_clock=sim_clock,
                time_step=int(time_step or 0),
                horizon_steps=getattr(self, "_sim_horizon_steps", None),
            ),
            seed=self._tick_seed(str(getattr(self, "run_id", "")), int(time_step or 0)),
            execute=_eventlet_tpool.execute if use_tpool else _direct_call,
            dials=self._solver_params,
        )
        events_before = runner.stats.get("violations", 0) + runner.stats.get("crashes", 0)
        self._tick_contract_events = 0
        outcome = runner.run(tick)
        self._tick_contract_events = (
            runner.stats.get("violations", 0) + runner.stats.get("crashes", 0) - events_before)
        mtt = prof.get("max_travel_time_pickup")

        def own_allowed(t, o):
            # The owner reserve's rule: the strict own-haulier feasibility.
            return assign_constraints.pair_allowed(t, o, max_travel_time_pickup=mtt)

        self._share_tags = accounting.render_share_tags(
            accounting.account_shares(outcome.pairs, candidates=candidates,
                                      cost=tick.measure.cost, own_allowed=own_allowed),
            fmt=runner.share_tag_format,
            solver_notes=outcome.solver_notes,
        )
        market_notes = outcome.tick_notes.get("market")
        if market_notes is not None:
            # Solver provenance: only a solver that ran a market reports it, so the
            # agent-side meta.market writer stays on the pooled path only.
            self._record_round_stats(market_notes, time_step=time_step)
            self._patch_round_stats_to_run_config()
        if self._share_tags:
            logging.info("Planner %s: %d cross-company assignment(s) this tick.",
                         runner.resolved.label, len(self._share_tags))
        return outcome.pairs

    def publish(self, assignment: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> None:
        share_tags = getattr(self, "_share_tags", {}) or {}
        for truck, order in assignment:
            tid = truck.get("_id")
            if not tid:
                continue
            tag = share_tags.get((str(tid), str(order.get("_id")))) or {}
            # The share fields (and a solver's ``solver_notes``) come from the tag via
            # the compat wire table; an unshared pair gets the dataclass defaults.
            payload = AssignedHaulTripPayload(
                action=ContainerLogisticsActions.ASSIGNED_HAUL_TRIP,
                order=self._order_publish_payload(order),
                truck_id=tid,
                **wire_fields(tag),
            )
            topic = f"{self.run_id}/{tid}"
            try:
                self.messenger.client.publish(topic, json.dumps(payload.to_wire_dict(), default=str))
            except Exception:
                logging.exception("Failed to publish assignment to %s", topic)
